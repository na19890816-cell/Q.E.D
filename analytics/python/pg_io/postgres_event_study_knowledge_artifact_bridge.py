"""
postgres_event_study_knowledge_artifact_bridge.py
--------------------------------------------------
Phase C: experiment_reports → knowledge_artifacts への昇格。

処理:
  1. event_study_experiment_report_bridge から report 情報取得
  2. artifact_id 生成 (trace_id + seq)
  3. knowledge_artifacts に UPSERT
  4. audit emit
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from psycopg import Connection

from .postgres_audit_event_writer import AuditEventWriter

logger = logging.getLogger(__name__)


class KnowledgeArtifactBridge:

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

    def promote(self, run_id: str) -> dict[str, Any]:
        """
        run_id に対応する experiment_report を knowledge_artifact へ昇格する。
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.run_id, b.trace_id, b.report_title, b.report_summary,
                       b.report_markdown, b.report_metadata, b.experiment_run_id
                FROM event_study_experiment_report_bridge b
                WHERE b.run_id = %s AND b.promotion_status = 'applied'
                """,
                (run_id,),
            )
            row = cur.fetchone()

        if row is None:
            raise ValueError(
                f"run_id={run_id} の experiment_report が見つかりません "
                f"(promotion_status='applied' のものが必要)"
            )

        (run_id_, trace_id, report_title, report_summary,
         report_markdown, report_meta_raw, experiment_run_id) = row
        report_meta: dict[str, Any] = report_meta_raw or {}

        artifact_id = self._make_artifact_id(trace_id, run_id)
        artifact_tag = report_meta.get("artifact_tag", f"event_study:{run_id}")

        if self.dry_run:
            self.audit.emit(
                trace_id=trace_id, phase="knowledge_artifact",
                object_type="knowledge_artifacts", object_id=artifact_id,
                event_type="TRANSITION_DRY_RUN", decision="DRY_RUN",
                decision_reason="DRY_RUN_MODE",
                metadata={"run_id": run_id, "artifact_id": artifact_id},
                dry_run=False,
            )
            logger.info("[DRY_RUN] KnowledgeArtifactBridge.promote skipped: run_id=%s", run_id)
            return {
                "artifact_id": artifact_id,
                "trace_id": trace_id,
                "promotion_status": "dry_run",
            }

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO knowledge_artifacts
                    (artifact_id, trace_id, source_run_id, source_experiment_run_id,
                     artifact_type, artifact_tag, title, summary, body_markdown,
                     metadata, status)
                VALUES (%s, %s, %s, %s,
                        'event_study_report', %s, %s, %s, %s, %s, 'draft')
                ON CONFLICT (artifact_id) DO UPDATE SET
                    trace_id                    = EXCLUDED.trace_id,
                    source_run_id               = EXCLUDED.source_run_id,
                    source_experiment_run_id    = EXCLUDED.source_experiment_run_id,
                    artifact_tag                = EXCLUDED.artifact_tag,
                    title                       = EXCLUDED.title,
                    summary                     = EXCLUDED.summary,
                    body_markdown               = EXCLUDED.body_markdown,
                    metadata                    = EXCLUDED.metadata,
                    updated_at                  = now()
                """,
                (
                    artifact_id, trace_id, run_id,
                    experiment_run_id,
                    artifact_tag, report_title, report_summary,
                    report_markdown, json.dumps(report_meta),
                ),
            )

        self.audit.emit(
            trace_id=trace_id, phase="knowledge_artifact",
            object_type="knowledge_artifacts", object_id=artifact_id,
            event_type="TRANSITION_APPLIED", decision="APPLIED",
            decision_reason="KNOWLEDGE_ARTIFACT_PROMOTED",
            metadata={"run_id": run_id, "artifact_id": artifact_id},
        )

        logger.info("KA promote OK: run_id=%s artifact_id=%s", run_id, artifact_id)
        return {
            "artifact_id": artifact_id,
            "trace_id": trace_id,
            "promotion_status": "applied",
            "artifact_tag": artifact_tag,
        }

    @staticmethod
    def _make_artifact_id(trace_id: str, run_id: str) -> str:
        """UUID5 ベースの再現可能 artifact_id。"""
        ns = uuid.uuid5(uuid.NAMESPACE_DNS, "knowledge_artifact")
        return str(uuid.uuid5(ns, f"{trace_id}:{run_id}"))
