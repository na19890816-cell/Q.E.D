"""
frost_stability.py
------------------
FROST 安定性評価モジュール。

fold 間の性能ブレ・レジーム偏差・sign 安定性・top-k 安定性を計算する。

設計原則:
  - 副作用なし
  - numpy 不使用 (標準ライブラリ + math のみ)
  - fold_results は dict のリストを想定 (harness.py FoldResult 相当)
  - 返り値は FrostEvaluation に詰め込む形式の dict
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


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


def _stdev_safe(values: List[float]) -> float:
    """標準偏差 (n < 2 の場合は 0.0 を返す)。純 Python 実装 (ADR-001 禁止リスト対応)。"""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(max(0.0, var))


def _mean_safe(values: List[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


# ---------------------------------------------------------------------------
# Fold 間 Sharpe 安定性
# ---------------------------------------------------------------------------

def compute_fold_sharpe_stability(
    fold_sharpes: List[float],
    min_folds: int = 3,
) -> Tuple[float, float, float]:
    """
    fold 間の Sharpe 安定性を計算する。

    Parameters
    ----------
    fold_sharpes : list of float
        各 fold の Sharpe 比
    min_folds : int
        安定性評価に必要な最小 fold 数

    Returns
    -------
    tuple (mean_sharpe, sharpe_std, stability_score)
        stability_score: 0〜1、高いほど安定
    """
    if len(fold_sharpes) < min_folds:
        return 0.0, 0.0, 0.5  # fold 不足 → 中立スコア

    valid = [s for s in fold_sharpes if not math.isnan(s)]
    if not valid:
        return 0.0, 0.0, 0.0

    mean_s = _mean_safe(valid)
    std_s  = _stdev_safe(valid)

    # 変動係数 (CV = std / |mean|) で安定性を測る
    if abs(mean_s) < 0.01:
        # mean ≈ 0 の場合は std を直接使用
        cv = std_s
    else:
        cv = std_s / abs(mean_s)

    # CV が低いほど安定: [0, 2] → [1, 0] の線形変換
    stability = max(0.0, min(1.0, 1.0 - cv / 2.0))
    return mean_s, std_s, stability


# ---------------------------------------------------------------------------
# Fold 間 IC 安定性
# ---------------------------------------------------------------------------

def compute_fold_ic_stability(
    fold_ics: List[float],
    min_folds: int = 3,
) -> Tuple[float, float]:
    """
    fold 間の IC 安定性を計算する。

    Returns
    -------
    tuple (mean_ic, ic_stability_score)
    """
    if len(fold_ics) < min_folds:
        return 0.0, 0.5

    valid = [v for v in fold_ics if not math.isnan(v)]
    if not valid:
        return 0.0, 0.0

    mean_ic = _mean_safe(valid)
    std_ic  = _stdev_safe(valid)

    if abs(mean_ic) < 0.001:
        stability = max(0.0, 1.0 - std_ic)
    else:
        cv = std_ic / abs(mean_ic)
        stability = max(0.0, min(1.0, 1.0 - cv / 2.0))

    return mean_ic, stability


# ---------------------------------------------------------------------------
# Sign 安定性 (fold ごとの方向性一貫性)
# ---------------------------------------------------------------------------

def compute_sign_stability(
    fold_values: List[float],
) -> float:
    """
    fold 間の sign (正負) 一貫性を計算する。

    Parameters
    ----------
    fold_values : list of float
        fold ごとの Sharpe / IC / return など

    Returns
    -------
    float
        sign 安定性スコア (0〜1)
        1.0 = 全 fold で同符号, 0.0 = 半々
    """
    if not fold_values:
        return 0.5

    valid = [v for v in fold_values if not math.isnan(v)]
    if not valid:
        return 0.5

    n = len(valid)
    positive = sum(1 for v in valid if v > 0)
    negative = n - positive

    majority = max(positive, negative)
    return majority / n


# ---------------------------------------------------------------------------
# Top-k 安定性 (fold ごとの候補ランキング一貫性)
# ---------------------------------------------------------------------------

def compute_top_k_stability(
    fold_rankings: List[List[str]],
    k: int = 5,
) -> float:
    """
    fold ごとの top-k candidate_id セットの重複率から安定性を計算する。

    Parameters
    ----------
    fold_rankings : list of list of str
        各 fold の上位 candidate_id リスト
    k : int
        top-k の k

    Returns
    -------
    float
        top-k 安定性スコア (0〜1)
        1.0 = 全 fold で同じ top-k, 0.0 = 全く異なる
    """
    if len(fold_rankings) < 2:
        return 1.0  # fold が 1 つ以下は評価不能 → 最大スコア

    top_k_sets = [set(r[:k]) for r in fold_rankings]

    # 全ペアの Jaccard 類似度の平均
    pair_count = 0
    total_jaccard = 0.0
    n = len(top_k_sets)
    for i in range(n):
        for j in range(i + 1, n):
            s1, s2 = top_k_sets[i], top_k_sets[j]
            union = len(s1 | s2)
            if union == 0:
                jaccard = 1.0
            else:
                jaccard = len(s1 & s2) / union
            total_jaccard += jaccard
            pair_count += 1

    return total_jaccard / pair_count if pair_count > 0 else 1.0


# ---------------------------------------------------------------------------
# レジーム偏差スコア
# ---------------------------------------------------------------------------

def compute_regime_deviation_score(
    regime_sharpes: List[float],
) -> float:
    """
    レジーム間の性能偏差スコアを計算する (0〜1、高いほど安定)。

    Parameters
    ----------
    regime_sharpes : list of float
        各レジーム (bull / bear / crisis) の Sharpe リスト

    Returns
    -------
    float
    """
    if len(regime_sharpes) < 2:
        return 0.5

    valid = [v for v in regime_sharpes if not math.isnan(v)]
    if len(valid) < 2:
        return 0.5

    std_r = _stdev_safe(valid)
    mean_r = _mean_safe(valid)

    if abs(mean_r) < 0.01:
        penalty = min(1.0, std_r * 2.0)
    else:
        cv = std_r / abs(mean_r)
        penalty = min(1.0, cv)

    return max(0.0, 1.0 - penalty)


# ---------------------------------------------------------------------------
# 選抜整合性スコア (メイン)
# ---------------------------------------------------------------------------

def compute_selection_consistency_score(
    fold_sharpes: List[float],
    fold_ics: List[float],
    regime_sharpes: List[float],
    min_folds: int = 3,
) -> float:
    """
    総合的な選抜整合性スコアを計算する (0〜1)。

    fold_sharpe_stability, fold_ic_stability, sign_stability,
    regime_deviation_score を組み合わせる。

    Returns
    -------
    float
        選抜整合性スコア (0〜1)
    """
    _, _, sharpe_stability = compute_fold_sharpe_stability(fold_sharpes, min_folds)
    _, ic_stability        = compute_fold_ic_stability(fold_ics, min_folds)
    sign_stab              = compute_sign_stability(fold_sharpes)
    regime_stab            = compute_regime_deviation_score(regime_sharpes)

    # 加重平均
    score = (
        0.35 * sharpe_stability
        + 0.25 * ic_stability
        + 0.20 * sign_stab
        + 0.20 * regime_stab
    )
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# 全安定性スコアを一括計算
# ---------------------------------------------------------------------------

def compute_all_stability(
    fold_sharpes: List[float],
    fold_ics: List[float],
    fold_rank_ics: List[float],
    regime_sharpes: List[float],
    min_folds: int = 3,
) -> Dict[str, Any]:
    """
    全安定性スコアを計算して辞書として返す。

    Returns
    -------
    dict
        FrostEvaluation に詰め込む各安定性スコア
    """
    mean_s, fold_sharpe_std, sharpe_stability = compute_fold_sharpe_stability(
        fold_sharpes, min_folds
    )
    mean_ic, ic_stability = compute_fold_ic_stability(fold_ics, min_folds)
    sign_stab = compute_sign_stability(fold_sharpes)
    regime_stab = compute_regime_deviation_score(regime_sharpes)
    selection_consistency = compute_selection_consistency_score(
        fold_sharpes, fold_ics, regime_sharpes, min_folds
    )

    return {
        "selection_consistency_score": selection_consistency,
        "top_k_stability":             sharpe_stability,   # fold-Sharpe 安定性
        "sign_stability":              sign_stab,
        "fold_sharpe_std":             fold_sharpe_std,    # fragility_penalty に使用
        "fold_sharpe_mean":            mean_s,
        "fold_ic_mean":                mean_ic,
        "regime_stability_from_folds": regime_stab,
    }
