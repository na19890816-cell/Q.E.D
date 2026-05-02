"""
risk.py
-------
Risk-adjusted metrics: risk_adjusted_return, var_5, cvar_5,
                       downside_vol, kelly_fraction, position_concentration
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class RiskMetrics:
    risk_adjusted_return: float = 0.0
    var_5: float = 0.0
    cvar_5: float = 0.0
    downside_vol: float = 0.0
    kelly_fraction: float = 0.0
    position_concentration: float = 0.0

    def to_dict(self) -> dict:
        return {
            "risk_adjusted_return": self.risk_adjusted_return,
            "var_5": self.var_5,
            "cvar_5": self.cvar_5,
            "downside_vol": self.downside_vol,
            "kelly_fraction": self.kelly_fraction,
            "position_concentration": self.position_concentration,
        }


def compute_risk(
    returns: pd.Series,
    position: pd.Series | None = None,
    annualize: int = 252,
) -> RiskMetrics:
    r = returns.dropna()
    m = RiskMetrics()
    if len(r) < 5:
        return m

    # VaR 5%
    m.var_5 = float(r.quantile(0.05))

    # CVaR 5%
    threshold = r.quantile(0.05)
    tail = r[r <= threshold]
    m.cvar_5 = float(tail.mean()) if len(tail) > 0 else 0.0

    # Downside vol (年率)
    downside = r[r < 0]
    if len(downside) >= 2:
        m.downside_vol = float(downside.std() * math.sqrt(annualize))

    # Risk-adjusted return = mean / downside_vol
    if m.downside_vol > 0:
        m.risk_adjusted_return = float(r.mean() * annualize / m.downside_vol)

    # Kelly fraction = μ / σ²
    mu    = r.mean()
    sigma2 = r.var()
    m.kelly_fraction = float(mu / sigma2) if sigma2 > 0 else 0.0

    # Position concentration = 平均絶対ポジション
    if position is not None:
        pos = position.reindex(r.index).abs()
        m.position_concentration = float(pos.mean()) if len(pos) > 0 else 0.0

    return m
