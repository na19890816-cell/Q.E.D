"""
postgres_event_study_artifact_links_bridge.py
----------------------------------------------
Phase E: resolved target に対して artifact_links を生成する。
unresolved / ambiguous は link を作成せず監査に残す。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from psycopg import Connection

from .postgres_audit_event_writer import AuditEventWriter

logger = logging.getLogger(__name__)


class ArtifactLinksBridge:

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

    def create_links(self, artifact_id: str) -> dict[str, Any]:
        """
        artifact_id に対応する resolved target をもとに artifact_links を作成する。
        unresolved / ambiguous の場合はスキップし監査ログを残す。

        Returns: {artifact_id, trace_id, link_status, target_id, resolution_status}
        """
        # 1. resolution_log から解決済み情報を取得
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT trl.artifact_id, trl.trace_id, trl.resolution_status,
                       trl.matched_target_id, trl.matched_target_type,
                       trl.matched_code, trl.matched_rule_name
                FROM target_resolution_log trl
                WHERE trl.artifact_id = %s
                """,
                (artifact_id,),
            )
            row = cur.fetchone()

        if row is None:
            raise ValueError(
                f"artifact_id={artifact_id} の target_resolution_log が存在しません。"
                "先に TargetRuleResolver.resolve() を実行してください。"
            )

        (artifact_id_, trace_id, resolution_status,
         matched_target_id, matched_target_type,
         matched_code, matched_rule_name) = row

        # 2. unresolved / ambiguous はリンク作成しない
        if resolution_status != "resolved":
            self.audit.emit(
                trace_id=trace_id, phase="artifact_link",
                object_type="artifact_links", object_id=artifact_id,
                event_type="TRANSITION_REJECTED",
                decision="REJECTED",
                decision_reason=f"TARGET_{resolution_status.upper()}",
                metadata={
                    "artifact_id": artifact_id,
                    "resolution_status": resolution_status,
                    "matched_code": matched_code,
                },
                dry_run=self.dry_run,
            )
            logger.info(
                "artifact_link skipped: artifact_id=%s status=%s",
                artifact_id, resolution_status,
            )
            return {
                "artifact_id": artifact_id,
                "trace_id": trace_id,
                "link_status": "skipped",
                "resolution_status": resolution_status,
                "target_id": None,
            }

        # 3. DRY_RUN
        if self.dry_run:
            self.audit.emit(
                trace_id=trace_id, phase="artifact_link",
                object_type="artifact_links", object_id=artifact_id,
                event_type="TRANSITION_DRY_RUN", decision="DRY_RUN",
                decision_reason="DRY_RUN_MODE",
                metadata={"artifact_id": artifact_id, "target_id": str(matched_target_id)},
                dry_run=False,
            )
            logger.info("[DRY_RUN] ArtifactLinksBridge.create_links skipped: %s", artifact_id)
            return {
                "artifact_id": artifact_id,
                "trace_id": trace_id,
                "link_status": "dry_run",
                "resolution_status": "resolved",
                "target_id": str(matched_target_id),
            }

        # 4. artifact_links UPSERT
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO artifact_links
                    (artifact_id, trace_id, target_type, target_id, target_code,
                     resolution_method, link_status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, 'active', %s)
                ON CONFLICT (artifact_id, target_type, target_id) DO UPDATE SET
                    target_code         = EXCLUDED.target_code,
                    resolution_method   = EXCLUDED.resolution_method,
                    link_status         = 'active',
                    metadata            = EXCLUDED.metadata,
                    updated_at          = now()
                """,
                (
                    artifact_id, trace_id,
                    matched_target_type,
                    uuid.UUID(str(matched_target_id)),
                    matched_code,
                    matched_rule_name or "unknown",
                    json.dumps({"trace_id": trace_id}),
                ),
            )

        self.audit.emit(
            trace_id=trace_id, phase="artifact_link",
            object_type="artifact_links", object_id=artifact_id,
            event_type="TRANSITION_APPLIED", decision="APPLIED",
            decision_reason="ARTIFACT_LINK_CREATED",
            metadata={
                "artifact_id": artifact_id,
                "target_id": str(matched_target_id),
                "target_type": matched_target_type,
                "resolution_method": matched_rule_name,
            },
        )

        logger.info(
            "artifact_link created: artifact_id=%s target=%s/%s",
            artifact_id, matched_target_type, matched_target_id,
        )
        return {
            "artifact_id": artifact_id,
            "trace_id": trace_id,
            "link_status": "created",
            "resolution_status": "resolved",
            "target_id": str(matched_target_id),
            "target_type": matched_target_type,
        }
