"""
portfolio_simulator.py
----------------------
ポートフォリオシミュレーター: シグナルからネットリターン系列を生成。

コスト・スリッページを控除した純リターンを返す。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .cost_model import CostModelConfig, apply_cost
from .slippage_model import SlippageModelConfig, apply_slippage


@dataclass
class SimulationResult:
    gross_returns: pd.Series
    net_returns: pd.Series
    position: pd.Series
    cost_series: pd.Series
    slippage_series: pd.Series
    turnover: float
    total_cost_drag: float


def simulate_portfolio(
    signal: pd.Series,
    returns: pd.Series,
    cost_config: Optional[CostModelConfig] = None,
    slippage_config: Optional[SlippageModelConfig] = None,
    max_position: float = 1.0,
) -> SimulationResult:
    """
    Parameters
    ----------
    signal         : アルファシグナル (rank 正規化推奨)
    returns        : 実現リターン
    cost_config    : コスト設定 (None → 環境変数)
    slippage_config: スリッページ設定 (None → 環境変数)
    max_position   : ポジション上限 (デフォルト 1.0)
    """
    if cost_config is None:
        cost_config = CostModelConfig.from_env()
    if slippage_config is None:
        slippage_config = SlippageModelConfig.from_env()

    # アライン
    sig, ret = signal.align(returns, join="inner")
    sig, ret = sig.dropna(), ret.dropna()
    sig = sig.reindex(ret.index).fillna(0.0)

    # ポジション
    pos = sig.clip(-max_position, max_position)

    # グロスリターン
    gross_ret = pos * ret

    # コスト・スリッページ
    cost_s  = apply_cost(pos, cost_config)
    slip_s  = apply_slippage(pos, slippage_config)

    # ネットリターン
    net_ret = gross_ret - cost_s - slip_s

    turnover = float(pos.diff().abs().mean())
    total_cost_drag = float((cost_s + slip_s).mean())

    return SimulationResult(
        gross_returns=gross_ret,
        net_returns=net_ret,
        position=pos,
        cost_series=cost_s,
        slippage_series=slip_s,
        turnover=turnover,
        total_cost_drag=total_cost_drag,
    )
