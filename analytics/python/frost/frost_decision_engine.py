"""
frost_decision_engine.py
------------------------
FROST 最終ポリシーエンジン。

frost_ranker.py から受け取った FrostDecision リストに対して:
1. top-k 選抜数の確定
2. portfolio overlap 制御
3. review_required フラグの最終調整
4. REVIEW_REQUIRED への昇格 (境界付近候補)
5. 採択統計の集計

設計原則:
  - 副作用なし
  - FrostConfig を参照
  - frost_runner.py から呼ばれる最後のフィルタ層
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from analytics.python.frost.frost_config import FrostConfig
from analytics.python.frost.frost_contracts import FrostDecision, FrostEvaluation


# ---------------------------------------------------------------------------
# 境界付近候補の REVIEW_REQUIRED 昇格
# ---------------------------------------------------------------------------

def promote_borderline_to_review(
    decisions: List[FrostDecision],
    evaluations_by_cid: Dict[str, FrostEvaluation],
    config: FrostConfig,
    borderline_margin: float = 0.05,
) -> List[FrostDecision]:
    """
    top_k 付近 (rank = top_k ± margin) の候補を REVIEW_REQUIRED に昇格する。

    HOLD 状態で frost_score が選抜下限に近い候補は
    人間レビューが必要と判断する。

    Parameters
    ----------
    decisions : list of FrostDecision
    evaluations_by_cid : dict
    config : FrostConfig
    borderline_margin : float
        SELECTED 最下位スコアとの差がこれ以内なら REVIEW_REQUIRED

    Returns
    -------
    list of FrostDecision (modified in-place)
    """
    # SELECTED の最小 frost_score を取得
    selected_scores = [
        d.frost_score for d in decisions
        if d.decision == "SELECTED"
    ]
    if not selected_scores:
        return decisions

    min_selected_score = min(selected_scores)
    threshold = min_selected_score - borderline_margin

    for d in decisions:
        if d.decision == "HOLD" and not d.suppressed_by_dedup:
            if d.frost_score >= threshold:
                d.decision = "REVIEW_REQUIRED"
                d.review_required = True
                d.decision_reason = (
                    f"Borderline candidate: frost_score={d.frost_score:.6f} "
                    f"near min_selected={min_selected_score:.6f} "
                    f"(margin={borderline_margin})"
                )

    return decisions


# ---------------------------------------------------------------------------
# 選抜数の最終確認・調整
# ---------------------------------------------------------------------------

def enforce_top_k_limit(
    decisions: List[FrostDecision],
    config: FrostConfig,
) -> List[FrostDecision]:
    """
    SELECTED 数が top_k を超えていれば余剰を HOLD に戻す。

    rank の大きいものから順に HOLD に変更する。

    Returns
    -------
    list of FrostDecision
    """
    selected = [d for d in decisions if d.decision == "SELECTED"]
    if len(selected) <= config.top_k:
        return decisions

    # rank 降順でソート (rank が None の場合は最後)
    selected_sorted = sorted(
        selected,
        key=lambda d: (d.decision_rank is None, d.decision_rank or 999999),
        reverse=True,
    )

    # 超過分を HOLD に
    excess = len(selected) - config.top_k
    for d in selected_sorted[:excess]:
        d.decision = "HOLD"
        d.promotion_eligible = False
        d.decision_reason = (
            f"Downgraded to HOLD: exceeds top_k={config.top_k}, "
            f"rank={d.decision_rank}"
        )

    return decisions


# ---------------------------------------------------------------------------
# Promotion eligible の最終確認
# ---------------------------------------------------------------------------

def enforce_promotion_top_k(
    decisions: List[FrostDecision],
    config: FrostConfig,
) -> List[FrostDecision]:
    """
    promotion_eligible=True の数が promotion_top_k を超えないように制限する。

    review_required=True の候補は promotion_eligible=False に強制する。

    Returns
    -------
    list of FrostDecision
    """
    for d in decisions:
        if d.review_required and d.review_status not in ("approved",):
            d.promotion_eligible = False

    eligible = [d for d in decisions if d.promotion_eligible]
    if len(eligible) <= config.promotion_top_k:
        return decisions

    # rank 降順でソート
    eligible_sorted = sorted(
        eligible,
        key=lambda d: (d.decision_rank is None, d.decision_rank or 999999),
        reverse=True,
    )
    excess = len(eligible) - config.promotion_top_k
    for d in eligible_sorted[:excess]:
        d.promotion_eligible = False
        d.decision_reason += f" [promotion_eligible demoted: exceeds promotion_top_k={config.promotion_top_k}]"

    return decisions


# ---------------------------------------------------------------------------
# 採択統計の集計
# ---------------------------------------------------------------------------

def summarize_decisions(
    decisions: List[FrostDecision],
) -> Dict[str, int]:
    """
    決定統計をカウントして辞書として返す。

    Returns
    -------
    dict
        selected_count, hold_count, rejected_count, review_required_count,
        promotion_eligible_count, dedup_suppressed_count
    """
    return {
        "selected_count":          sum(1 for d in decisions if d.decision == "SELECTED"),
        "hold_count":              sum(1 for d in decisions if d.decision == "HOLD"),
        "rejected_count":          sum(1 for d in decisions if d.decision == "REJECTED"),
        "review_required_count":   sum(1 for d in decisions if d.decision == "REVIEW_REQUIRED"),
        "promotion_eligible_count": sum(1 for d in decisions if d.promotion_eligible),
        "dedup_suppressed_count":  sum(1 for d in decisions if d.suppressed_by_dedup),
    }


# ---------------------------------------------------------------------------
# 最終ポリシー適用 (メインエントリポイント)
# ---------------------------------------------------------------------------

def apply_final_policy(
    decisions: List[FrostDecision],
    evaluations: List[FrostEvaluation],
    config: FrostConfig,
    promote_borderline: bool = True,
    borderline_margin: float = 0.05,
) -> Tuple[List[FrostDecision], Dict[str, int]]:
    """
    全決定に対して最終ポリシーを適用する。

    処理順序:
    1. top_k 超過確認
    2. borderline REVIEW_REQUIRED 昇格
    3. promotion_top_k 超過確認
    4. 統計集計

    Parameters
    ----------
    decisions : list of FrostDecision
    evaluations : list of FrostEvaluation
    config : FrostConfig
    promote_borderline : bool
        True の場合は境界付近候補を REVIEW_REQUIRED に昇格
    borderline_margin : float
        REVIEW_REQUIRED 昇格のスコア margin

    Returns
    -------
    tuple (decisions, summary_stats)
    """
    eval_by_cid = {ev.candidate_id: ev for ev in evaluations}

    # Step 1: top_k 超過確認
    decisions = enforce_top_k_limit(decisions, config)

    # Step 2: borderline → REVIEW_REQUIRED
    if promote_borderline:
        decisions = promote_borderline_to_review(
            decisions, eval_by_cid, config, borderline_margin
        )

    # Step 3: promotion_top_k 確認
    decisions = enforce_promotion_top_k(decisions, config)

    # Step 4: 統計
    stats = summarize_decisions(decisions)

    return decisions, stats
