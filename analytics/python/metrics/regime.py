"""
regime.py
---------
Regime-aware metrics: crisis_period_sharpe, low_liquidity_sharpe,
                      high_vol_sharpe, event_window_only_sharpe,
                      regime_consistency_score
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class RegimeMetrics:
    crisis_period_sharpe: float = 0.0
    low_liquidity_sharpe: float = 0.0
    high_vol_sharpe: float = 0.0
    event_window_only_sharpe: float = 0.0
    regime_consistency_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "crisis_period_sharpe": self.crisis_period_sharpe,
            "low_liquidity_sharpe": self.low_liquidity_sharpe,
            "high_vol_sharpe": self.high_vol_sharpe,
            "event_window_only_sharpe": self.event_window_only_sharpe,
            "regime_consistency_score": self.regime_consistency_score,
        }


def _sharpe(r: pd.Series, ann: int = 252) -> float:
    r = r.dropna()
    if len(r) < 5 or r.std() == 0:
        return 0.0
    return float(r.mean() / r.std() * math.sqrt(ann))


def _rank_ic(signal: pd.Series, returns: pd.Series) -> float:
    s, r = signal.align(returns, join="inner")
    valid = ~(s.isna() | r.isna())
    s, r = s[valid], r[valid]
    if len(s) < 5:
        return 0.0
    return float(s.rank().corr(r.rank()))


def compute_regime(
    signal: pd.Series,
    returns: pd.Series,
    net_returns: pd.Series,
    crisis_mask: Optional[pd.Series] = None,
    liquidity_mask: Optional[pd.Series] = None,
    event_mask: Optional[pd.Series] = None,
    annualize: int = 252,
) -> RegimeMetrics:
    """
    Parameters
    ----------
    signal      : アルファシグナル
    returns     : 実現リターン
    net_returns : コスト/スリッページ控除後リターン
    crisis_mask : True = crisis regime
    liquidity_mask : True = low liquidity
    event_mask  : True = event window
    """
    m = RegimeMetrics()
    idx = net_returns.dropna().index

    # Crisis period Sharpe
    if crisis_mask is not None:
        cm = crisis_mask.reindex(idx, fill_value=False)
        m.crisis_period_sharpe = _sharpe(net_returns[cm], annualize)

        # Regime consistency (min of normal/crisis rank IC)
        nm = ~cm
        ic_n = _rank_ic(signal.reindex(idx)[nm], returns.reindex(idx)[nm])
        ic_c = _rank_ic(signal.reindex(idx)[cm], returns.reindex(idx)[cm])
        m.regime_consistency_score = min(ic_n, ic_c)
    else:
        m.regime_consistency_score = _rank_ic(signal, returns)

    # Low liquidity Sharpe
    if liquidity_mask is not None:
        lm = liquidity_mask.reindex(idx, fill_value=False)
        m.low_liquidity_sharpe = _sharpe(net_returns[lm], annualize)

    # High vol Sharpe (rolling 20日 vol の上位 25% 期間)
    rolling_vol = net_returns.rolling(20).std()
    hv_thresh   = rolling_vol.quantile(0.75)
    hv_mask     = (rolling_vol > hv_thresh).reindex(idx, fill_value=False)
    m.high_vol_sharpe = _sharpe(net_returns.reindex(idx)[hv_mask], annualize)

    # Event window only Sharpe
    if event_mask is not None:
        em = event_mask.reindex(idx, fill_value=False)
        m.event_window_only_sharpe = _sharpe(net_returns.reindex(idx)[em], annualize)

    return m
