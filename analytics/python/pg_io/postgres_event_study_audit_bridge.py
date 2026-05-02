"""
postgres_event_study_audit_bridge.py
-------------------------------------
Phase F: 各段で emit された audit を集約して確認するユーティリティ。
主に検証・verify スクリプトから使用する。
"""
from __future__ import annotations

import logging
from typing import Any

from psycopg import Connection

logger = logging.getLogger(__name__)


def fetch_audit_for_trace(conn: Connection, trace_id: str) -> dict[str, Any]:
    """
    trace_id に紐づく全監査イベントをフェーズ別に返す。
    """
    result: dict[str, Any] = {
        "trace_id": trace_id,
        "pipeline_audit": [],
        "audit_events": [],
        "summary": {},
    }

    # event_study_pipeline_audit
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT phase, object_type, object_id, event_type, decision, decision_reason, created_at
            FROM event_study_pipeline_audit
            WHERE trace_id = %s
            ORDER BY created_at
            """,
            (trace_id,),
        )
        cols = [d[0] for d in cur.description]
        result["pipeline_audit"] = [dict(zip(cols, row)) for row in cur.fetchall()]

    # audit_events (QED本体) - trace_id が text / uuid 両対応
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT object_type, object_id, event_type, decision, decision_reason_code, created_at
                FROM audit_events
                WHERE trace_id::text = %s
                ORDER BY created_at
                """,
                (trace_id,),
            )
            cols = [d[0] for d in cur.description]
            result["audit_events"] = [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            logger.debug("audit_events query failed: %s", e)

    # サマリー
    phases = [r["phase"] for r in result["pipeline_audit"]]
    decisions = [r["decision"] for r in result["pipeline_audit"]]
    result["summary"] = {
        "total_pipeline_events": len(result["pipeline_audit"]),
        "total_qed_events": len(result["audit_events"]),
        "phases": list(set(phases)),
        "decisions": {d: decisions.count(d) for d in set(decisions)},
    }

    return result


def fetch_resolution_summary(conn: Connection, trace_id: str) -> dict[str, Any]:
    """trace_id に紐づく target_resolution_log を返す。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT artifact_id, resolution_status, matched_rule_name,
                   matched_target_type, matched_target_id, matched_code,
                   candidate_count, resolved_at
            FROM target_resolution_log
            WHERE trace_id = %s
            ORDER BY resolved_at
            """,
            (trace_id,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    statuses = [r["resolution_status"] for r in rows]
    return {
        "trace_id": trace_id,
        "rows": rows,
        "counts": {s: statuses.count(s) for s in set(statuses)},
    }
