"""
postgres_eml_evaluation_writer.py
----------------------------------
EML 評価結果 (eml_alpha_evaluations) を PostgreSQL へ UPSERT する IO 層。

実際のスキーマ (032_eml_alpha_evaluations.sql):
  evaluation_id, candidate_id, trace_id, fold_id,
  fold_start_at, fold_end_at, ic, rank_ic, ic_t_stat, hit_rate, r2_oos,
  sharpe, sortino, calmar, max_drawdown, turnover, cost_drag, cost_adj_sharpe,
  regime_tag, score, metadata, created_at
"""
from __future__ import annotations

import json
import uuid
from typing import List

import psycopg
from psycopg.sql import SQL, Identifier

from analytics.python.alpha.eml.eml_evaluation_runner import EMLEvaluationResult


def upsert_evaluations(
    conn: psycopg.Connection,
    results: List[EMLEvaluationResult],
) -> None:
    """
    eml_alpha_evaluations に一括 UPSERT する。
    実際のスキーマ列に合わせたマッピング。
    """
    sql = SQL(
        "INSERT INTO {tbl} "
        "(evaluation_id, candidate_id, trace_id, fold_id, "
        " fold_start_at, fold_end_at, "
        " ic, rank_ic, ic_t_stat, hit_rate, r2_oos, "
        " sharpe, sortino, calmar, max_drawdown, "
        " turnover, cost_drag, cost_adj_sharpe, "
        " regime_tag, score, metadata, created_at) "
        "VALUES "
        "(%s, %s, %s, %s, "
        " %s, %s, "
        " %s, %s, %s, %s, %s, "
        " %s, %s, %s, %s, "
        " %s, %s, %s, "
        " %s, %s, %s::jsonb, now()) "
        "ON CONFLICT (candidate_id, fold_id) DO UPDATE SET "
        "  rank_ic          = EXCLUDED.rank_ic, "
        "  sharpe           = EXCLUDED.sharpe, "
        "  cost_adj_sharpe  = EXCLUDED.cost_adj_sharpe, "
        "  score            = EXCLUDED.score, "
        "  metadata         = EXCLUDED.metadata"
    ).format(tbl=Identifier("eml_alpha_evaluations"))

    rows = []
    for r in results:
        # fold_id: eval_id をフォールドIDとして流用
        fold_id = r.eval_id
        # cost_adj_sharpe = sharpe - cost_drag 近似
        cost_adj_sharpe = r.sharpe - abs(r.cost_drag) * 252
        # score = fitness に相当 (rank_ic ベース)
        score = r.rank_ic
        # regime_tag
        regime_tag = "normal"
        if r.crisis_period_sharpe < -0.5:
            regime_tag = "crisis"
        elif r.high_vol_sharpe < 0:
            regime_tag = "high_vol"

        meta = json.dumps({
            "horizon":                    r.horizon,
            "hit_rate":                   r.hit_rate,
            "r2_oos":                     r.r2_oos,
            "event_window_ic":            r.event_window_ic,
            "sortino":                    r.sortino,
            "calmar":                     r.calmar,
            "recovery_period":            r.recovery_period,
            "tail_ratio":                 r.tail_ratio,
            "cvar_5":                     r.cvar_5,
            "trade_count":                r.trade_count,
            "avg_hold_days":              r.avg_hold_days,
            "win_loss_ratio":             r.win_loss_ratio,
            "expectancy":                 r.expectancy,
            "risk_adjusted_return":       r.risk_adjusted_return,
            "var_5":                      r.var_5,
            "downside_vol":               r.downside_vol,
            "kelly_fraction":             r.kelly_fraction,
            "position_concentration":     r.position_concentration,
            "crisis_period_sharpe":       r.crisis_period_sharpe,
            "low_liquidity_sharpe":       r.low_liquidity_sharpe,
            "high_vol_sharpe":            r.high_vol_sharpe,
            "event_window_only_sharpe":   r.event_window_only_sharpe,
            "regime_consistency_score":   r.regime_consistency_score,
        })

        rows.append((
            r.eval_id,          # evaluation_id
            r.candidate_id,
            r.trace_id,
            fold_id,            # fold_id
            None,               # fold_start_at
            None,               # fold_end_at
            r.ic,
            r.rank_ic,
            r.ic_t_stat,
            r.hit_rate,
            r.r2_oos,
            r.sharpe,
            r.sortino,
            r.calmar,
            r.max_drawdown,
            r.turnover,
            r.cost_drag,
            cost_adj_sharpe,
            regime_tag,
            score,
            meta,
        ))

    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
