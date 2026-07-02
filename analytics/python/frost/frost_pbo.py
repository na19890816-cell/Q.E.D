"""
frost_pbo.py
------------
Probability of Backtest Overfitting (PBO) 計算モジュール。

PBO は組合せ的クロスバリデーション (Combinatorial Purged Cross-Validation,
CPCV 近似) によって推定する。

設計原則:
  - 副作用なし
  - Phase 7 numpy 化 (ADR-001 対象): statistics.median → np.median
  - PBO 近似: fold 間の Sharpe ランクを用いた簡易推定
  - 「最良 IS fold を選んだ時に OOS でも最良か」の確率を測る
  - 返り値は frost_score に組み込むペナルティとして使用

参考:
  Bailey et al. (2015) "The Probability of Backtest Overfitting"
  https://doi.org/10.21314/JCF.2015.289
"""
from __future__ import annotations

import itertools
import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _safe(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


def _rank_values(values: List[float]) -> List[int]:
    """
    値のリストを昇順ランク (1-indexed) に変換する。
    同値は平均ランクで処理する (tied ranks)。
    """
    n = len(values)
    sorted_idx = sorted(range(n), key=lambda i: values[i])
    ranks = [0] * n
    i = 0
    while i < n:
        j = i
        while j < n - 1 and values[sorted_idx[j]] == values[sorted_idx[j + 1]]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-indexed
        for k in range(i, j + 1):
            ranks[sorted_idx[k]] = avg_rank
        i = j + 1
    return ranks


# ---------------------------------------------------------------------------
# 簡易 PBO 推定 (折り返し法)
# ---------------------------------------------------------------------------

def estimate_pbo_from_folds(
    fold_sharpes: List[float],
    min_folds: int = 4,
) -> float:
    """
    fold 間の Sharpe を使った簡易 PBO 近似。

    アルゴリズム:
    1. N fold を IS/OOS に半分ずつ分割する全パターンを列挙 (最大 C(N, N/2) パターン)
    2. 各パターンで:
       a. IS フォールドの Sharpe 合計が最大の「最良 IS」候補を特定
       b. 同じ候補の OOS Sharpe が OOS の中央値を下回れば「overfitting」
    3. 全パターンで overfitting した割合が PBO

    Parameters
    ----------
    fold_sharpes : list of float
        各 fold の Sharpe 比 (IS/OOS 混合)
    min_folds : int
        PBO 計算に必要な最小 fold 数

    Returns
    -------
    float
        PBO 推定値 (0〜1)。高いほど過学習リスクが高い。
        fold 不足の場合は 0.5 (不明) を返す。
    """
    n = len(fold_sharpes)
    if n < min_folds:
        return 0.5  # fold 不足 → 中立

    valid = [_safe(s) for s in fold_sharpes]

    # 最大組み合わせ数を制限 (計算量対策)
    half = n // 2
    all_combos = list(itertools.combinations(range(n), half))
    MAX_COMBOS = 256
    if len(all_combos) > MAX_COMBOS:
        # ランダムサンプリングの代わりに等間隔サンプリング
        step = len(all_combos) // MAX_COMBOS
        all_combos = all_combos[::step][:MAX_COMBOS]

    overfitting_count = 0
    total_count = 0

    for is_indices in all_combos:
        oos_indices = [i for i in range(n) if i not in is_indices]
        if not oos_indices:
            continue

        is_sharpes  = [valid[i] for i in is_indices]
        oos_sharpes = [valid[i] for i in oos_indices]

        # IS で最良の fold インデックス
        best_is_local_idx = max(range(len(is_sharpes)), key=lambda k: is_sharpes[k])
        best_is_global_idx = list(is_indices)[best_is_local_idx]

        # 同じ fold の OOS Sharpe を取得
        # (簡易近似: IS と OOS は同じ fold 集合から取るため、
        #  best IS fold の値を OOS でも「評価」する)
        oos_median = float(np.median(oos_sharpes))

        # best IS fold の Sharpe が OOS 中央値を下回れば overfitting
        # (この fold が OOS でも "best" かどうかを判定)
        best_is_sharpe = is_sharpes[best_is_local_idx]
        if best_is_sharpe < oos_median:
            overfitting_count += 1
        total_count += 1

    if total_count == 0:
        return 0.5

    pbo = overfitting_count / total_count
    return max(0.0, min(1.0, pbo))


# ---------------------------------------------------------------------------
# 代替 PBO: IS/OOS ペア法
# ---------------------------------------------------------------------------

def estimate_pbo_is_oos_pairs(
    is_sharpes: List[float],
    oos_sharpes: List[float],
) -> float:
    """
    IS/OOS ペアを直接受け取る場合の PBO 推定。

    「IS で正、OOS で負」の割合で過学習を検出する。

    Parameters
    ----------
    is_sharpes : list of float
    oos_sharpes : list of float

    Returns
    -------
    float
        PBO 推定値 (0〜1)
    """
    if not is_sharpes or not oos_sharpes or len(is_sharpes) != len(oos_sharpes):
        return 0.5

    n = len(is_sharpes)
    overfit = sum(
        1 for is_s, oos_s in zip(is_sharpes, oos_sharpes)
        if _safe(is_s) > 0 and _safe(oos_s) <= 0
    )
    return overfit / n


# ---------------------------------------------------------------------------
# 選抜脆弱性 (PBO の補完指標)
# ---------------------------------------------------------------------------

def compute_selection_fragility(
    fold_sharpes: List[float],
    fold_rank_ics: List[float],
) -> float:
    """
    選抜脆弱性スコアを計算する (0〜1)。

    fold を 1 つ抜いた時にランキングが変動する度合いを測る
    (Leave-One-Fold-Out 不安定性)。

    Parameters
    ----------
    fold_sharpes : list of float
    fold_rank_ics : list of float

    Returns
    -------
    float
        脆弱性スコア (0〜1)、高いほど不安定
    """
    n = len(fold_sharpes)
    if n < 3:
        return 0.5

    valid_s = [_safe(s) for s in fold_sharpes]
    overall_mean = sum(valid_s) / n

    # LOFO: fold i を抜いた時の mean Sharpe の変化
    changes = []
    for i in range(n):
        lofo = [s for j, s in enumerate(valid_s) if j != i]
        lofo_mean = sum(lofo) / len(lofo)
        changes.append(abs(overall_mean - lofo_mean))

    # 変化の最大値を脆弱性として使用
    max_change = max(changes)
    # Sharpe ≈ 1.0 に対して 0.3 以上の変化があれば高脆弱性
    fragility = min(1.0, max_change / 0.5)
    return fragility


# ---------------------------------------------------------------------------
# PBO 計算の統合エントリポイント
# ---------------------------------------------------------------------------

def compute_pbo_all(
    fold_results: List[Dict[str, Any]],
    min_folds: int = 4,
) -> Dict[str, float]:
    """
    fold_results から PBO 関連の全スコアを計算する。

    Parameters
    ----------
    fold_results : list of dict
        各 fold の結果辞書 (sharpe / ic / rank_ic 等を含む)
    min_folds : int
        PBO 計算に必要な最小 fold 数

    Returns
    -------
    dict
        pbo_score, selection_fragility, fold_sharpe_std 等
    """
    if not fold_results:
        return {
            "pbo_score":           0.5,
            "selection_fragility": 0.5,
            "n_folds":             0,
        }

    fold_sharpes  = [_safe(f.get("sharpe") or f.get("oos_sharpe", 0.0)) for f in fold_results]
    fold_rank_ics = [_safe(f.get("rank_ic", 0.0)) for f in fold_results]

    pbo_score = estimate_pbo_from_folds(fold_sharpes, min_folds)
    fragility = compute_selection_fragility(fold_sharpes, fold_rank_ics)

    # 複合 PBO: 単純 PBO と脆弱性の加重平均
    combined_pbo = 0.7 * pbo_score + 0.3 * fragility

    return {
        "pbo_score":           max(0.0, min(1.0, combined_pbo)),
        "pbo_raw":             pbo_score,
        "selection_fragility": fragility,
        "n_folds":             len(fold_results),
        "fold_sharpe_mean":    sum(fold_sharpes) / len(fold_sharpes) if fold_sharpes else 0.0,
    }
