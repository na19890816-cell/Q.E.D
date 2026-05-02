"""
eml_fitness.py
--------------
EML 候補のフィットネス評価関数群。

Promotion fitness formula:
  fitness = w1*rank_IC + w2*cost_adjusted_sharpe + w3*regime_consistency_score
          - w4*turnover_penalty - w5*complexity_penalty - w6*drawdown_penalty

各重みは環境変数 EML_PROMO_W_* から取得。
"""
from __future__ import annotations

import math
import os
from typing import Dict, Optional

import numpy as np
import pandas as pd

from .eml_core import EMLNode
from .eml_compiler import compile_to_expr
from .eml_runtime_lower import lower_and_rank_normalize


# ------------------------------------------------------------------ #
# 環境変数から重み取得
# ------------------------------------------------------------------ #

def _get_weights() -> Dict[str, float]:
    return {
        "rank_ic":       float(os.environ.get("EML_PROMO_W_RANK_IC",       "0.30")),
        "cost_sharpe":   float(os.environ.get("EML_PROMO_W_COST_SHARPE",   "0.30")),
        "regime_cons":   float(os.environ.get("EML_PROMO_W_REGIME_CONS",   "0.20")),
        "turnover_pen":  float(os.environ.get("EML_PROMO_W_TURNOVER_PEN",  "0.10")),
        "complexity_pen":float(os.environ.get("EML_PROMO_W_COMPLEXITY_PEN","0.05")),
        "drawdown_pen":  float(os.environ.get("EML_PROMO_W_DRAWDOWN_PEN",  "0.05")),
    }


# ------------------------------------------------------------------ #
# 個別スコア計算
# ------------------------------------------------------------------ #

def calc_rank_ic(
    signal: pd.Series,
    returns: pd.Series,
) -> float:
    """Rank IC (Spearman correlation)。"""
    df = pd.DataFrame({"signal": signal, "ret": returns}).dropna()
    if len(df) < 5:
        return 0.0
    return float(df["signal"].rank().corr(df["ret"].rank()))


def calc_ic_t_stat(
    signal: pd.Series,
    returns: pd.Series,
) -> float:
    """IC の t 統計量 (シリーズ単位)。"""
    ic = calc_rank_ic(signal, returns)
    n = signal.dropna().shape[0]
    if n <= 2:
        return 0.0
    t = ic * math.sqrt(n - 2) / math.sqrt(max(1 - ic**2, 1e-9))
    return float(t)


def calc_sharpe(returns: pd.Series, annualize: int = 252) -> float:
    """年率換算シャープレシオ (日次リターン想定)。"""
    r = returns.dropna()
    if len(r) < 5 or r.std() == 0:
        return 0.0
    return float(r.mean() / r.std() * math.sqrt(annualize))


def calc_max_drawdown(cum_returns: pd.Series) -> float:
    """最大ドローダウン (負値)。"""
    peak = cum_returns.cummax()
    dd = (cum_returns - peak) / peak.replace(0, np.nan)
    return float(dd.min()) if not dd.empty else 0.0


def calc_turnover(signal: pd.Series) -> float:
    """1期間当たりの平均ターンオーバー (絶対変化量の平均)。"""
    diff = signal.diff().abs()
    return float(diff.mean()) if not diff.empty else 0.0


def complexity_penalty(node: EMLNode) -> float:
    """ノード数に基づく複雑度ペナルティ (0〜1)。"""
    n = node.node_count()
    # 最大ノード数を 15 として正規化
    return min(n / 15.0, 1.0)


# ------------------------------------------------------------------ #
# メインフィットネス計算
# ------------------------------------------------------------------ #

def compute_fitness(
    node: EMLNode,
    feature_df: pd.DataFrame,
    target: pd.Series,
    regime_mask: Optional[pd.Series] = None,
    cost_bps: float = 2.0,
) -> float:
    """
    フィットネス = w1*rank_IC + w2*cost_adjusted_sharpe
                 + w3*regime_consistency_score
                 - w4*turnover_penalty - w5*complexity_penalty
                 - w6*drawdown_penalty

    Parameters
    ----------
    node        : snap 済み EMLNode
    feature_df  : terminal feature DataFrame
    target      : 予測対象リターン Series
    regime_mask : crisis regime マスク (True = crisis)
    cost_bps    : 片道コスト (bps)
    """
    weights = _get_weights()

    # シグナル計算
    expr = compile_to_expr(node)
    try:
        signal = lower_and_rank_normalize(expr, feature_df)
    except Exception:
        return -999.0

    # rank IC
    rank_ic = calc_rank_ic(signal, target)

    # コスト調整シャープ
    pos_size = signal.clip(-1, 1)
    gross_ret = pos_size * target
    turnover = calc_turnover(pos_size)
    cost_drag = turnover * cost_bps * 1e-4
    net_ret = gross_ret - cost_drag
    cost_sharpe = calc_sharpe(net_ret)

    # ターンオーバーペナルティ (0〜1)
    turnover_pen = min(turnover / 2.0, 1.0)

    # 複雑度ペナルティ
    comp_pen = complexity_penalty(node)

    # ドローダウンペナルティ (0〜1)
    cum = (1 + net_ret).cumprod()
    mdd = abs(calc_max_drawdown(cum))

    # レジームコンシステンシー
    if regime_mask is not None and regime_mask.sum() > 5:
        normal_mask = ~regime_mask.reindex(signal.index, fill_value=False)
        ic_normal = calc_rank_ic(signal[normal_mask], target[normal_mask])
        ic_crisis = calc_rank_ic(
            signal[regime_mask.reindex(signal.index, fill_value=False)],
            target[regime_mask.reindex(signal.index, fill_value=False)],
        )
        # 一貫性 = 両期間 IC の最小値を使用
        regime_cons = min(ic_normal, ic_crisis)
    else:
        regime_cons = rank_ic  # regime 情報なしは rank_IC を代替

    fitness = (
        weights["rank_ic"]       * rank_ic
        + weights["cost_sharpe"] * _clamp(cost_sharpe / 3.0, -1, 1)
        + weights["regime_cons"] * _clamp(regime_cons, -1, 1)
        - weights["turnover_pen"]  * turnover_pen
        - weights["complexity_pen"] * comp_pen
        - weights["drawdown_pen"]  * mdd
    )
    return float(fitness)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ------------------------------------------------------------------ #
# EML Search 用シンプル fitness (IC のみ)
# ------------------------------------------------------------------ #

def simple_rank_ic_fitness(
    node: EMLNode,
    feature_df: pd.DataFrame,
    target: pd.Series,
) -> float:
    """exhaustive / gradient search 用の軽量フィットネス関数。"""
    expr = compile_to_expr(node)
    try:
        signal = lower_and_rank_normalize(expr, feature_df)
    except Exception:
        return -999.0
    return calc_rank_ic(signal, target)
