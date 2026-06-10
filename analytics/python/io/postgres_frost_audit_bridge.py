"""
postgres_frost_audit_bridge.py
------------------------------
FROST audit_events 発行 IO 層。

audit_events テーブルへイベントを発行し、
frost_audit_event_bridges テーブルに追跡レコードを UPSERT する。

発行するイベント名:
  - frost.run.started
  - frost.candidate.ingested
  - frost.candidate.evaluated
  - frost.candidate.selected
  - frost.candidate.rejected
  - frost.candidate.review_required
  - frost.promotion.ready
  - frost.run.completed

audit_events の decision 制約 (4 ステータス):
  APPLIED / DRY_RUN / REJECTED / CONFLICTED

設計原則:
  - psycopg3, %s プレースホルダー
  - UPSERT: frost_audit_event_bridges は INSERT で追記 (同一イベントは別行)
  - dry_run=True → frost_audit_event_bridges.decision = 'DRY_RUN'
  - 例外は emitted=False として記録、呼び出し元には伝搬させない
"""
from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.sql import SQL, Identifier

from analytics.python.frost.frost_contracts import (
    FrostAuditRecord,
    FrostDecision,
    FrostRunOutput,
)


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_json(obj: Any) -> str:
    def _sanitize(v: Any) -> Any:
        if isinstance(v, float):
            return None if (math.isnan(v) or math.isinf(v)) else v
        if isinstance(v, dict):
            return {k: _sanitize(vv) for k, vv in v.items()}
        if isinstance(v, list):
            return [_sanitize(x) for x in v]
        if isinstance(v, (datetime,)):
            return v.isoformat()
        return v
    return json.dumps(_sanitize(obj), default=str)


def _decision_for_audit(dry_run: bool, base_decision: str = "APPLIED") -> str:
    """
    dry_run=True なら 'DRY_RUN' を返す。
    それ以外は base_decision をそのまま返す。
    """
    if dry_run:
        return "DRY_RUN"
    return base_decision


# ---------------------------------------------------------------------------
# frost_audit_event_bridges への INSERT
# ---------------------------------------------------------------------------

def _insert_audit_bridge(
    conn: psycopg.Connection,
    record: FrostAuditRecord,
) -> None:
    """
    frost_audit_event_bridges に INSERT する (追記形式)。
    """
    sql = SQL(
        "INSERT INTO {tbl} "
        "(audit_bridge_id, run_id, candidate_id, trace_id, "
        " event_name, event_status, decision, audit_event_id, "
        " payload_json, occurred_at, created_at) "
        "VALUES "
        "(%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, now()) "
        "ON CONFLICT DO NOTHING"
    ).format(tbl=Identifier("frost_audit_event_bridges"))

    conn.execute(sql, (
        record.audit_bridge_id,
        record.run_id,
        record.candidate_id,
        record.trace_id,
        record.event_name,
        record.event_status,
        record.decision,
        record.audit_event_id,
        _safe_json(record.payload),
        record.occurred_at,
    ))


# ---------------------------------------------------------------------------
# audit_events への発行
# ---------------------------------------------------------------------------

def _emit_to_audit_events(
    conn: psycopg.Connection,
    run_id: str,
    candidate_id: Optional[str],
    trace_id: str,
    event_name: str,
    decision: str,
    payload: Dict[str, Any],
    phase: str = "FROST",
) -> Optional[str]:
    """
    audit_events テーブルに 1 イベントを INSERT する。

    audit_events テーブルが存在する場合のみ書く。
    存在しない場合はスキップ (silent)。

    Returns
    -------
    str or None
        生成した audit_event_id
    """
    audit_event_id = str(uuid.uuid4())

    # audit_events テーブルの存在確認 (一度だけ)
    check = conn.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='audit_events' LIMIT 1"
    ).fetchone()
    if not check:
        return None

    # audit_events のスキーマは既存 Q.E.D. に合わせる
    # 最低限: event_id, run_id, trace_id, phase, event_name, decision, payload_json
    try:
        conn.execute(
            SQL(
                "INSERT INTO {tbl} "
                "(event_id, trace_id, phase, event_name, decision, "
                " payload_json, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb, now()) "
                "ON CONFLICT (event_id) DO NOTHING"
            ).format(tbl=Identifier("audit_events")),
            (
                audit_event_id,
                trace_id,
                phase,
                event_name,
                decision,
                _safe_json({
                    "run_id":       run_id,
                    "candidate_id": candidate_id,
                    **payload,
                }),
            ),
        )
    except Exception:
        # audit_events のスキーマが異なる場合はスキップ
        return None

    return audit_event_id


# ---------------------------------------------------------------------------
# 公開 API: 個別イベント発行
# ---------------------------------------------------------------------------

def emit_frost_event(
    conn: psycopg.Connection,
    run_id: str,
    trace_id: str,
    event_name: str,
    dry_run: bool,
    candidate_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    base_decision: str = "APPLIED",
) -> FrostAuditRecord:
    """
    1 つの FROST イベントを発行し、FrostAuditRecord を返す。

    Parameters
    ----------
    conn : psycopg.Connection
    run_id : str
    trace_id : str
    event_name : str
    dry_run : bool
    candidate_id : str, optional
    payload : dict, optional
    base_decision : str
        APPLIED / REJECTED / CONFLICTED (dry_run=True の場合は自動的に DRY_RUN)

    Returns
    -------
    FrostAuditRecord
    """
    decision   = _decision_for_audit(dry_run, base_decision)
    payload    = payload or {}
    audit_id   = None
    event_status = "emitted"

    try:
        audit_id = _emit_to_audit_events(
            conn, run_id, candidate_id, trace_id, event_name, decision, payload
        )
    except Exception as exc:
        event_status = "failed"
        payload["_error"] = str(exc)

    record = FrostAuditRecord(
        run_id=run_id,
        candidate_id=candidate_id,
        trace_id=trace_id,
        event_name=event_name,
        event_status=event_status,
        decision=decision,
        audit_event_id=audit_id,
        payload=payload,
        occurred_at=_now(),
    )

    try:
        _insert_audit_bridge(conn, record)
        conn.commit()
    except Exception:
        pass  # bridge INSERT の失敗はサイレント

    return record


# ---------------------------------------------------------------------------
# 公開 API: run 全体の audit イベントを一括発行
# ---------------------------------------------------------------------------

def emit_run_audit_events(
    conn: psycopg.Connection,
    output: FrostRunOutput,
) -> List[FrostAuditRecord]:
    """
    FrostRunOutput から必要な audit イベントを一括発行する。

    発行イベント:
      1. frost.run.completed (run 単位)
      2. frost.candidate.selected (SELECTED 候補ごと)
      3. frost.candidate.rejected (REJECTED 候補ごと)
      4. frost.candidate.review_required (REVIEW_REQUIRED 候補ごと)
      5. frost.promotion.ready (promotion_eligible 候補ごと)

    Returns
    -------
    list of FrostAuditRecord
    """
    records: List[FrostAuditRecord] = []
    dry_run = output.dry_run

    # 1. run completed
    run_payload = {
        "batch_label":    output.batch_label,
        "candidate_count": output.candidate_count,
        "selected_count":  output.selected_count,
        "rejected_count":  output.rejected_count,
        "promotion_count": output.promotion_count,
        "status":          output.status,
    }
    r = emit_frost_event(
        conn, output.run_id, output.trace_id,
        "frost.run.completed", dry_run,
        payload=run_payload,
    )
    records.append(r)

    # 2〜5. 候補ごと
    eval_by_cid = {ev.candidate_id: ev for ev in output.evaluations}

    for decision in output.decisions:
        ev = eval_by_cid.get(decision.candidate_id)
        cand_payload = {
            "frost_score":    decision.frost_score,
            "decision_rank":  decision.decision_rank,
            "oos_sharpe":     ev.oos_sharpe if ev else None,
            "pbo_score":      ev.pbo_score if ev else None,
            "gate_failures":  decision.gate_failures,
        }

        if decision.decision == "SELECTED":
            r = emit_frost_event(
                conn, output.run_id, output.trace_id,
                "frost.candidate.selected", dry_run,
                candidate_id=decision.candidate_id,
                payload=cand_payload,
            )
            records.append(r)

            if decision.promotion_eligible and not dry_run:
                r2 = emit_frost_event(
                    conn, output.run_id, output.trace_id,
                    "frost.promotion.ready", dry_run,
                    candidate_id=decision.candidate_id,
                    payload=cand_payload,
                )
                records.append(r2)

        elif decision.decision == "REJECTED":
            r = emit_frost_event(
                conn, output.run_id, output.trace_id,
                "frost.candidate.rejected", dry_run,
                candidate_id=decision.candidate_id,
                payload=cand_payload,
                base_decision="REJECTED",
            )
            records.append(r)

        elif decision.decision == "REVIEW_REQUIRED":
            r = emit_frost_event(
                conn, output.run_id, output.trace_id,
                "frost.candidate.review_required", dry_run,
                candidate_id=decision.candidate_id,
                payload=cand_payload,
            )
            records.append(r)

    return records
