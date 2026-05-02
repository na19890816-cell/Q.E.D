"""
predictive.py
-------------
Predictive metrics: IC, rank_IC, IC_t_stat, hit_rate, r2_oos, event_window_ic
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class PredictiveMetrics:
    ic: float = 0.0
    rank_ic: float = 0.0
    ic_t_stat: float = 0.0
    hit_rate: float = 0.0
    r2_oos: float = 0.0
    event_window_ic: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ic": self.ic,
            "rank_ic": self.rank_ic,
            "ic_t_stat": self.ic_t_stat,
            "hit_rate": self.hit_rate,
            "r2_oos": self.r2_oos,
            "event_window_ic": self.event_window_ic,
        }


def compute_predictive(
    signal: pd.Series,
    returns: pd.Series,
    event_mask: Optional[pd.Series] = None,
) -> PredictiveMetrics:
    """
    Parameters
    ----------
    signal     : アルファシグナル (cross-sectional rank 正規化済み推奨)
    returns    : 実現リターン
    event_mask : True = event window (IC を別途計算)
    """
    s, r = signal.align(returns, join="inner")
    valid = ~(s.isna() | r.isna())
    s, r = s[valid], r[valid]
    n = len(s)

    m = PredictiveMetrics()
    if n < 5:
        return m

    # Pearson IC
    m.ic = float(s.corr(r)) if n > 1 else 0.0

    # Rank IC (Spearman)
    m.rank_ic = float(s.rank().corr(r.rank())) if n > 1 else 0.0

    # IC t-stat
    ic = m.rank_ic
    if n > 2:
        denom = math.sqrt(max(1 - ic**2, 1e-12))
        m.ic_t_stat = float(ic * math.sqrt(n - 2) / denom)

    # Hit rate (signal 正と return 正の一致率)
    m.hit_rate = float(((s > 0) == (r > 0)).mean())

    # R2 OOS (signal を予測値として使用)
    ss_res = ((r - s) ** 2).sum()
    ss_tot = ((r - r.mean()) ** 2).sum()
    m.r2_oos = float(1 - ss_res / max(ss_tot, 1e-12))

    # Event window IC
    if event_mask is not None:
        em = event_mask.reindex(s.index, fill_value=False)
        es, er = s[em], r[em]
        if len(es) >= 5:
            m.event_window_ic = float(es.rank().corr(er.rank()))

    return m
