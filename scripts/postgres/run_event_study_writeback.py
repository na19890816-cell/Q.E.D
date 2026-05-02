#!/usr/bin/env python3
"""
run_event_study_writeback.sh 相当の Python エントリポイント。
DuckDB panel parquet → event_study_summary_runs / event_study_summaries
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# ---- path setup ----
# scripts/postgres/ → prostock/ は parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "analytics/python"))

from dotenv import load_dotenv  # type: ignore[import]

# .env.local を自動ロード (python-dotenv があれば)
_env = _REPO_ROOT / "config/env/.env.local"
if _env.exists():
    load_dotenv(_env)

from pg_io.postgres_conn import get_connection
from pg_io.postgres_audit_event_writer import AuditEventWriter
from pg_io.postgres_event_study_writer import EventStudyWriter
from features.build_event_study_abnormal_return_panel import build_panel

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_writeback")


def main() -> None:
    dry_run = os.environ.get("EVENT_STUDY_WRITEBACK_DRY_RUN", "false").lower() == "true"
    enabled = os.environ.get("EVENT_STUDY_WRITEBACK_ENABLED", "true").lower() == "true"
    if not enabled:
        logger.info("EVENT_STUDY_WRITEBACK_ENABLED=false → スキップ")
        return

    parquet_path = os.environ.get("EVENT_AR_OUTPUT_PATH", "")
    logger.info("writeback 開始: parquet=%s dry_run=%s", parquet_path, dry_run)

    # panel 生成
    df = build_panel(parquet_path=parquet_path or None)
    logger.info("panel rows: %d", len(df))

    with get_connection() as conn:
        audit = AuditEventWriter(conn, strict=False)
        writer = EventStudyWriter(conn, dry_run=dry_run)

        # run UPSERT
        writer.upsert_run(total_events=len(df))

        try:
            # summaries UPSERT
            count = writer.upsert_summaries_from_df(df)
            writer.complete_run(count)

            audit.emit(
                trace_id=writer.trace_id,
                phase="writeback",
                object_type="event_study_summary_runs",
                object_id=writer.run_id,
                event_type="TRANSITION_APPLIED" if not dry_run else "TRANSITION_DRY_RUN",
                decision="APPLIED" if not dry_run else "DRY_RUN",
                decision_reason="WRITEBACK_COMPLETE",
                metadata={"rows": count, "run_id": writer.run_id},
                dry_run=False,
            )
            conn.commit()
            logger.info(
                "writeback 完了: run_id=%s trace_id=%s rows=%d",
                writer.run_id, writer.trace_id, count,
            )
            print(f"RUN_ID={writer.run_id}")
            print(f"TRACE_ID={writer.trace_id}")

        except Exception as e:
            conn.rollback()
            writer.fail_run(str(e))
            conn.commit()
            audit.emit(
                trace_id=writer.trace_id, phase="writeback",
                object_type="event_study_summary_runs", object_id=writer.run_id,
                event_type="TRANSITION_REJECTED", decision="REJECTED",
                decision_reason="WRITEBACK_FAILED",
                metadata={"error": str(e)},
            )
            conn.commit()
            raise


if __name__ == "__main__":
    main()
