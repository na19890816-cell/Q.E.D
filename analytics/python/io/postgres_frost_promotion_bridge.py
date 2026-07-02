"""
postgres_frost_promotion_bridge.py
-----------------------------------
FROST → Q.E.D. 昇格接続 IO 層。

SELECTED 候補を frost_promotion_bridges テーブルに UPSERT し、
Q.E.D. の promotion chain に橋渡しする。

対象テーブル: frost_promotion_bridges

設計原則:
  - dry_run=True の場合は promotion_status='dry_run' で UPSERT する
    (DB には書くが Q.E.D. canonical には反映しない)
  - dry_run=False の場合は promotion_status='applied' で UPSERT する
  - rerun-safe: UNIQUE(run_id, candidate_id) ON CONFLICT DO UPDATE
  - trace_id を全スコープで維持
  - psycopg3, %s プレースホルダー
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.sql import SQL, Identifier

from analytics.python.frost.frost_contracts import (
    FrostDecision,
    FrostEvaluation,
    FrostPromotionRecord,
    FrostRunOutput,
)

# Phase 6: D6 負債解消 — サニタイズ / 時刻ユーティリティを base_writer に一元化
from analytics.python.pg_io.base_writer import (
    safe_json as _safe_json,
    now_utc as _now,
)


# ---------------------------------------------------------------------------
# frost_promotion_bridges UPSERT
# ---------------------------------------------------------------------------

def upsert_frost_promotion_bridge(
    conn: psycopg.Connection,
    record: FrostPromotionRecord,
) -> None:
    """
    frost_promotion_bridges に 1 レコードを UPSERT する。

    UNIQUE(run_id, candidate_id) — 再実行時は promotion_status を上書き。
    """
    sql = SQL(
        "INSERT INTO {tbl} "
        "(bridge_id, run_id, candidate_id, trace_id, "
        " target_entity_type, target_entity_id, artifact_id, link_id, "
        " promotion_status, promotion_payload_json, "
        " frost_score, decision_rank, "
        " promoted_at, error_message, "
        " created_at, updated_at) "
        "VALUES "
        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, "
        " now(), now()) "
        "ON CONFLICT (run_id, candidate_id) DO UPDATE SET "
        "  target_entity_type     = EXCLUDED.target_entity_type, "
        "  target_entity_id       = EXCLUDED.target_entity_id, "
        "  artifact_id            = EXCLUDED.artifact_id, "
        "  promotion_status       = EXCLUDED.promotion_status, "
        "  promotion_payload_json = EXCLUDED.promotion_payload_json, "
        "  frost_score            = EXCLUDED.frost_score, "
        "  decision_rank          = EXCLUDED.decision_rank, "
        "  promoted_at            = EXCLUDED.promoted_at, "
        "  error_message          = EXCLUDED.error_message, "
        "  updated_at             = now()"
    ).format(tbl=Identifier("frost_promotion_bridges"))

    conn.execute(sql, (
        record.bridge_id,
        record.run_id,
        record.candidate_id,
        record.trace_id,
        record.target_entity_type,
        record.target_entity_id,
        record.artifact_id,
        record.link_id,
        record.promotion_status,
        _safe_json(record.promotion_payload),
        record.frost_score,
        record.decision_rank,
        record.promoted_at,
        record.error_message,
    ))


# ---------------------------------------------------------------------------
# FrostRunOutput から昇格レコードを生成・UPSERT
# ---------------------------------------------------------------------------

def promote_frost_decisions(
    conn: psycopg.Connection,
    output: FrostRunOutput,
    target_entity_type: str = "candidate",
) -> List[FrostPromotionRecord]:
    """
    FrostRunOutput の promotion_eligible 候補を frost_promotion_bridges に書く。

    dry_run=True の場合は promotion_status='dry_run' で書き込む
    (canonical side-effect なし、監査記録は残る)。

    Parameters
    ----------
    conn : psycopg.Connection
    output : FrostRunOutput
    target_entity_type : str
        昇格先エンティティ種別 (candidate / hypothesis / knowledge_artifact 等)

    Returns
    -------
    list of FrostPromotionRecord
    """
    dry_run = output.dry_run
    eval_by_cid = {ev.candidate_id: ev for ev in output.evaluations}

    records: List[FrostPromotionRecord] = []

    for decision in output.decisions:
        if not decision.promotion_eligible:
            continue

        ev = eval_by_cid.get(decision.candidate_id)

        promotion_status = "dry_run" if dry_run else "applied"
        promoted_at      = None if dry_run else _now()

        payload = {
            "decision":          decision.decision,
            "frost_score":       decision.frost_score,
            "decision_rank":     decision.decision_rank,
            "review_required":   decision.review_required,
            "review_status":     decision.review_status,
            "batch_label":       output.batch_label,
            "engine_version":    output.engine_version,
            "oos_sharpe":        ev.oos_sharpe if ev else None,
            "rank_ic":           ev.rank_ic if ev else None,
            "pbo_score":         ev.pbo_score if ev else None,
        }

        record = FrostPromotionRecord(
            bridge_id=str(uuid.uuid4()),
            run_id=output.run_id,
            candidate_id=decision.candidate_id,
            trace_id=output.trace_id,
            target_entity_type=target_entity_type,
            target_entity_id=decision.candidate_id,  # 昇格先 ID
            promotion_status=promotion_status,
            promotion_payload=payload,
            frost_score=decision.frost_score,
            decision_rank=decision.decision_rank,
            promoted_at=promoted_at,
        )

        try:
            upsert_frost_promotion_bridge(conn, record)
            records.append(record)
        except Exception as exc:
            record.promotion_status = "error"
            record.error_message    = str(exc)
            # エラーでも記録を保持
            try:
                upsert_frost_promotion_bridge(conn, record)
            except Exception:
                pass
            records.append(record)

    conn.commit()
    return records


# ---------------------------------------------------------------------------
# 昇格ステータス更新 (review 承認後)
# ---------------------------------------------------------------------------

def update_promotion_status(
    conn: psycopg.Connection,
    run_id: str,
    candidate_id: str,
    new_status: str,
    error_message: Optional[str] = None,
) -> None:
    """
    frost_promotion_bridges のステータスを更新する。

    review 承認後 ('pending' → 'applied') などに使用。

    Parameters
    ----------
    conn : psycopg.Connection
    run_id : str
    candidate_id : str
    new_status : str
        pending / applied / dry_run / rejected / conflicted / error
    error_message : str, optional
    """
    sql = SQL(
        "UPDATE {tbl} "
        "SET promotion_status=%s, error_message=%s, "
        "    promoted_at=CASE WHEN %s='applied' THEN now() ELSE promoted_at END, "
        "    updated_at=now() "
        "WHERE run_id=%s AND candidate_id=%s"
    ).format(tbl=Identifier("frost_promotion_bridges"))
    conn.execute(sql, (new_status, error_message, new_status, run_id, candidate_id))
    conn.commit()


# ---------------------------------------------------------------------------
# 昇格状態の一括確認
# ---------------------------------------------------------------------------

def get_pending_promotions(
    conn: psycopg.Connection,
    run_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    pending 状態の昇格レコードを取得する。

    Returns
    -------
    list of dict
    """
    if run_id:
        sql = SQL(
            "SELECT bridge_id, run_id, candidate_id, trace_id, "
            "       target_entity_type, target_entity_id, "
            "       promotion_status, frost_score, decision_rank "
            "FROM {tbl} "
            "WHERE promotion_status='pending' AND run_id=%s "
            "ORDER BY frost_score DESC NULLS LAST"
        ).format(tbl=Identifier("frost_promotion_bridges"))
        rows = conn.execute(sql, (run_id,)).fetchall()
    else:
        sql = SQL(
            "SELECT bridge_id, run_id, candidate_id, trace_id, "
            "       target_entity_type, target_entity_id, "
            "       promotion_status, frost_score, decision_rank "
            "FROM {tbl} "
            "WHERE promotion_status='pending' "
            "ORDER BY frost_score DESC NULLS LAST "
            "LIMIT 100"
        ).format(tbl=Identifier("frost_promotion_bridges"))
        rows = conn.execute(sql).fetchall()

    columns = [
        "bridge_id", "run_id", "candidate_id", "trace_id",
        "target_entity_type", "target_entity_id",
        "promotion_status", "frost_score", "decision_rank",
    ]
    return [dict(zip(columns, row)) for row in rows]
