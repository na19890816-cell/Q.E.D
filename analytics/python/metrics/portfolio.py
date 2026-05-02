"""
portfolio.py
------------
Portfolio metrics: sharpe, sortino, calmar, max_drawdown,
                   recovery_period, tail_ratio, cvar_5
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class PortfolioMetrics:
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    max_drawdown: float = 0.0
    recovery_period: int = 0
    tail_ratio: float = 0.0
    cvar_5: float = 0.0

    def to_dict(self) -> dict:
        return {
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "calmar": self.calmar,
            "max_drawdown": self.max_drawdown,
            "recovery_period": self.recovery_period,
            "tail_ratio": self.tail_ratio,
            "cvar_5": self.cvar_5,
        }


def compute_portfolio(
    returns: pd.Series,
    annualize: int = 252,
) -> PortfolioMetrics:
    r = returns.dropna()
    m = PortfolioMetrics()
    if len(r) < 5:
        return m

    mu    = r.mean()
    sigma = r.std()
    cum   = (1 + r).cumprod()

    # Sharpe
    m.sharpe = float(mu / sigma * math.sqrt(annualize)) if sigma > 0 else 0.0

    # Sortino
    downside = r[r < 0]
    ds_vol = downside.std() * math.sqrt(annualize) if len(downside) >= 2 else 0.0
    m.sortino = float(mu * annualize / ds_vol) if ds_vol > 0 else 0.0

    # Max drawdown
    peak = cum.cummax()
    dd   = (cum - peak) / peak.replace(0, np.nan)
    m.max_drawdown = float(dd.min()) if not dd.empty else 0.0

    # Calmar
    ann_ret = float((1 + mu) ** annualize - 1)
    m.calmar = float(ann_ret / abs(m.max_drawdown)) if m.max_drawdown != 0 else 0.0

    # Recovery period (最長アンダーウォーター bars)
    underwater = cum < peak
    max_run = run = 0
    for v in underwater:
        run = run + 1 if v else 0
        max_run = max(max_run, run)
    m.recovery_period = max_run

    # Tail ratio
    q95 = r.quantile(0.95)
    q05 = abs(r.quantile(0.05))
    m.tail_ratio = float(q95 / q05) if q05 > 0 else 0.0

    # CVaR 5%
    threshold = r.quantile(0.05)
    tail = r[r <= threshold]
    m.cvar_5 = float(tail.mean()) if len(tail) > 0 else 0.0

    return m
