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

Phase 2 変更:
  - evaluate_candidate() の内部を evaluate_candidate_to_bundle() +
    evaluation_from_bundle() に委譲 (D5 負債解消)
  - 外部 API / 戻り値型は変更なし (完全後方互換)
  - check_hard_gates() は後方互換のため残すが内部は GateVerdict を利用

Phase 3 変更:
  - check_hard_gates() の内部を GateEngine.evaluate_from_dict() に委譲 (D2 負債解消)
  - GateEngine が v1/v2 全ゲートの単一責任クラスとなる
  - check_hard_gates() のシグネチャ・戻り値型は変更なし (完全後方互換)
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from analytics.python.frost.evidence_bundle import (
    evaluate_candidate_to_bundle,
    evaluation_from_bundle,
)
from analytics.python.frost.gate_engine import GateEngine
from analytics.python.frost.frost_config import FrostConfig
from analytics.python.frost.frost_contracts import (
    FrostCandidate,
    FrostDecision,
    FrostEvaluation,
)


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

    Phase 3: 内部を GateEngine.evaluate_from_dict() に委譲 (D2 負債解消)。
    シグネチャ・戻り値型は変更なし (完全後方互換)。

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
    return GateEngine.from_config(config).evaluate_from_dict(
        feat, pbo_score, selection_consistency_score
    )


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

    Phase 2: 内部を evaluate_candidate_to_bundle() + evaluation_from_bundle()
    に委譲して D5 (stringly-typed 結合) 負債を解消。
    外部 API は変更なし (完全後方互換)。

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
    bundle = evaluate_candidate_to_bundle(candidate, run_id, trace_id, config)
    return evaluation_from_bundle(
        bundle,
        use_v2_score=getattr(config, "use_v2_score", False),
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
