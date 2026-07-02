"""
frost_ranker.py
---------------
FROST ランキング・多様化集約モジュール。

複数候補の FrostEvaluation を受け取り:
1. frost_score 降順でランキングを付ける
2. near-duplicate を検出して抑制する  ← Phase 4: DedupStage に移管
3. diversification-aware な top-k 選抜を行う
4. 同一 source family の過集中を制限する

設計原則:
  - 副作用なし
  - FrostConfig を参照
  - FrostDecision.decision_rank を更新して返す

Phase 4 変更:
  detect_near_duplicates() → DedupStage.detect_structural() に委譲
  select_diverse_top_k()   → detect_near_duplicates() 呼び出しを委譲経由に統一
  _hash_similarity() / _formula_similarity() は DedupStage 内部に移管 (ここでは削除)
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Set, Tuple

from analytics.python.frost.frost_config import FrostConfig
from analytics.python.frost.frost_contracts import (
    FrostCandidate,
    FrostDecision,
    FrostEvaluation,
)
from analytics.python.frost.frost_selector import make_decision


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _safe(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 候補スコアリスト生成 (ランキング前ソート)
# ---------------------------------------------------------------------------

def rank_evaluations(
    evaluations: List[FrostEvaluation],
) -> List[Tuple[int, FrostEvaluation]]:
    """
    frost_score 降順でランク付けする。

    hard_gate_passed=False の候補は後ろに置く。

    Returns
    -------
    list of (rank: int, evaluation: FrostEvaluation)
        rank は 1-indexed
    """
    # Gate PASS を先に、FAIL を後ろに
    passed = [ev for ev in evaluations if ev.hard_gate_passed]
    failed = [ev for ev in evaluations if not ev.hard_gate_passed]

    passed_sorted = sorted(passed, key=lambda ev: ev.frost_score, reverse=True)
    failed_sorted = sorted(failed, key=lambda ev: ev.frost_score, reverse=True)

    ranked = []
    for i, ev in enumerate(passed_sorted + failed_sorted, start=1):
        ranked.append((i, ev))
    return ranked


# ---------------------------------------------------------------------------
# Near-duplicate 検出  (Phase 4: DedupStage に委譲)
# ---------------------------------------------------------------------------

def detect_near_duplicates(
    candidates: List[FrostCandidate],
    threshold: float = 0.95,
) -> Dict[str, Optional[str]]:
    """
    候補間の near-duplicate を検出する。

    Phase 4: 内部を DedupStage.detect_structural() に委譲 (D4 負債解消)。

    Returns
    -------
    dict: {suppressed_candidate_id: dominant_candidate_id or None}
        抑制対象の candidate_id → 支配候補の candidate_id
        重複なしの候補はキーに含まない
    """
    from analytics.python.frost.dedup_stage import DedupStage
    result = DedupStage(structural_threshold=threshold).detect_structural(
        candidates, threshold=threshold
    )
    return result.suppressed


# ---------------------------------------------------------------------------
# Source family 過集中チェック
# ---------------------------------------------------------------------------

def check_family_limit(
    candidate: FrostCandidate,
    family_counts: Dict[str, int],
    max_same_family: int,
) -> bool:
    """
    同一 source_candidate family の採択数が上限に達しているか確認する。

    source_candidate_id の先頭 8 文字をファミリーキーとして使用。
    (EML の場合: 同じ run から来た候補が同じファミリー)

    Returns
    -------
    bool: True = 上限超過 (採択不可)
    """
    if not candidate.source_candidate_id:
        return False

    # ファミリーキー: source_candidate_id の先頭部分 (または run_id 的な prefix)
    family_key = candidate.source_candidate_id[:8] if len(candidate.source_candidate_id) >= 8 else candidate.source_candidate_id
    return family_counts.get(family_key, 0) >= max_same_family


# ---------------------------------------------------------------------------
# 多様化を考慮した top-k 選抜
# ---------------------------------------------------------------------------

def select_diverse_top_k(
    ranked: List[Tuple[int, FrostEvaluation]],
    candidates_by_id: Dict[str, FrostCandidate],
    config: FrostConfig,
) -> Tuple[List[Tuple[int, FrostEvaluation]], Set[str]]:
    """
    near-duplicate 抑制 + family 制限を適用した top-k 選抜を行う。

    Parameters
    ----------
    ranked : list of (rank, evaluation)
        rank_evaluations() の出力
    candidates_by_id : dict
        candidate_id → FrostCandidate
    config : FrostConfig

    Returns
    -------
    tuple (selected_ranked, suppressed_ids)
        selected_ranked: 採択された (rank, evaluation) のリスト
        suppressed_ids: near-duplicate で抑制された candidate_id の集合
    """
    all_candidates = list(candidates_by_id.values())
    near_dup_map = detect_near_duplicates(all_candidates, config.near_duplicate_threshold)
    suppressed_ids: Set[str] = set(near_dup_map.keys())

    selected: List[Tuple[int, FrostEvaluation]] = []
    family_counts: Dict[str, int] = {}

    for rank, ev in ranked:
        cid = ev.candidate_id

        # Gate FAIL はスキップ
        if not ev.hard_gate_passed:
            continue

        # near-duplicate 抑制
        if cid in suppressed_ids:
            continue

        # top_k 上限
        if len(selected) >= config.top_k:
            break

        # family 上限チェック
        c = candidates_by_id.get(cid)
        if c and check_family_limit(c, family_counts, config.max_same_family):
            continue

        # 採択
        selected.append((rank, ev))

        # family カウント更新
        if c and c.source_candidate_id:
            fk = c.source_candidate_id[:8] if len(c.source_candidate_id) >= 8 else c.source_candidate_id
            family_counts[fk] = family_counts.get(fk, 0) + 1

    return selected, suppressed_ids


# ---------------------------------------------------------------------------
# 全候補への決定付与 (メインエントリポイント)
# ---------------------------------------------------------------------------

def assign_decisions(
    candidates: List[FrostCandidate],
    evaluations: List[FrostEvaluation],
    config: FrostConfig,
) -> List[FrostDecision]:
    """
    全候補に対して FrostDecision を生成する。

    処理フロー:
    1. frost_score でランキング
    2. near-duplicate 検出
    3. family 制限
    4. top-k 選抜
    5. 全候補に decision 付与

    Parameters
    ----------
    candidates : list of FrostCandidate
    evaluations : list of FrostEvaluation
    config : FrostConfig

    Returns
    -------
    list of FrostDecision
    """
    # 評価を candidate_id でインデックス化
    eval_by_cid: Dict[str, FrostEvaluation] = {ev.candidate_id: ev for ev in evaluations}
    cand_by_cid: Dict[str, FrostCandidate]  = {c.candidate_id: c for c in candidates}

    # ランキング
    ranked = rank_evaluations(evaluations)
    rank_by_cid = {ev.candidate_id: rank for rank, ev in ranked}

    # Near-duplicate 抑制
    near_dup_map = detect_near_duplicates(candidates, config.near_duplicate_threshold)
    suppressed_ids: Set[str] = set(near_dup_map.keys())

    # top-k 多様化選抜
    selected_ranked, _ = select_diverse_top_k(ranked, cand_by_cid, config)
    selected_ids: Set[str] = {ev.candidate_id for _, ev in selected_ranked}

    decisions: List[FrostDecision] = []
    # 新しいランク (多様化選抜後の 1-indexed)
    new_rank = {ev.candidate_id: i + 1 for i, (_, ev) in enumerate(selected_ranked)}

    for c in candidates:
        cid = c.candidate_id
        ev  = eval_by_cid.get(cid)
        if ev is None:
            # 評価なし → HOLD
            d = FrostDecision(
                run_id=c.run_id,
                candidate_id=cid,
                trace_id=c.trace_id,
                decision="HOLD",
                decision_reason="No evaluation result",
                promotion_eligible=False,
            )
            decisions.append(d)
            continue

        current_rank = new_rank.get(cid, rank_by_cid.get(cid))
        d = make_decision(c, ev, config, rank=current_rank)

        # Near-duplicate 抑制フラグを付与
        if cid in suppressed_ids:
            d.decision = "REJECTED"
            d.suppressed_by_dedup = True
            d.near_duplicate_of = near_dup_map.get(cid)
            d.decision_reason = (
                f"Near-duplicate of {d.near_duplicate_of}: "
                f"similarity >= {config.near_duplicate_threshold}"
            )
            d.promotion_eligible = False

        decisions.append(d)

    return decisions
