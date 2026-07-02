"""
frost_signal_dedup.py
----------------------
OOS Signal Correlation Dedup — Phase 4 委譲シム。

Phase 4 で `DedupStage` に処理を移管した。
本モジュールの公開 API はシムとして残し、内部を DedupStage に委譲する。
既存の呼び出し元は変更不要。

主要関数 (後方互換 API)
-----------------------
compute_signal_correlation_matrix(signal_matrix)
    → DedupStage.compute_correlation_matrix() に委譲

find_oos_duplicates(corr_matrix, threshold)
    → DedupStage.find_corr_duplicates() に委譲

select_preferred_from_duplicates(dup_pairs, eval_map)
    → DedupStage._select_preferred() に委譲 (内部インスタンス経由)

apply_signal_dedup(evaluations, signal_matrix, threshold)
    → DedupStage().apply_signal() に委譲

設計原則
--------
- numpy 不使用（pure Python + statistics モジュール）
- NaN/Inf セーフ
- dry_run 対応（flag をセットするだけで DB に書かない）
- FROST_SIGNAL_DEDUP_ENABLED=0 でスキップ
- trace_id 維持
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

# --------------------------------------------------------------------------- #
# 設定 (後方互換: モジュールレベル変数として公開)
# --------------------------------------------------------------------------- #

_ENABLED: bool = os.environ.get("FROST_SIGNAL_DEDUP_ENABLED", "1") == "1"
_CORR_MAX: float = float(os.environ.get("FROST_SIGNAL_CORR_MAX", "0.90"))


# --------------------------------------------------------------------------- #
# 後方互換 Dataclasses
# Phase 4: dedup_stage.SignalDedupResult を再エクスポート
# --------------------------------------------------------------------------- #

from analytics.python.frost.dedup_stage import SignalDedupResult  # noqa: E402


@dataclass
class _EvalProxy:
    """
    パイプライン外からテストする際に使う最小ダックタイプ。
    FrostEvaluation の部分集合。
    """
    candidate_id: str
    frost_score: float = 0.0
    complexity_penalty: float = 0.0
    # crowding / regime_entropy は v2 で追加されるが、ここではオプション
    crowding_penalty: float = 0.0
    regime_entropy_score: float = 0.0


# --------------------------------------------------------------------------- #
# 後方互換 API — 内部を DedupStage に委譲
# --------------------------------------------------------------------------- #

def compute_signal_correlation_matrix(
    signal_matrix: Dict[str, List[float]],
) -> Dict[Tuple[str, str], float]:
    """
    候補ごとの OOS シグナル列から全ペアのピアソン相関を算出する。

    Phase 4: DedupStage.compute_correlation_matrix() に委譲。
    """
    from analytics.python.frost.dedup_stage import DedupStage
    return DedupStage.compute_correlation_matrix(signal_matrix)


def find_oos_duplicates(
    corr_matrix: Dict[Tuple[str, str], float],
    threshold: float = _CORR_MAX,
) -> List[Tuple[str, str, float]]:
    """
    |ρ| > threshold なペアを near-duplicate として返す。

    Phase 4: DedupStage.find_corr_duplicates() に委譲。
    """
    from analytics.python.frost.dedup_stage import DedupStage
    return DedupStage.find_corr_duplicates(corr_matrix, threshold)


def select_preferred_from_duplicates(
    dup_pairs: List[Tuple[str, str, float]],
    eval_map: Dict[str, _EvalProxy],
) -> SignalDedupResult:
    """
    重複ペアから「残す候補」を決定し、SignalDedupResult を返す。

    Phase 4: DedupStage._select_preferred() に委譲。
    """
    from analytics.python.frost.dedup_stage import DedupStage
    stage = DedupStage(signal_threshold=_CORR_MAX)
    return stage._select_preferred(dup_pairs, eval_map, _CORR_MAX)


def apply_signal_dedup(
    evaluations: List,
    signal_matrix: Dict[str, List[float]],
    threshold: Optional[float] = None,
) -> Tuple[List, SignalDedupResult]:
    """
    FROST パイプラインへの統合エントリポイント。

    Phase 4: DedupStage().apply_signal() に委譲。

    Parameters
    ----------
    evaluations : List[FrostEvaluation]
    signal_matrix : {candidate_id: [oos_signal_t0, ...]}
    threshold : float | None

    Returns
    -------
    (evaluations_updated, SignalDedupResult)
    """
    from analytics.python.frost.dedup_stage import DedupStage
    stage = DedupStage(
        signal_threshold=threshold if threshold is not None else _CORR_MAX,
        signal_dedup_enabled=_ENABLED,
    )
    return stage.apply_signal(evaluations, signal_matrix, threshold)
