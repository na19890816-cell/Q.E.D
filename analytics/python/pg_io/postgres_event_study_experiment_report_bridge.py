"""
postgres_event_study_experiment_report_bridge.py
-------------------------------------------------
Phase B: event_study_summary_runs → experiment_reports への昇格。

処理:
  1. run の summary 統計を集計
  2. experiment_runs に UPSERT (hypothesis_id は run_metadata から取得、なければ dummy)
  3. event_study_experiment_report_bridge に UPSERT
  4. audit emit
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

from psycopg import Connection

from .postgres_audit_event_writer import AuditEventWriter

logger = logging.getLogger(__name__)


class ExperimentReportBridge:

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
        run_id に対応する summary run を experiment_reports へ昇格する。
        Returns: {experiment_run_id, trace_id, promotion_status, report_title}
        """
        with self.conn.cursor() as cur:
            # 1. run 情報取得
            cur.execute(
                """
                SELECT run_id, trace_id, source_name, panel_kind, batch_label,
                       total_events, run_metadata, status
                FROM event_study_summary_runs WHERE run_id = %s
                """,
                (run_id,),
            )
            run = cur.fetchone()
        if run is None:
            raise ValueError(f"run_id={run_id} が event_study_summary_runs に存在しません")

        (run_id_, trace_id, source_name, panel_kind, batch_label,
         total_events, run_metadata_raw, run_status) = run
        run_meta: dict[str, Any] = run_metadata_raw or {}

        if run_status != "completed":
            logger.warning("run_id=%s status=%s (completed でない)", run_id, run_status)

        # 2. summary 統計集計
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*)           AS n_events,
                    AVG(car_from_t0)   AS avg_car,
                    AVG(abnormal_return) AS avg_ar,
                    MIN(event_offset)  AS min_offset,
                    MAX(event_offset)  AS max_offset
                FROM event_study_summaries WHERE run_id = %s
                """,
                (run_id,),
            )
            stats = cur.fetchone()
        n_events, avg_car, avg_ar, min_off, max_off = stats or (0, None, None, None, None)

        # 3. report 生成
        report_title = f"[Event Study] {source_name} / {panel_kind} — {batch_label}"
        report_summary = (
            f"Events: {n_events} | "
            f"avg_CAR: {float(avg_car):.4f} | "
            f"avg_AR: {float(avg_ar):.4f} | "
            f"offset range: [{min_off}, {max_off}]"
        ) if avg_car is not None else f"Events: {n_events}"

        report_md = self._build_markdown(
            run_id=run_id,
            trace_id=trace_id,
            source_name=source_name,
            panel_kind=panel_kind,
            batch_label=batch_label,
            n_events=n_events,
            avg_car=avg_car,
            avg_ar=avg_ar,
            min_off=min_off,
            max_off=max_off,
            run_meta=run_meta,
        )

        report_metadata: dict[str, Any] = {
            "source_name": source_name,
            "panel_kind": panel_kind,
            "batch_label": batch_label,
            "n_events": n_events,
            "avg_car": float(avg_car) if avg_car is not None else None,
            "avg_ar": float(avg_ar) if avg_ar is not None else None,
        }
        # run_meta から candidate_code / hypothesis_code を引き継ぐ
        for key in ("candidate_code", "hypothesis_code", "artifact_tag"):
            if key in run_meta:
                report_metadata[key] = run_meta[key]

        if self.dry_run:
            self.audit.emit(
                trace_id=trace_id, phase="experiment_report",
                object_type="event_study_experiment_report_bridge",
                object_id=run_id, event_type="TRANSITION_DRY_RUN",
                decision="DRY_RUN", decision_reason="DRY_RUN_MODE",
                metadata={"run_id": run_id},
                dry_run=False,  # audit emit 自体は行う
            )
            logger.info("[DRY_RUN] ExperimentReportBridge.promote skipped: run_id=%s", run_id)
            return {
                "experiment_run_id": None,
                "trace_id": trace_id,
                "promotion_status": "dry_run",
                "report_title": report_title,
            }

        # 4. hypothesis_id 解決 (run_meta から、なければ dummy)
        hypothesis_id = run_meta.get("hypothesis_id")
        if hypothesis_id is None:
            hypothesis_id = self._get_or_create_dummy_hypothesis(trace_id, run_id)

        # 5. experiment_runs UPSERT
        experiment_run_id = self._upsert_experiment_run(
            trace_id=trace_id,
            run_id=run_id,
            hypothesis_id=hypothesis_id,
            report_summary=report_summary,
            report_metadata=report_metadata,
            n_events=n_events,
            avg_car=avg_car,
            avg_ar=avg_ar,
        )

        # 6. bridge UPSERT
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO event_study_experiment_report_bridge
                    (run_id, experiment_run_id, trace_id, report_title,
                     report_summary, report_markdown, report_metadata,
                     promotion_status, promoted_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'applied', now())
                ON CONFLICT (run_id) DO UPDATE SET
                    experiment_run_id   = EXCLUDED.experiment_run_id,
                    trace_id            = EXCLUDED.trace_id,
                    report_title        = EXCLUDED.report_title,
                    report_summary      = EXCLUDED.report_summary,
                    report_markdown     = EXCLUDED.report_markdown,
                    report_metadata     = EXCLUDED.report_metadata,
                    promotion_status    = 'applied',
                    promoted_at         = now(),
                    updated_at          = now()
                """,
                (
                    run_id, experiment_run_id, trace_id, report_title,
                    report_summary, report_md, json.dumps(report_metadata),
                ),
            )

        self.audit.emit(
            trace_id=trace_id, phase="experiment_report",
            object_type="event_study_experiment_report_bridge",
            object_id=run_id, event_type="TRANSITION_APPLIED",
            decision="APPLIED", decision_reason="EXPERIMENT_REPORT_PROMOTED",
            metadata={"run_id": run_id, "experiment_run_id": str(experiment_run_id)},
        )

        logger.info(
            "promote OK: run_id=%s experiment_run_id=%s", run_id, experiment_run_id
        )
        return {
            "experiment_run_id": str(experiment_run_id),
            "trace_id": trace_id,
            "promotion_status": "applied",
            "report_title": report_title,
        }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _upsert_experiment_run(
        self,
        trace_id: str,
        run_id: str,
        hypothesis_id: str,
        report_summary: str,
        report_metadata: dict[str, Any],
        n_events: Any,
        avg_car: Any,
        avg_ar: Any,
    ) -> uuid.UUID:
        object_id = f"event_study:{run_id}"
        result_summary = {
            "n_events": n_events,
            "avg_car": float(avg_car) if avg_car is not None else None,
            "avg_ar": float(avg_ar) if avg_ar is not None else None,
        }
        with self.conn.cursor() as cur:
            # 既存チェック
            cur.execute(
                "SELECT id FROM experiment_runs WHERE object_id = %s LIMIT 1",
                (object_id,),
            )
            row = cur.fetchone()
            if row:
                exp_id = row[0]
                cur.execute(
                    """
                    UPDATE experiment_runs SET
                        result_summary = %s,
                        payload        = payload || %s,
                        status         = 'completed',
                        completed_at   = now(),
                        updated_at     = now()
                    WHERE id = %s
                    """,
                    (
                        json.dumps(result_summary),
                        json.dumps({"report_metadata": report_metadata}),
                        exp_id,
                    ),
                )
                return exp_id
            else:
                cur.execute(
                    """
                    INSERT INTO experiment_runs
                        (trace_id, case_id, object_type, object_id, hypothesis_id,
                         experiment_type, parameters, result_summary, status,
                         started_at, completed_at, payload)
                    VALUES (%s, %s, 'experiment_run', %s, %s,
                            'event_study', %s, %s, 'completed', now(), now(), %s)
                    RETURNING id
                    """,
                    (
                        uuid.UUID(trace_id) if len(trace_id) == 36 else uuid.uuid5(uuid.NAMESPACE_DNS, trace_id),
                        f"event_study:{run_id}",
                        object_id,
                        uuid.UUID(hypothesis_id),
                        json.dumps({"run_id": run_id}),
                        json.dumps(result_summary),
                        json.dumps({"report_metadata": report_metadata}),
                    ),
                )
                return cur.fetchone()[0]

    def _get_or_create_dummy_hypothesis(self, trace_id: str, run_id: str) -> str:
        """
        run_meta に hypothesis_id がない場合、ダミー hypothesis を作成して返す。
        冪等: object_id で既存を検索。
        """
        object_id = f"event_study:dummy:{run_id}"
        trace_uuid = uuid.UUID(trace_id) if len(trace_id) == 36 else uuid.uuid5(uuid.NAMESPACE_DNS, trace_id)

        with self.conn.cursor() as cur:
            # まず evidence_bundle を確保
            bundle_object_id = f"event_study:bundle:{run_id}"
            cur.execute(
                "SELECT id FROM evidence_bundles WHERE object_id = %s LIMIT 1",
                (bundle_object_id,),
            )
            brow = cur.fetchone()
            if brow:
                bundle_id = brow[0]
            else:
                cur.execute(
                    """
                    INSERT INTO evidence_bundles (trace_id, case_id, object_id, title, status, payload)
                    VALUES (%s, %s, %s, %s, 'closed', '{}')
                    RETURNING id
                    """,
                    (
                        trace_uuid,
                        f"event_study:{run_id}",
                        bundle_object_id,
                        f"[Event Study] Bundle — {run_id}",
                    ),
                )
                bundle_id = cur.fetchone()[0]

            # hypothesis
            cur.execute(
                "SELECT id FROM hypotheses WHERE object_id = %s LIMIT 1",
                (object_id,),
            )
            hrow = cur.fetchone()
            if hrow:
                return str(hrow[0])

            cur.execute(
                """
                INSERT INTO hypotheses
                    (trace_id, case_id, object_id, title, description,
                     evidence_bundle_id, status, confidence, payload)
                VALUES (%s, %s, %s, %s, %s, %s, 'draft', 0.5, '{}')
                RETURNING id
                """,
                (
                    trace_uuid,
                    f"event_study:{run_id}",
                    object_id,
                    f"[Event Study] Hypothesis — {run_id}",
                    "Auto-generated by event study pipeline",
                    bundle_id,
                ),
            )
            return str(cur.fetchone()[0])

    @staticmethod
    def _build_markdown(
        run_id: str, trace_id: str, source_name: str, panel_kind: str,
        batch_label: str, n_events: Any, avg_car: Any, avg_ar: Any,
        min_off: Any, max_off: Any, run_meta: dict[str, Any],
    ) -> str:
        car_str = f"{float(avg_car):.4f}" if avg_car is not None else "N/A"
        ar_str  = f"{float(avg_ar):.4f}"  if avg_ar  is not None else "N/A"
        return f"""# Event Study Report: {source_name}

## Overview
| Key | Value |
|---|---|
| run_id | `{run_id}` |
| trace_id | `{trace_id}` |
| source_name | {source_name} |
| panel_kind | {panel_kind} |
| batch_label | {batch_label} |

## Summary Statistics
| Metric | Value |
|---|---|
| N Events | {n_events} |
| Avg CAR (t0) | {car_str} |
| Avg Abnormal Return | {ar_str} |
| Offset Range | [{min_off}, {max_off}] |

## Metadata
```json
{json.dumps(run_meta, ensure_ascii=False, indent=2)}
```
"""
