"""
frost_selector.py
-----------------
FROST スコア計算・hard gate 判定・採択判断モジュール。

1 候補ずつ評価して FrostEvaluation を生成し、
hard gate の合否と SELECTED/HOLD/REJECTED/REVIEW_REQUIRED を決定する。

設計原則:
  - 副作用なし
  - FrostConfig を参照して閾値・重みを取得
  - hard gate は soft score より先に評価 (gate FAIL → 即 REJECTED)
  - frost_score は gate pass 候補のみ意味を持つ
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from analytics.python.frost.frost_config import FrostConfig
from analytics.python.frost.frost_contracts import (
    FrostCandidate,
    FrostDecision,
    FrostEvaluation,
)
from analytics.python.frost.frost_features import extract_all_features
from analytics.python.frost.frost_metrics import (
    compute_frost_score,
    compute_predictive_score,
    compute_oos_sharpe_score,
    compute_regime_stability_score,
    compute_capacity_score,
    compute_pbo_penalty,
    compute_turnover_penalty,
    compute_complexity_penalty,
    compute_drawdown_penalty,
    compute_fragility_penalty,
)
from analytics.python.frost.frost_pbo import compute_pbo_all
from analytics.python.frost.frost_stability import compute_all_stability


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _safe(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


def _safe_opt(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Hard Gate 判定
# ---------------------------------------------------------------------------

GATE_NAMES = [
    "pbo",
    "rank_ic",
    "oos_sharpe",
    "turnover",
    "max_drawdown",
    "regime_pass_ratio",
    "complexity",
    "selection_stability",
]


def check_hard_gates(
    feat: Dict[str, Any],
    pbo_score: float,
    selection_consistency_score: float,
    config: FrostConfig,
) -> Tuple[bool, List[str]]:
    """
    Hard gate を評価する。

    1 つでも FAIL なら (False, [失敗ゲート名リスト]) を返す。
    全 PASS なら (True, []) を返す。

    Parameters
    ----------
    feat : dict
        extract_all_features() の戻り値
    pbo_score : float
        compute_pbo_all() の pbo_score
    selection_consistency_score : float
        compute_all_stability() の selection_consistency_score
    config : FrostConfig

    Returns
    -------
    tuple (hard_gate_passed: bool, gate_failures: list of str)
    """
    failures: List[str] = []

    # Gate 1: PBO
    if pbo_score > config.pbo_threshold:
        failures.append(
            f"pbo={pbo_score:.4f} > threshold={config.pbo_threshold:.4f}"
        )

    # Gate 2: Rank IC
    rank_ic = _safe_opt(feat.get("rank_ic"))
    if rank_ic is not None and abs(rank_ic) < config.min_rank_ic:
        failures.append(
            f"rank_ic={rank_ic:.4f} < min={config.min_rank_ic:.4f}"
        )
    elif rank_ic is None:
        # IC が取れない場合はスキップ (警告としては diagnostics_json に残す)
        pass

    # Gate 3: OOS Sharpe
    oos_sharpe = _safe_opt(feat.get("oos_sharpe"))
    if oos_sharpe is not None and oos_sharpe < config.min_oos_sharpe:
        failures.append(
            f"oos_sharpe={oos_sharpe:.4f} < min={config.min_oos_sharpe:.4f}"
        )

    # Gate 4: Turnover
    turnover = _safe_opt(feat.get("turnover"))
    if turnover is not None and turnover > config.max_turnover:
        failures.append(
            f"turnover={turnover:.2f} > max={config.max_turnover:.2f}"
        )

    # Gate 5: Max Drawdown
    oos_mdd = _safe_opt(feat.get("oos_max_drawdown"))
    if oos_mdd is not None:
        abs_mdd = abs(oos_mdd)
        if abs_mdd > config.max_drawdown:
            failures.append(
                f"max_drawdown={abs_mdd:.4f} > max={config.max_drawdown:.4f}"
            )

    # Gate 6: Regime pass ratio
    regime_pass = _safe_opt(feat.get("regime_pass_ratio_raw"))
    if regime_pass is not None and regime_pass < config.min_regime_pass_ratio:
        failures.append(
            f"regime_pass_ratio={regime_pass:.4f} < min={config.min_regime_pass_ratio:.4f}"
        )

    # Gate 7: Complexity
    complexity = _safe(feat.get("complexity_score", 0.0))
    if complexity > config.max_complexity_score:
        failures.append(
            f"complexity={complexity:.4f} > max={config.max_complexity_score:.4f}"
        )

    # Gate 8: Selection stability
    if selection_consistency_score < config.min_selection_stability:
        failures.append(
            f"selection_stability={selection_consistency_score:.4f} < min={config.min_selection_stability:.4f}"
        )

    return (len(failures) == 0, failures)


# ---------------------------------------------------------------------------
# 1 候補の評価
# ---------------------------------------------------------------------------

def evaluate_candidate(
    candidate: FrostCandidate,
    run_id: str,
    trace_id: str,
    config: FrostConfig,
) -> FrostEvaluation:
    """
    1 候補を評価して FrostEvaluation を返す。

    Parameters
    ----------
    candidate : FrostCandidate
    run_id : str
    trace_id : str
    config : FrostConfig

    Returns
    -------
    FrostEvaluation
    """
    # 特徴量抽出
    feat = extract_all_features(candidate)

    # PBO 計算
    pbo_result = compute_pbo_all(candidate.fold_results, config.min_backtest_folds)
    pbo_score_combined = pbo_result["pbo_score"]

    # 安定性計算
    fold_sharpes  = feat.get("fold_sharpes", [])
    fold_ics      = feat.get("fold_ics", [])
    fold_rank_ics = feat.get("fold_rank_ics", [])
    regime_sharpes = feat.get("regime_sharpes", [])

    stability = compute_all_stability(
        fold_sharpes, fold_ics, fold_rank_ics, regime_sharpes,
        config.min_backtest_folds,
    )
    selection_consistency_score = stability["selection_consistency_score"]
    fold_sharpe_std = stability["fold_sharpe_std"]

    # 個別スコア計算
    predictive_score = compute_predictive_score(feat)
    oos_sharpe_score = compute_oos_sharpe_score(feat, config.min_oos_sharpe)
    regime_stab_score = compute_regime_stability_score(feat)
    capacity_score    = compute_capacity_score(feat)

    # ペナルティ計算
    pbo_pen        = compute_pbo_penalty(feat, pbo_score_combined)
    turnover_pen   = compute_turnover_penalty(feat, config.max_turnover)
    complexity_pen = compute_complexity_penalty(_safe(feat.get("complexity_score", 0.0)))
    drawdown_pen   = compute_drawdown_penalty(feat, config.max_drawdown)
    fragility_pen  = compute_fragility_penalty(feat, fold_sharpe_std)

    # Hard Gate 判定
    hard_gate_passed, gate_failures = check_hard_gates(
        feat, pbo_score_combined, selection_consistency_score, config
    )

    # FROST スコア計算 (gate FAIL でも計算は行う; 判断は selector が行う)
    frost_score = compute_frost_score(
        predictive_score=predictive_score,
        oos_sharpe_score=oos_sharpe_score,
        regime_stability_score=regime_stab_score,
        selection_consistency_score=selection_consistency_score,
        capacity_score=capacity_score,
        pbo_score=pbo_pen,
        turnover_penalty=turnover_pen,
        complexity_penalty=complexity_pen,
        drawdown_penalty=drawdown_pen,
        fragility_penalty=fragility_pen,
        w_predictive=config.w_predictive,
        w_oos_sharpe=config.w_oos_sharpe,
        w_regime_stability=config.w_regime_stability,
        w_selection_consistency=config.w_selection_consistency,
        w_capacity=config.w_capacity,
        w_pbo_penalty=config.w_pbo_penalty,
        w_turnover_penalty=config.w_turnover_penalty,
        w_complexity_penalty=config.w_complexity_penalty,
        w_drawdown_penalty=config.w_drawdown_penalty,
        w_fragility_penalty=config.w_fragility_penalty,
    )

    # diagnostics 組み立て
    diagnostics = {
        "pbo_raw": pbo_result.get("pbo_raw", 0.0),
        "selection_fragility": pbo_result.get("selection_fragility", 0.0),
        "fold_sharpe_std": fold_sharpe_std,
        "fold_sharpe_mean": stability.get("fold_sharpe_mean", 0.0),
        "fold_ic_mean": stability.get("fold_ic_mean", 0.0),
        "n_folds": pbo_result.get("n_folds", 0),
        "gate_failures": gate_failures,
        "score_breakdown": {
            "predictive": predictive_score,
            "oos_sharpe": oos_sharpe_score,
            "regime_stability": regime_stab_score,
            "selection_consistency": selection_consistency_score,
            "capacity": capacity_score,
            "pbo_penalty": pbo_pen,
            "turnover_penalty": turnover_pen,
            "complexity_penalty": complexity_pen,
            "drawdown_penalty": drawdown_pen,
            "fragility_penalty": fragility_pen,
        },
    }

    return FrostEvaluation(
        run_id=run_id,
        candidate_id=candidate.candidate_id,
        trace_id=trace_id,
        # 予測力
        predictive_score=predictive_score,
        rank_ic=_safe_opt(feat.get("rank_ic")),
        ic=_safe_opt(feat.get("ic")),
        ic_t_stat=_safe_opt(feat.get("ic_t_stat")),
        hit_rate=_safe_opt(feat.get("hit_rate")),
        # OOS
        oos_sharpe=_safe_opt(feat.get("oos_sharpe")),
        oos_sortino=_safe_opt(feat.get("oos_sortino")),
        oos_calmar=_safe_opt(feat.get("oos_calmar")),
        oos_max_drawdown=_safe_opt(feat.get("oos_max_drawdown")),
        # レジーム
        regime_stability_score=regime_stab_score,
        regime_pass_ratio=_safe_opt(feat.get("regime_pass_ratio_raw")),
        crisis_sharpe=_safe_opt(feat.get("crisis_sharpe")),
        bull_sharpe=_safe_opt(feat.get("bull_sharpe")),
        # 選抜整合性
        selection_consistency_score=selection_consistency_score,
        top_k_stability=stability.get("top_k_stability"),
        sign_stability=stability.get("sign_stability"),
        # キャパシティ
        capacity_score=capacity_score,
        turnover=_safe_opt(feat.get("turnover")),
        avg_hold_days=_safe_opt(feat.get("avg_hold_days")),
        # リスク
        tail_risk_score=_safe_opt(feat.get("cvar_5")),
        var_5=_safe_opt(feat.get("var_5")),
        cvar_5=_safe_opt(feat.get("cvar_5")),
        downside_vol=_safe_opt(feat.get("downside_vol")),
        # ペナルティ
        pbo_score=pbo_pen,
        turnover_penalty=turnover_pen,
        complexity_penalty=complexity_pen,
        drawdown_penalty=drawdown_pen,
        fragility_penalty=fragility_pen,
        # 総合
        frost_score=frost_score,
        # JSON
        metrics_json=candidate.metrics,
        backtest_json=candidate.backtest_summary,
        regime_json=candidate.regime_breakdown,
        diagnostics_json=diagnostics,
        # Gate
        hard_gate_passed=hard_gate_passed,
        hard_gate_failures=gate_failures,
    )


# ---------------------------------------------------------------------------
# バッチ評価
# ---------------------------------------------------------------------------

def evaluate_candidates_batch(
    candidates: List[FrostCandidate],
    run_id: str,
    trace_id: str,
    config: FrostConfig,
) -> List[FrostEvaluation]:
    """
    候補リストを一括評価して FrostEvaluation のリストを返す。
    """
    return [
        evaluate_candidate(c, run_id, trace_id, config)
        for c in candidates
    ]


# ---------------------------------------------------------------------------
# 採択判断 (決定生成)
# ---------------------------------------------------------------------------

def make_decision(
    candidate: FrostCandidate,
    evaluation: FrostEvaluation,
    config: FrostConfig,
    rank: Optional[int] = None,
) -> FrostDecision:
    """
    FrostEvaluation から FrostDecision を生成する。

    判断ロジック:
    1. hard_gate_passed=False → REJECTED
    2. frost_score < 0 → REJECTED
    3. rank <= top_k → SELECTED (review_required は config に従う)
    4. それ以外 → HOLD

    Parameters
    ----------
    candidate : FrostCandidate
    evaluation : FrostEvaluation
    config : FrostConfig
    rank : int or None
        frost_score 順位 (1-indexed)。None の場合は HOLD 扱い。

    Returns
    -------
    FrostDecision
    """
    gate_failures   = evaluation.hard_gate_failures
    hard_gate_passed = evaluation.hard_gate_passed

    # Gate FAIL → REJECTED
    if not hard_gate_passed:
        reason = f"Hard gate failures: {'; '.join(gate_failures)}"
        return FrostDecision(
            run_id=candidate.run_id,
            candidate_id=candidate.candidate_id,
            trace_id=evaluation.trace_id,
            decision="REJECTED",
            decision_reason=reason,
            decision_rank=rank,
            frost_score=evaluation.frost_score,
            promotion_eligible=False,
            review_required=False,
            review_status="pending",
            rejection_reasons=[reason],
            gate_failures=gate_failures,
        )

    # Negative score → REJECTED
    if evaluation.frost_score < 0:
        reason = f"frost_score={evaluation.frost_score:.6f} < 0"
        return FrostDecision(
            run_id=candidate.run_id,
            candidate_id=candidate.candidate_id,
            trace_id=evaluation.trace_id,
            decision="REJECTED",
            decision_reason=reason,
            decision_rank=rank,
            frost_score=evaluation.frost_score,
            promotion_eligible=False,
            review_required=False,
            review_status="pending",
            rejection_reasons=[reason],
            gate_failures=[],
        )

    # top_k 圏内 → SELECTED
    if rank is not None and rank <= config.top_k:
        review_required = config.review_required_default
        promotion_eligible = (rank <= config.promotion_top_k)
        reason = f"top_k selection: rank={rank}, frost_score={evaluation.frost_score:.6f}"
        return FrostDecision(
            run_id=candidate.run_id,
            candidate_id=candidate.candidate_id,
            trace_id=evaluation.trace_id,
            decision="SELECTED",
            decision_reason=reason,
            decision_rank=rank,
            frost_score=evaluation.frost_score,
            promotion_eligible=promotion_eligible,
            review_required=review_required,
            review_status="pending",
            rejection_reasons=[],
            gate_failures=[],
        )

    # それ以外 → HOLD
    reason = f"Below top_k={config.top_k}: rank={rank}, frost_score={evaluation.frost_score:.6f}"
    return FrostDecision(
        run_id=candidate.run_id,
        candidate_id=candidate.candidate_id,
        trace_id=evaluation.trace_id,
        decision="HOLD",
        decision_reason=reason,
        decision_rank=rank,
        frost_score=evaluation.frost_score,
        promotion_eligible=False,
        review_required=False,
        review_status="pending",
        rejection_reasons=[],
        gate_failures=[],
    )
