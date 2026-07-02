"""
postgres_audit_event_writer.py
-------------------------------
audit_events (QED本体) と event_study_pipeline_audit (補助) への
二重書き込みを管理する。

モード:
  strict=True  : audit_events が存在しなければ例外
  strict=False : audit_events がなければ event_study_pipeline_audit のみ書き込む (non-strict)
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import psycopg
from psycopg import Connection

# Phase 6: D6 負債解消 — 共通ユーティリティ / BaseWriter 継承
from analytics.python.pg_io.base_writer import BaseWriter
from .postgres_conn import check_table_columns, table_exists

logger = logging.getLogger(__name__)

# audit_events の必須列 (互換 insert 用)
_AUDIT_EVENTS_CORE_COLS = {
    "trace_id", "case_id", "object_type", "object_id",
    "requested_by", "event_type", "decision", "decision_reason_code", "metadata",
}


class AuditEventWriter(BaseWriter):
    """
    QED audit_events + event_study_pipeline_audit への書き込み担当。
    """

    def __init__(
        self,
        conn: Connection,
        *,
        audit_schema: str | None = None,
        audit_table: str | None = None,
        pipeline_audit_table: str = "event_study_pipeline_audit",
        strict: bool = False,
    ) -> None:
        super().__init__(conn, writer_name="AuditEventWriter")
        self.audit_schema = audit_schema or os.environ.get("AUDIT_EVENTS_SCHEMA", "public")
        self.audit_table = audit_table or os.environ.get("AUDIT_EVENTS_TABLE", "audit_events")
        self.pipeline_audit_table = pipeline_audit_table
        self.strict = strict

        # audit_events の利用可能列を事前確認
        self._available_cols: dict[str, str] = {}
        if table_exists(conn, self.audit_schema, self.audit_table):
            self._available_cols = check_table_columns(conn, self.audit_schema, self.audit_table)
            logger.debug("audit_events available columns: %s", list(self._available_cols.keys()))
        else:
            if strict:
                raise RuntimeError(
                    f"audit_events テーブル {self.audit_schema}.{self.audit_table} が見つかりません (strict=True)"
                )
            logger.warning(
                "audit_events テーブルが見つかりません。pipeline_audit のみ使用します (non-strict)。"
            )

    def emit(
        self,
        *,
        trace_id: str,
        phase: str,
        object_type: str,
        object_id: str,
        event_type: str,
        decision: str,
        decision_reason: str = "PIPELINE_EMIT",
        case_id: str | None = None,
        requested_by: str = "event_study_pipeline",
        metadata: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> None:
        """
        1 件の監査イベントを emit する。

        Parameters
        ----------
        trace_id        : パイプライン横断 trace_id
        phase           : writeback | experiment_report | knowledge_artifact | target_resolution | artifact_link
        object_type     : 対象オブジェクトタイプ
        object_id       : 対象オブジェクト ID
        event_type      : TRANSITION_APPLIED | TRANSITION_DRY_RUN | TRANSITION_REJECTED | TRANSITION_CONFLICTED
        decision        : APPLIED | DRY_RUN | REJECTED | CONFLICTED
        decision_reason : コード文字列
        dry_run         : True のとき DB 書き込みをスキップ
        """
        if dry_run:
            logger.info(
                "[DRY_RUN] audit emit skipped: trace=%s phase=%s decision=%s",
                trace_id, phase, decision,
            )
            return

        _case_id = case_id or f"event_study:{phase}:{trace_id[:8]}"
        _metadata = metadata or {}
        _metadata["pipeline_phase"] = phase

        with self.conn.cursor() as cur:
            # 1. QED audit_events (互換 insert)
            if self._available_cols:
                self._insert_audit_events(
                    cur,
                    trace_id=trace_id,
                    case_id=_case_id,
                    object_type=object_type,
                    object_id=object_id,
                    event_type=event_type,
                    decision=decision,
                    decision_reason_code=decision_reason,
                    requested_by=requested_by,
                    metadata=_metadata,
                )

            # 2. pipeline_audit (補助表・常に書く)
            self._insert_pipeline_audit(
                cur,
                trace_id=trace_id,
                phase=phase,
                object_type=object_type,
                object_id=object_id,
                event_type=event_type,
                decision=decision,
                decision_reason=decision_reason,
                metadata=_metadata,
            )

        logger.info(
            "audit emitted: trace=%s phase=%s obj=%s/%s decision=%s",
            trace_id, phase, object_type, object_id, decision,
        )

    def _insert_audit_events(
        self,
        cur: psycopg.Cursor,
        *,
        trace_id: str,
        case_id: str,
        object_type: str,
        object_id: str,
        event_type: str,
        decision: str,
        decision_reason_code: str,
        requested_by: str,
        metadata: dict[str, Any],
    ) -> None:
        """
        互換 insert: audit_events の列の揺れ (id/event_id/audit_event_id) を吸収する。
        """
        # ID 列の互換
        id_col = "id"
        if "id" not in self._available_cols:
            if "event_id" in self._available_cols:
                id_col = "event_id"
            elif "audit_event_id" in self._available_cols:
                id_col = "audit_event_id"

        # trace_id の型 (text / uuid)
        trace_val: Any = trace_id
        if self._available_cols.get("trace_id") in ("uuid",):
            try:
                trace_val = uuid.UUID(trace_id)
            except ValueError:
                trace_val = trace_id  # 型変換失敗時はそのまま

        import json
        from psycopg import sql as pgsql

        tbl = pgsql.Identifier(self.audit_schema, self.audit_table)
        id_ident = pgsql.Identifier(id_col)

        query = pgsql.SQL(
            """
            INSERT INTO {tbl} ({id_col}, trace_id, case_id, object_type, object_id,
                               requested_by, event_type, decision, decision_reason_code, metadata)
            VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """
        ).format(tbl=tbl, id_col=id_ident)

        cur.execute(
            query,
            (
                trace_val, case_id, object_type, object_id,
                requested_by, event_type, decision, decision_reason_code,
                json.dumps(metadata),
            ),
        )

    def _insert_pipeline_audit(
        self,
        cur: psycopg.Cursor,
        *,
        trace_id: str,
        phase: str,
        object_type: str,
        object_id: str,
        event_type: str,
        decision: str,
        decision_reason: str,
        metadata: dict[str, Any],
    ) -> None:
        import json
        cur.execute(
            """
            INSERT INTO event_study_pipeline_audit
                (trace_id, phase, object_type, object_id, event_type,
                 decision, decision_reason, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                trace_id, phase, object_type, object_id, event_type,
                decision, decision_reason, json.dumps(metadata),
            ),
        )
