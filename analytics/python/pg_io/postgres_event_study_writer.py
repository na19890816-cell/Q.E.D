"""
postgres_event_study_writer.py
-------------------------------
Phase A: DuckDB 生成 event study panel を
  event_study_summary_runs / event_study_summaries へ UPSERT する。

入力: pandas DataFrame (parquet から読んだもの)
必須列: benchmark_id, event_offset
任意列: event_date, abnormal_return, car_from_t0, normal_return, actual_return, n_events
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg import Connection

logger = logging.getLogger(__name__)

# DuckDB panel に期待する必須列
REQUIRED_PANEL_COLS = {"benchmark_id", "event_offset"}
# あれば取り込む任意列
OPTIONAL_PANEL_COLS = {
    "event_date", "abnormal_return", "car_from_t0",
    "normal_return", "actual_return", "n_events",
}


def _build_run_id(source_name: str, batch_label: str) -> str:
    return f"{source_name}__{batch_label}"


def _make_trace_id(namespace: str, run_id: str) -> str:
    """UUID5 ベースの再現可能 trace_id を生成する。"""
    ns = uuid.uuid5(uuid.NAMESPACE_DNS, namespace)
    return str(uuid.uuid5(ns, run_id))


class EventStudyWriter:
    """
    DuckDB panel → PostgreSQL の書き戻し担当。
    """

    def __init__(
        self,
        conn: Connection,
        *,
        source_name: str | None = None,
        panel_kind: str | None = None,
        batch_label: str | None = None,
        trace_namespace: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self.conn = conn
        self.source_name = source_name or os.environ.get("EVENT_STUDY_SOURCE_NAME", "event_study_v1")
        self.panel_kind = panel_kind or os.environ.get("EVENT_STUDY_PANEL_KIND", "abnormal_return")
        self.batch_label = batch_label or os.environ.get("EVENT_STUDY_BATCH_LABEL", "batch_default")
        self.trace_namespace = trace_namespace or os.environ.get(
            "EVENT_STUDY_TRACE_NAMESPACE", "event_study"
        )
        self.dry_run = dry_run or (
            os.environ.get("EVENT_STUDY_WRITEBACK_DRY_RUN", "false").lower() == "true"
        )
        self.run_id = _build_run_id(self.source_name, self.batch_label)
        self.trace_id = _make_trace_id(self.trace_namespace, self.run_id)
        logger.info(
            "EventStudyWriter init: run_id=%s trace_id=%s dry_run=%s",
            self.run_id, self.trace_id, self.dry_run,
        )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def upsert_run(self, total_events: int = 0, extra_metadata: dict[str, Any] | None = None) -> str:
        """
        event_study_summary_runs を UPSERT し、run_id を返す。
        """
        meta = extra_metadata or {}
        meta.update({
            "source_name": self.source_name,
            "panel_kind": self.panel_kind,
            "batch_label": self.batch_label,
            "trace_namespace": self.trace_namespace,
        })

        if self.dry_run:
            logger.info("[DRY_RUN] upsert_run skipped: run_id=%s", self.run_id)
            return self.run_id

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO event_study_summary_runs
                    (run_id, trace_id, source_name, panel_kind, batch_label,
                     status, total_events, run_metadata, started_at)
                VALUES (%s, %s, %s, %s, %s, 'running', %s, %s, now())
                ON CONFLICT (run_id) DO UPDATE SET
                    trace_id        = EXCLUDED.trace_id,
                    status          = 'running',
                    total_events    = EXCLUDED.total_events,
                    run_metadata    = EXCLUDED.run_metadata,
                    updated_at      = now()
                """,
                (
                    self.run_id, self.trace_id, self.source_name,
                    self.panel_kind, self.batch_label,
                    total_events, json.dumps(meta),
                ),
            )
        logger.info("upsert_run OK: run_id=%s total_events=%d", self.run_id, total_events)
        return self.run_id

    def upsert_summaries_from_df(self, df: Any) -> int:
        """
        pandas DataFrame から event_study_summaries を一括 UPSERT する。
        Returns: upserted row count
        """
        import pandas as pd

        # 必須列チェック
        missing = REQUIRED_PANEL_COLS - set(df.columns)
        if missing:
            raise ValueError(
                f"DuckDB panel に必須列が不足しています: {missing}\n"
                f"実際の列: {list(df.columns)}"
            )

        if self.dry_run:
            logger.info("[DRY_RUN] upsert_summaries_from_df skipped: rows=%d", len(df))
            return 0

        count = 0
        with self.conn.cursor() as cur:
            for _, row in df.iterrows():
                extra: dict[str, Any] = {}
                for col in df.columns:
                    if col not in REQUIRED_PANEL_COLS | OPTIONAL_PANEL_COLS:
                        val = row[col]
                        # NaN / NaT → None
                        if pd.isna(val):
                            extra[col] = None
                        else:
                            extra[col] = val

                def _safe(col_name: str) -> Any:
                    val = row.get(col_name)
                    if val is None:
                        return None
                    try:
                        if pd.isna(val):
                            return None
                    except (TypeError, ValueError):
                        pass
                    return val

                event_date = _safe("event_date")
                if event_date is not None:
                    # numpy/pandas 型 → Python date
                    if hasattr(event_date, "date"):
                        event_date = event_date.date()
                    else:
                        try:
                            from datetime import date
                            event_date = pd.Timestamp(event_date).date()
                        except Exception:
                            event_date = None

                cur.execute(
                    """
                    INSERT INTO event_study_summaries
                        (run_id, trace_id, benchmark_id, event_date, event_offset,
                         abnormal_return, car_from_t0, normal_return, actual_return,
                         n_events, extra_metrics)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id, benchmark_id, event_offset) DO UPDATE SET
                        event_date      = EXCLUDED.event_date,
                        abnormal_return = EXCLUDED.abnormal_return,
                        car_from_t0     = EXCLUDED.car_from_t0,
                        normal_return   = EXCLUDED.normal_return,
                        actual_return   = EXCLUDED.actual_return,
                        n_events        = EXCLUDED.n_events,
                        extra_metrics   = EXCLUDED.extra_metrics,
                        updated_at      = now()
                    """,
                    (
                        self.run_id,
                        self.trace_id,
                        str(row["benchmark_id"]),
                        event_date,
                        int(row["event_offset"]),
                        _safe("abnormal_return"),
                        _safe("car_from_t0"),
                        _safe("normal_return"),
                        _safe("actual_return"),
                        _safe("n_events"),
                        json.dumps(extra),
                    ),
                )
                count += 1

        logger.info("upsert_summaries OK: run_id=%s rows=%d", self.run_id, count)
        return count

    def complete_run(self, total_events: int) -> None:
        """run を completed に更新する。"""
        if self.dry_run:
            logger.info("[DRY_RUN] complete_run skipped")
            return
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE event_study_summary_runs
                SET status='completed', total_events=%s, completed_at=now(), updated_at=now()
                WHERE run_id=%s
                """,
                (total_events, self.run_id),
            )

    def fail_run(self, reason: str) -> None:
        """run を failed に更新する。"""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE event_study_summary_runs
                SET status='failed', run_metadata=run_metadata || %s, updated_at=now()
                WHERE run_id=%s
                """,
                (json.dumps({"failure_reason": reason}), self.run_id),
            )
