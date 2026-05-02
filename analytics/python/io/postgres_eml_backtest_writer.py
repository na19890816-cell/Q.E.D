"""
postgres_eml_backtest_writer.py
--------------------------------
バックテスト結果 (eml_backtest_runs, eml_backtest_folds)
を PostgreSQL へ UPSERT する IO 層。

実際のスキーマに準拠:
  eml_backtest_runs: backtest_run_id, candidate_id, trace_id, mode,
    universe_tag, horizon_days, cost_bps, slippage_bps, gap_open_bps,
    regime_tag_set, status, total_folds, summary_json,
    started_at, finished_at, created_at, updated_at

  eml_backtest_folds: fold_id, backtest_run_id, candidate_id, trace_id,
    fold_index, fold_start_at, fold_end_at, regime_tag,
    total_trades, sharpe, sortino, max_drawdown, total_return,
    turnover, cost_drag, win_rate, metrics_json, regime_breakdown_json,
    status, created_at
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, List

import psycopg
from psycopg.sql import SQL, Identifier

from analytics.python.backtest.harness import BacktestRunResult, FoldResult


# ------------------------------------------------------------------ #
# NaN/Inf サニタイズ
# ------------------------------------------------------------------ #

def _sf(v: Any) -> Any:
    """NaN / Inf を None に変換 (PostgreSQL numeric 互換)。"""
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_json(obj: Any) -> str:
    def _s(v: Any) -> Any:
        if isinstance(v, float):
            return None if (math.isnan(v) or math.isinf(v)) else v
        if isinstance(v, dict):
            return {k: _s(vv) for k, vv in v.items()}
        if isinstance(v, list):
            return [_s(x) for x in v]
        return v
    return json.dumps(_s(obj))


# ------------------------------------------------------------------ #
# eml_backtest_runs UPSERT
# ------------------------------------------------------------------ #

def upsert_backtest_run(
    conn: psycopg.Connection,
    result: BacktestRunResult,
    candidate_id: str,
    status: str = "completed",
) -> None:
    sql = SQL(
        "INSERT INTO {tbl} "
        "(backtest_run_id, candidate_id, trace_id, mode, "
        " universe_tag, horizon_days, cost_bps, slippage_bps, gap_open_bps, "
        " regime_tag_set, status, total_folds, summary_json, "
        " started_at, finished_at, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, "
        " now(), now(), now(), now()) "
        "ON CONFLICT (backtest_run_id) DO UPDATE SET "
        "  status       = EXCLUDED.status, "
        "  total_folds  = EXCLUDED.total_folds, "
        "  summary_json = EXCLUDED.summary_json, "
        "  finished_at  = EXCLUDED.finished_at, "
        "  updated_at   = now()"
    ).format(tbl=Identifier("eml_backtest_runs"))

    cost_bps     = float(os.environ.get("EML_BACKTEST_COST_BPS", "2.0"))
    slippage_bps = float(os.environ.get("EML_BACKTEST_SLIPPAGE_BPS", "2.0"))

    summary = {
        "overall_sharpe":       _sf(result.overall_sharpe),
        "overall_max_drawdown": _sf(result.overall_max_drawdown),
        "gate_trigger_count":   result.gate_trigger_count,
        "mode":                 result.mode,
        **result.metadata,
    }

    with conn.cursor() as cur:
        cur.execute(sql, (
            result.backtest_run_id,
            candidate_id,
            result.trace_id,
            result.mode,
            "event_study_v1",   # universe_tag
            5,                   # horizon_days
            cost_bps,
            slippage_bps,
            3.0,                 # gap_open_bps
            [],                  # regime_tag_set
            status,
            result.total_folds,
            _safe_json(summary),
        ))
    conn.commit()


# ------------------------------------------------------------------ #
# eml_backtest_folds UPSERT
# ------------------------------------------------------------------ #

def upsert_backtest_folds(
    conn: psycopg.Connection,
    folds: List[FoldResult],
    candidate_id: str = "",
    trace_id: str = "",
) -> None:
    sql = SQL(
        "INSERT INTO {tbl} "
        "(fold_id, backtest_run_id, candidate_id, trace_id, "
        " fold_index, fold_start_at, fold_end_at, "
        " regime_tag, total_trades, sharpe, sortino, max_drawdown, "
        " total_return, turnover, cost_drag, win_rate, "
        " metrics_json, regime_breakdown_json, status, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        " %s::jsonb, %s::jsonb, %s, now()) "
        "ON CONFLICT (fold_id) DO UPDATE SET "
        "  sharpe       = EXCLUDED.sharpe, "
        "  max_drawdown = EXCLUDED.max_drawdown, "
        "  status       = EXCLUDED.status, "
        "  metrics_json = EXCLUDED.metrics_json"
    ).format(tbl=Identifier("eml_backtest_folds"))

    rows = []
    for f in folds:
        # 日付変換
        def _date(s: str) -> Any:
            if not s:
                return None
            try:
                from datetime import date
                # "2020-01-01" か整数インデックスを date に変換
                return date.fromisoformat(str(s)[:10])
            except Exception:
                return None

        # 純リターン合計
        net_ret = f.net_returns.dropna()
        total_return = float(net_ret.sum()) if len(net_ret) > 0 else 0.0
        # ウィン率
        win_rate = float((net_ret > 0).mean()) if len(net_ret) > 0 else 0.0

        # regime tag 推定
        regime_tag = "crisis" if f.status == "gate_fired" else "normal"

        metrics = {
            "turnover":    _sf(f.turnover),
            "sharpe":      _sf(f.sharpe),
            "max_drawdown":_sf(f.max_drawdown),
            "total_return":_sf(total_return),
            "total_bars":  f.total_bars,
            "gate_triggers": [
                {"bar_idx": gt.bar_idx, "trigger_type": gt.trigger_type}
                for gt in f.gate_triggers
            ],
        }

        fold_cid = candidate_id
        fold_tid = trace_id

        rows.append((
            f.fold_id,
            f.backtest_run_id,
            fold_cid,
            fold_tid,
            f.fold_index,
            _date(f.test_start) or _date("2020-01-01"),
            _date(f.test_end)   or _date("2020-12-31"),
            regime_tag,
            len(f.gate_triggers),   # total_trades (proxy)
            _sf(f.sharpe),
            None,                   # sortino (未計算)
            _sf(f.max_drawdown),
            _sf(total_return),
            _sf(f.turnover),
            None,                   # cost_drag
            _sf(win_rate),
            _safe_json(metrics),
            "{}",                   # regime_breakdown_json
            f.status,
        ))

    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
