"""
postgres_frost_writer.py
------------------------
FROST テーブル群への UPSERT IO 層。

対象テーブル:
  - frost_runs
  - frost_fitness_candidates
  - frost_evaluations
  - frost_selection_decisions

設計原則:
  - psycopg3 使用, %s プレースホルダー
  - 識別子は psycopg.sql.Identifier
  - JSONB は dict → json.dumps() / psycopg adapter
  - UPSERT: INSERT ... ON CONFLICT DO UPDATE — rerun-safe
  - NaN/Inf は _safe_float() / _safe_json() でサニタイズ
  - 引数は FrostConfig / FrostCandidate / FrostEvaluation / FrostDecision / FrostRunOutput
  - dry_run=True 時は frost_promotion_bridges / knowledge_artifacts には書かない
    （この writer は evaluation/decision まで書く; bridge は別モジュール）
"""
from __future__ import annotations

import json
import math
from typing import Any, List, Optional

import psycopg
from psycopg.sql import SQL, Identifier

from analytics.python.frost.frost_contracts import (
    FrostCandidate,
    FrostDecision,
    FrostEvaluation,
    FrostRunOutput,
)


# ---------------------------------------------------------------------------
# NaN/Inf サニタイズ ユーティリティ
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> float:
    """NaN / Inf を 0.0 に変換。PostgreSQL NUMERIC 列へ安全に渡す。"""
    try:
        f = float(v)
        return 0.0 if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return 0.0


def _safe_float_opt(v: Any) -> Optional[float]:
    """NaN / Inf を None に変換。NULL 許容列向け。"""
    if v is None:
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def _safe_json(obj: Any) -> str:
    """NaN/Inf を None に変換してから JSON シリアライズ。JSONB 列向け。"""
    def _sanitize(v: Any) -> Any:
        if isinstance(v, float):
            return None if (math.isnan(v) or math.isinf(v)) else v
        if isinstance(v, dict):
            return {k: _sanitize(vv) for k, vv in v.items()}
        if isinstance(v, list):
            return [_sanitize(x) for x in v]
        return v
    return json.dumps(_sanitize(obj), default=str)


# ---------------------------------------------------------------------------
# frost_runs UPSERT
# ---------------------------------------------------------------------------

def upsert_frost_run(
    conn: psycopg.Connection,
    output: FrostRunOutput,
) -> None:
    """
    frost_runs に UPSERT する。

    ON CONFLICT (run_id) → カウンター・status・ended_at を更新。
    """
    config_json = _safe_json(output.config_snapshot)
    status = "dry_run" if output.dry_run else output.status

    sql = SQL(
        "INSERT INTO {tbl} "
        "(run_id, trace_id, batch_label, engine_version, "
        " config_json, candidate_count, evaluated_count, "
        " selected_count, hold_count, rejected_count, promotion_count, "
        " status, dry_run, error_message, "
        " started_at, ended_at, created_at, updated_at) "
        "VALUES "
        "(%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        " %s, %s, now(), now()) "
        "ON CONFLICT (run_id) DO UPDATE SET "
        "  candidate_count  = EXCLUDED.candidate_count, "
        "  evaluated_count  = EXCLUDED.evaluated_count, "
        "  selected_count   = EXCLUDED.selected_count, "
        "  hold_count       = EXCLUDED.hold_count, "
        "  rejected_count   = EXCLUDED.rejected_count, "
        "  promotion_count  = EXCLUDED.promotion_count, "
        "  status           = EXCLUDED.status, "
        "  error_message    = EXCLUDED.error_message, "
        "  ended_at         = EXCLUDED.ended_at, "
        "  updated_at       = now()"
    ).format(tbl=Identifier("frost_runs"))

    conn.execute(sql, (
        output.run_id,
        output.trace_id,
        output.batch_label,
        output.engine_version,
        config_json,
        output.candidate_count,
        output.evaluated_count,
        output.selected_count,
        output.hold_count,
        output.rejected_count,
        output.promotion_count,
        status,
        output.dry_run,
        output.error_message,
        output.started_at,
        output.ended_at,
    ))
    conn.commit()


def update_frost_run_status(
    conn: psycopg.Connection,
    run_id: str,
    status: str,
    error_message: Optional[str] = None,
) -> None:
    """
    frost_runs のステータスのみ更新する（実行完了 / 失敗時）。
    """
    sql = SQL(
        "UPDATE {tbl} "
        "SET status=%s, error_message=%s, ended_at=now(), updated_at=now() "
        "WHERE run_id=%s"
    ).format(tbl=Identifier("frost_runs"))
    conn.execute(sql, (status, error_message, run_id))
    conn.commit()


# ---------------------------------------------------------------------------
# frost_fitness_candidates UPSERT
# ---------------------------------------------------------------------------

def upsert_frost_candidate(
    conn: psycopg.Connection,
    candidate: FrostCandidate,
) -> None:
    """
    frost_fitness_candidates に UPSERT する。

    UNIQUE(run_id, candidate_hash) — 同一 run_id で同じ hash の候補は上書き。
    """
    feature_json = _safe_json(candidate.feature_spec_json)

    sql = SQL(
        "INSERT INTO {tbl} "
        "(candidate_id, run_id, trace_id, source_type, source_candidate_id, "
        " formula_text, real_safe_formula_text, feature_spec_json, "
        " complexity_score, horizon, candidate_hash, status, "
        " created_at, updated_at) "
        "VALUES "
        "(%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, now(), now()) "
        "ON CONFLICT (run_id, candidate_hash) DO UPDATE SET "
        "  source_candidate_id    = EXCLUDED.source_candidate_id, "
        "  formula_text           = EXCLUDED.formula_text, "
        "  real_safe_formula_text = EXCLUDED.real_safe_formula_text, "
        "  feature_spec_json      = EXCLUDED.feature_spec_json, "
        "  complexity_score       = EXCLUDED.complexity_score, "
        "  status                 = EXCLUDED.status, "
        "  updated_at             = now()"
    ).format(tbl=Identifier("frost_fitness_candidates"))

    conn.execute(sql, (
        candidate.candidate_id,
        candidate.run_id,
        candidate.trace_id,
        candidate.source_type,
        candidate.source_candidate_id,
        candidate.formula_text,
        candidate.real_safe_formula_text,
        feature_json,
        _safe_float(candidate.complexity_score),
        candidate.horizon,
        candidate.candidate_hash,
        candidate.status,
    ))


def upsert_frost_candidates_batch(
    conn: psycopg.Connection,
    candidates: List[FrostCandidate],
) -> None:
    """
    frost_fitness_candidates に複数候補をバッチ UPSERT する。
    """
    for c in candidates:
        upsert_frost_candidate(conn, c)
    conn.commit()


def update_frost_candidate_status(
    conn: psycopg.Connection,
    candidate_id: str,
    status: str,
) -> None:
    """
    frost_fitness_candidates のステータスのみ更新する。
    status: pending / evaluated / selected / hold / rejected / review_required
    """
    sql = SQL(
        "UPDATE {tbl} SET status=%s, updated_at=now() WHERE candidate_id=%s"
    ).format(tbl=Identifier("frost_fitness_candidates"))
    conn.execute(sql, (status, candidate_id))


# ---------------------------------------------------------------------------
# frost_evaluations UPSERT
# ---------------------------------------------------------------------------

def upsert_frost_evaluation(
    conn: psycopg.Connection,
    ev: FrostEvaluation,
) -> None:
    """
    frost_evaluations に UPSERT する。

    UNIQUE(run_id, candidate_id) — 再実行時は全スコアを上書き。
    """
    metrics_json    = _safe_json(ev.metrics_json)
    backtest_json   = _safe_json(ev.backtest_json)
    regime_json     = _safe_json(ev.regime_json)
    diagnostics_json = _safe_json(ev.diagnostics_json)
    gate_failures_json = _safe_json(ev.hard_gate_failures)

    sql = SQL(
        "INSERT INTO {tbl} "
        "(evaluation_id, run_id, candidate_id, trace_id, "
        " predictive_score, rank_ic, ic, ic_t_stat, hit_rate, "
        " oos_sharpe, oos_sortino, oos_calmar, oos_max_drawdown, "
        " regime_stability_score, regime_pass_ratio, crisis_sharpe, bull_sharpe, "
        " selection_consistency_score, top_k_stability, sign_stability, "
        " capacity_score, turnover, avg_hold_days, "
        " tail_risk_score, var_5, cvar_5, downside_vol, "
        " pbo_score, turnover_penalty, complexity_penalty, drawdown_penalty, fragility_penalty, "
        " frost_score, "
        " metrics_json, backtest_json, regime_json, diagnostics_json, "
        " hard_gate_passed, hard_gate_failures, "
        " created_at, updated_at) "
        "VALUES "
        "(%s, %s, %s, %s, "
        " %s, %s, %s, %s, %s, "
        " %s, %s, %s, %s, "
        " %s, %s, %s, %s, "
        " %s, %s, %s, "
        " %s, %s, %s, "
        " %s, %s, %s, %s, "
        " %s, %s, %s, %s, %s, "
        " %s, "
        " %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, "
        " %s, %s::jsonb, "
        " now(), now()) "
        "ON CONFLICT (run_id, candidate_id) DO UPDATE SET "
        "  predictive_score              = EXCLUDED.predictive_score, "
        "  rank_ic                       = EXCLUDED.rank_ic, "
        "  ic                            = EXCLUDED.ic, "
        "  ic_t_stat                     = EXCLUDED.ic_t_stat, "
        "  hit_rate                      = EXCLUDED.hit_rate, "
        "  oos_sharpe                    = EXCLUDED.oos_sharpe, "
        "  oos_sortino                   = EXCLUDED.oos_sortino, "
        "  oos_calmar                    = EXCLUDED.oos_calmar, "
        "  oos_max_drawdown              = EXCLUDED.oos_max_drawdown, "
        "  regime_stability_score        = EXCLUDED.regime_stability_score, "
        "  regime_pass_ratio             = EXCLUDED.regime_pass_ratio, "
        "  crisis_sharpe                 = EXCLUDED.crisis_sharpe, "
        "  bull_sharpe                   = EXCLUDED.bull_sharpe, "
        "  selection_consistency_score   = EXCLUDED.selection_consistency_score, "
        "  top_k_stability               = EXCLUDED.top_k_stability, "
        "  sign_stability                = EXCLUDED.sign_stability, "
        "  capacity_score                = EXCLUDED.capacity_score, "
        "  turnover                      = EXCLUDED.turnover, "
        "  avg_hold_days                 = EXCLUDED.avg_hold_days, "
        "  tail_risk_score               = EXCLUDED.tail_risk_score, "
        "  var_5                         = EXCLUDED.var_5, "
        "  cvar_5                        = EXCLUDED.cvar_5, "
        "  downside_vol                  = EXCLUDED.downside_vol, "
        "  pbo_score                     = EXCLUDED.pbo_score, "
        "  turnover_penalty              = EXCLUDED.turnover_penalty, "
        "  complexity_penalty            = EXCLUDED.complexity_penalty, "
        "  drawdown_penalty              = EXCLUDED.drawdown_penalty, "
        "  fragility_penalty             = EXCLUDED.fragility_penalty, "
        "  frost_score                   = EXCLUDED.frost_score, "
        "  metrics_json                  = EXCLUDED.metrics_json, "
        "  backtest_json                 = EXCLUDED.backtest_json, "
        "  regime_json                   = EXCLUDED.regime_json, "
        "  diagnostics_json              = EXCLUDED.diagnostics_json, "
        "  hard_gate_passed              = EXCLUDED.hard_gate_passed, "
        "  hard_gate_failures            = EXCLUDED.hard_gate_failures, "
        "  updated_at                    = now()"
    ).format(tbl=Identifier("frost_evaluations"))

    conn.execute(sql, (
        ev.evaluation_id,
        ev.run_id,
        ev.candidate_id,
        ev.trace_id,
        # 予測力
        _safe_float(ev.predictive_score),
        _safe_float_opt(ev.rank_ic),
        _safe_float_opt(ev.ic),
        _safe_float_opt(ev.ic_t_stat),
        _safe_float_opt(ev.hit_rate),
        # OOS
        _safe_float_opt(ev.oos_sharpe),
        _safe_float_opt(ev.oos_sortino),
        _safe_float_opt(ev.oos_calmar),
        _safe_float_opt(ev.oos_max_drawdown),
        # レジーム
        _safe_float_opt(ev.regime_stability_score),
        _safe_float_opt(ev.regime_pass_ratio),
        _safe_float_opt(ev.crisis_sharpe),
        _safe_float_opt(ev.bull_sharpe),
        # 選抜整合性
        _safe_float_opt(ev.selection_consistency_score),
        _safe_float_opt(ev.top_k_stability),
        _safe_float_opt(ev.sign_stability),
        # キャパシティ
        _safe_float_opt(ev.capacity_score),
        _safe_float_opt(ev.turnover),
        _safe_float_opt(ev.avg_hold_days),
        # リスク
        _safe_float_opt(ev.tail_risk_score),
        _safe_float_opt(ev.var_5),
        _safe_float_opt(ev.cvar_5),
        _safe_float_opt(ev.downside_vol),
        # ペナルティ
        _safe_float(ev.pbo_score),
        _safe_float(ev.turnover_penalty),
        _safe_float(ev.complexity_penalty),
        _safe_float(ev.drawdown_penalty),
        _safe_float(ev.fragility_penalty),
        # 総合スコア
        _safe_float(ev.frost_score),
        # JSON
        metrics_json,
        backtest_json,
        regime_json,
        diagnostics_json,
        # Gate
        ev.hard_gate_passed,
        gate_failures_json,
    ))


def upsert_frost_evaluations_batch(
    conn: psycopg.Connection,
    evaluations: List[FrostEvaluation],
) -> None:
    """
    frost_evaluations に複数評価をバッチ UPSERT する。
    """
    for ev in evaluations:
        upsert_frost_evaluation(conn, ev)
    conn.commit()


# ---------------------------------------------------------------------------
# frost_selection_decisions UPSERT
# ---------------------------------------------------------------------------

def upsert_frost_decision(
    conn: psycopg.Connection,
    decision: FrostDecision,
) -> None:
    """
    frost_selection_decisions に UPSERT する。

    UNIQUE(run_id, candidate_id) — 再実行時は判断を上書き。
    """
    rejection_json = _safe_json(decision.rejection_reasons)
    gate_json      = _safe_json(decision.gate_failures)

    sql = SQL(
        "INSERT INTO {tbl} "
        "(decision_id, run_id, candidate_id, trace_id, "
        " decision, decision_reason, decision_rank, "
        " frost_score, promotion_eligible, review_required, review_status, "
        " reviewed_at, reviewed_by, "
        " rejection_reasons, gate_failures, "
        " near_duplicate_of, suppressed_by_dedup, "
        " created_at, updated_at) "
        "VALUES "
        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        " %s::jsonb, %s::jsonb, %s, %s, now(), now()) "
        "ON CONFLICT (run_id, candidate_id) DO UPDATE SET "
        "  decision             = EXCLUDED.decision, "
        "  decision_reason      = EXCLUDED.decision_reason, "
        "  decision_rank        = EXCLUDED.decision_rank, "
        "  frost_score          = EXCLUDED.frost_score, "
        "  promotion_eligible   = EXCLUDED.promotion_eligible, "
        "  review_required      = EXCLUDED.review_required, "
        "  review_status        = EXCLUDED.review_status, "
        "  reviewed_at          = EXCLUDED.reviewed_at, "
        "  reviewed_by          = EXCLUDED.reviewed_by, "
        "  rejection_reasons    = EXCLUDED.rejection_reasons, "
        "  gate_failures        = EXCLUDED.gate_failures, "
        "  near_duplicate_of    = EXCLUDED.near_duplicate_of, "
        "  suppressed_by_dedup  = EXCLUDED.suppressed_by_dedup, "
        "  updated_at           = now()"
    ).format(tbl=Identifier("frost_selection_decisions"))

    conn.execute(sql, (
        decision.decision_id,
        decision.run_id,
        decision.candidate_id,
        decision.trace_id,
        decision.decision,
        decision.decision_reason,
        decision.decision_rank,
        _safe_float(decision.frost_score),
        decision.promotion_eligible,
        decision.review_required,
        decision.review_status,
        decision.reviewed_at,
        decision.reviewed_by,
        rejection_json,
        gate_json,
        decision.near_duplicate_of,
        decision.suppressed_by_dedup,
    ))


def upsert_frost_decisions_batch(
    conn: psycopg.Connection,
    decisions: List[FrostDecision],
) -> None:
    """
    frost_selection_decisions に複数決定をバッチ UPSERT する。
    """
    for d in decisions:
        upsert_frost_decision(conn, d)
    conn.commit()


# ---------------------------------------------------------------------------
# FrostRunOutput の一括書き込み
# ---------------------------------------------------------------------------

def write_frost_run_output(
    conn: psycopg.Connection,
    output: FrostRunOutput,
) -> None:
    """
    FrostRunOutput を PostgreSQL の 4 テーブルに一括書き込みする。

    書き込み順:
      1. frost_runs (実行レコード)
      2. frost_fitness_candidates (候補)
      3. frost_evaluations (評価)
      4. frost_selection_decisions (判断)

    注意:
      - dry_run=True でもこの writer は評価・判断まで書く。
        frost_promotion_bridges への書き込みは postgres_frost_promotion_bridge.py が担当し、
        そちらで dry_run ガードを行う。
      - 各テーブルは独立した UPSERT のため、部分失敗後の再実行も安全。
    """
    # 1. 実行レコード
    upsert_frost_run(conn, output)

    # 2. 候補
    if output.candidates:
        upsert_frost_candidates_batch(conn, output.candidates)

    # 3. 評価
    if output.evaluations:
        upsert_frost_evaluations_batch(conn, output.evaluations)

    # 4. 判断 (candidate status も更新)
    if output.decisions:
        upsert_frost_decisions_batch(conn, output.decisions)
        _sync_candidate_statuses(conn, output.decisions)

    # 最終 run status 更新
    update_frost_run_status(conn, output.run_id, output.status, output.error_message)


def _sync_candidate_statuses(
    conn: psycopg.Connection,
    decisions: List[FrostDecision],
) -> None:
    """
    frost_selection_decisions の decision に合わせて
    frost_fitness_candidates.status を同期更新する。
    """
    DECISION_TO_STATUS = {
        "SELECTED":        "selected",
        "HOLD":            "hold",
        "REJECTED":        "rejected",
        "REVIEW_REQUIRED": "review_required",
    }
    for d in decisions:
        status = DECISION_TO_STATUS.get(d.decision)
        if status:
            update_frost_candidate_status(conn, d.candidate_id, status)
    conn.commit()
