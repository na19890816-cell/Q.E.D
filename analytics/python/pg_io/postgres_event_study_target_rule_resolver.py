"""
postgres_event_study_target_rule_resolver.py
---------------------------------------------
Phase D: knowledge_artifact の metadata / tag から
         target (factor_candidates / hypotheses) を自動解決する。

解決優先順位:
  P1 candidate_code_direct : report_metadata.candidate_code → factor_candidates.name
  P2 hypothesis_code_direct : report_metadata.hypothesis_code → hypotheses.title
  P3 tag_candidate          : artifact_tag = "candidate:CODE"
  P4 tag_hypothesis         : artifact_tag = "hypothesis:CODE"
  P5 (alias fallback)       : 拡張余地

結果:
  resolved   → 1件一致
  ambiguous  → 複数件一致
  unresolved → 0件一致
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from psycopg import Connection
from psycopg import sql as pgsql

from .postgres_artifact_link_target_catalog import TargetRuleCatalog
from .postgres_audit_event_writer import AuditEventWriter
from .postgres_conn import table_exists

logger = logging.getLogger(__name__)

_TAG_CANDIDATE_RE  = re.compile(r"^candidate:(.+)$")
_TAG_HYPOTHESIS_RE = re.compile(r"^hypothesis:(.+)$")


class TargetRuleResolver:
    """
    1 件の knowledge_artifact に対して target を自動解決し、
    target_resolution_log に UPSERT する。
    """

    def __init__(
        self,
        conn: Connection,
        audit_writer: AuditEventWriter,
        *,
        dry_run: bool = False,
    ) -> None:
        self.conn = conn
        self.audit = audit_writer
        self.dry_run = dry_run
        self.catalog = TargetRuleCatalog(conn)

    def resolve(self, artifact_id: str) -> dict[str, Any]:
        """
        artifact_id に対して target resolution を実行する。
        Returns: {artifact_id, trace_id, resolution_status, matched_target_id, ...}
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT artifact_id, trace_id, artifact_tag, metadata
                FROM knowledge_artifacts WHERE artifact_id = %s
                """,
                (artifact_id,),
            )
            row = cur.fetchone()

        if row is None:
            raise ValueError(f"artifact_id={artifact_id} が knowledge_artifacts に存在しません")

        artifact_id_, trace_id, artifact_tag, metadata_raw = row
        metadata: dict[str, Any] = metadata_raw or {}

        rules = self.catalog.load_active_rules()
        result = self._run_rules(
            rules=rules,
            artifact_id=artifact_id,
            artifact_tag=artifact_tag or "",
            metadata=metadata,
        )

        resolution_status = result["resolution_status"]
        decision = "APPLIED" if resolution_status == "resolved" else "REJECTED"
        event_type = (
            "TRANSITION_APPLIED"  if resolution_status == "resolved"
            else "TRANSITION_REJECTED"
        )
        reason = (
            f"TARGET_{resolution_status.upper()}"
        )

        if not self.dry_run:
            self._upsert_resolution_log(
                artifact_id=artifact_id,
                trace_id=trace_id,
                result=result,
            )

        self.audit.emit(
            trace_id=trace_id, phase="target_resolution",
            object_type="knowledge_artifacts", object_id=artifact_id,
            event_type=event_type, decision=decision,
            decision_reason=reason,
            metadata={
                "artifact_id": artifact_id,
                "resolution_status": resolution_status,
                "matched_rule": result.get("matched_rule_name"),
                "candidate_count": result.get("candidate_count", 0),
            },
            dry_run=self.dry_run,
        )

        logger.info(
            "resolve: artifact_id=%s status=%s target=%s",
            artifact_id, resolution_status, result.get("matched_target_id"),
        )
        return {"trace_id": trace_id, **result}

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _run_rules(
        self,
        rules: list[dict[str, Any]],
        artifact_id: str,
        artifact_tag: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """
        ルールを優先順位順に試し、最初に resolved になった結果を返す。
        全ルールで unresolved / ambiguous の場合はその結果を返す。
        """
        last_result: dict[str, Any] = {
            "artifact_id": artifact_id,
            "resolution_status": "unresolved",
            "matched_rule_name": None,
            "matched_target_id": None,
            "matched_target_type": None,
            "matched_code": None,
            "candidate_count": 0,
            "resolution_detail": {},
        }

        for rule in rules:
            code = self._extract_code(rule, artifact_tag, metadata)
            if code is None:
                continue

            matches = self._lookup(
                table=rule["target_table"],
                id_col=rule["target_id_col"],
                code_col=rule["target_code_col"],
                code_value=code,
            )
            count = len(matches)

            if count == 0:
                last_result = {
                    **last_result,
                    "resolution_status": "unresolved",
                    "matched_rule_name": rule["rule_name"],
                    "matched_code": code,
                    "candidate_count": 0,
                    "resolution_detail": {"tried_rule": rule["rule_name"], "code": code},
                }
            elif count == 1:
                target_id, = matches[0]
                target_type = (
                    "factor_candidate" if "candidate" in rule["target_table"]
                    else "hypothesis"
                )
                return {
                    "artifact_id": artifact_id,
                    "resolution_status": "resolved",
                    "matched_rule_name": rule["rule_name"],
                    "matched_target_id": str(target_id),
                    "matched_target_type": target_type,
                    "matched_code": code,
                    "candidate_count": 1,
                    "resolution_detail": {
                        "rule": rule["rule_name"],
                        "code": code,
                        "target_id": str(target_id),
                    },
                }
            else:  # ambiguous
                target_type = (
                    "factor_candidate" if "candidate" in rule["target_table"]
                    else "hypothesis"
                )
                last_result = {
                    "artifact_id": artifact_id,
                    "resolution_status": "ambiguous",
                    "matched_rule_name": rule["rule_name"],
                    "matched_target_id": None,
                    "matched_target_type": target_type,
                    "matched_code": code,
                    "candidate_count": count,
                    "resolution_detail": {
                        "rule": rule["rule_name"],
                        "code": code,
                        "matches": [str(m[0]) for m in matches],
                    },
                }
                # ambiguous は次のルールへはいかず即返す
                return last_result

        return last_result

    def _extract_code(
        self,
        rule: dict[str, Any],
        artifact_tag: str,
        metadata: dict[str, Any],
    ) -> str | None:
        strategy = rule["match_strategy"]
        source_field = rule["source_field"]

        if strategy == "candidate_code":
            return metadata.get(source_field)
        elif strategy == "hypothesis_code":
            return metadata.get(source_field)
        elif strategy == "tag_candidate":
            m = _TAG_CANDIDATE_RE.match(artifact_tag)
            return m.group(1) if m else None
        elif strategy == "tag_hypothesis":
            m = _TAG_HYPOTHESIS_RE.match(artifact_tag)
            return m.group(1) if m else None
        else:
            return None

    def _lookup(
        self,
        table: str,
        id_col: str,
        code_col: str,
        code_value: str,
    ) -> list[tuple]:
        """
        psycopg.sql.Identifier で動的テーブル/列名を安全に扱う。
        """
        if not table_exists(self.conn, "public", table):
            logger.warning("lookup: テーブル %s が存在しません", table)
            return []

        query = pgsql.SQL(
            "SELECT {id_col} FROM {table} WHERE {code_col} = %s"
        ).format(
            id_col=pgsql.Identifier(id_col),
            table=pgsql.Identifier("public", table),
            code_col=pgsql.Identifier(code_col),
        )
        with self.conn.cursor() as cur:
            cur.execute(query, (code_value,))
            return cur.fetchall()

    def _upsert_resolution_log(
        self,
        artifact_id: str,
        trace_id: str,
        result: dict[str, Any],
    ) -> None:
        with self.conn.cursor() as cur:
            matched_id = result.get("matched_target_id")
            cur.execute(
                """
                INSERT INTO target_resolution_log
                    (artifact_id, trace_id, resolution_status, matched_rule_name,
                     matched_target_id, matched_target_type, matched_code,
                     candidate_count, resolution_detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (artifact_id) DO UPDATE SET
                    trace_id            = EXCLUDED.trace_id,
                    resolution_status   = EXCLUDED.resolution_status,
                    matched_rule_name   = EXCLUDED.matched_rule_name,
                    matched_target_id   = EXCLUDED.matched_target_id,
                    matched_target_type = EXCLUDED.matched_target_type,
                    matched_code        = EXCLUDED.matched_code,
                    candidate_count     = EXCLUDED.candidate_count,
                    resolution_detail   = EXCLUDED.resolution_detail,
                    resolved_at         = now()
                """,
                (
                    artifact_id, trace_id,
                    result["resolution_status"],
                    result.get("matched_rule_name"),
                    uuid.UUID(matched_id) if matched_id else None,
                    result.get("matched_target_type"),
                    result.get("matched_code"),
                    result.get("candidate_count", 0),
                    json.dumps(result.get("resolution_detail", {})),
                ),
            )
