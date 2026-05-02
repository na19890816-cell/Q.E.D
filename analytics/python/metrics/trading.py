"""
trading.py
----------
Trading metrics: turnover, trade_count, avg_hold_days,
                 win_loss_ratio, expectancy, cost_drag
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class TradingMetrics:
    turnover: float = 0.0
    trade_count: int = 0
    avg_hold_days: float = 0.0
    win_loss_ratio: float = 0.0
    expectancy: float = 0.0
    cost_drag: float = 0.0

    def to_dict(self) -> dict:
        return {
            "turnover": self.turnover,
            "trade_count": self.trade_count,
            "avg_hold_days": self.avg_hold_days,
            "win_loss_ratio": self.win_loss_ratio,
            "expectancy": self.expectancy,
            "cost_drag": self.cost_drag,
        }


def compute_trading(
    position: pd.Series,
    returns: pd.Series,
    cost_bps: float = 2.0,
    slippage_bps: float = 2.0,
) -> TradingMetrics:
    """
    Parameters
    ----------
    position   : ポジションサイズ Series [-1, 1]
    returns    : 実現リターン Series
    cost_bps   : 片道コスト (basis points)
    slippage_bps: 片道スリッページ (basis points)
    """
    m = TradingMetrics()
    pos = position.dropna()
    r   = returns.reindex(pos.index).dropna()
    pos = pos.reindex(r.index)
    if len(pos) < 2:
        return m

    delta    = pos.diff().abs()
    turnover = float(delta.mean())
    m.turnover = turnover

    # 総コスト (cost + slippage)
    total_bps = cost_bps + slippage_bps
    cost_per_bar = delta * total_bps * 1e-4
    m.cost_drag = float(cost_per_bar.mean())

    # 取引回数 (|Δpos| > 5% をトレードとみなす)
    m.trade_count = int((delta > 0.05).sum())

    # 平均保有日数
    m.avg_hold_days = float(1.0 / max(turnover, 1e-6))

    # 純リターン
    net_ret = pos * r - cost_per_bar

    # Win/Loss ratio
    wins   = net_ret[net_ret > 0]
    losses = net_ret[net_ret < 0]
    if len(losses) > 0 and losses.mean() != 0:
        m.win_loss_ratio = float(wins.mean() / abs(losses.mean()))

    # Expectancy (期待値)
    m.expectancy = float(net_ret.mean()) if len(net_ret) > 0 else 0.0

    return m
