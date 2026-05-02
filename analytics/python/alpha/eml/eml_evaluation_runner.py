"""
eml_evaluation_runner.py
------------------------
EML 候補に対して 5 指標グループを一括評価し EMLEvaluationResult を返す。

使用:
  runner = EMLEvaluationRunner(feature_df, target, regime_mask)
  result = runner.run(candidate)
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from .eml_core import EMLNode
from .eml_compiler import compile_to_expr
from .eml_runtime_lower import lower_and_rank_normalize
from .eml_fitness import (
    calc_rank_ic,
    calc_ic_t_stat,
    calc_sharpe,
    calc_max_drawdown,
    calc_turnover,
)
from .eml_search import EMLCandidate


# ------------------------------------------------------------------ #
# 評価結果
# ------------------------------------------------------------------ #

@dataclass
class EMLEvaluationResult:
    eval_id: str
    candidate_id: str
    run_id: str
    trace_id: str
    horizon: str

    # Predictive metrics
    rank_ic: float = 0.0
    ic: float = 0.0
    ic_t_stat: float = 0.0
    hit_rate: float = 0.0
    r2_oos: float = 0.0
    event_window_ic: float = 0.0

    # Portfolio metrics
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    max_drawdown: float = 0.0
    recovery_period: int = 0
    tail_ratio: float = 0.0
    cvar_5: float = 0.0

    # Trading metrics
    turnover: float = 0.0
    trade_count: int = 0
    avg_hold_days: float = 0.0
    win_loss_ratio: float = 0.0
    expectancy: float = 0.0
    cost_drag: float = 0.0

    # Risk-adjusted metrics
    risk_adjusted_return: float = 0.0
    var_5: float = 0.0
    downside_vol: float = 0.0
    kelly_fraction: float = 0.0
    position_concentration: float = 0.0

    # Regime-aware metrics
    crisis_period_sharpe: float = 0.0
    low_liquidity_sharpe: float = 0.0
    high_vol_sharpe: float = 0.0
    event_window_only_sharpe: float = 0.0
    regime_consistency_score: float = 0.0

    metadata: dict = field(default_factory=dict)


# ------------------------------------------------------------------ #
# ランナー
# ------------------------------------------------------------------ #

class EMLEvaluationRunner:
    def __init__(
        self,
        feature_df: pd.DataFrame,
        target: pd.Series,
        regime_mask: Optional[pd.Series] = None,
        event_mask: Optional[pd.Series] = None,
        liquidity_mask: Optional[pd.Series] = None,
        cost_bps: float = 2.0,
        horizon: str = "5d",
    ):
        self.feature_df    = feature_df
        self.target        = target
        self.regime_mask   = regime_mask    # True = crisis
        self.event_mask    = event_mask     # True = event window
        self.liquidity_mask = liquidity_mask  # True = low liquidity
        self.cost_bps      = cost_bps
        self.horizon       = horizon

    # ------------------------------------------------------------------ #

    def run(self, candidate: EMLCandidate) -> EMLEvaluationResult:
        eval_id = str(uuid.uuid4())
        result = EMLEvaluationResult(
            eval_id=eval_id,
            candidate_id=candidate.candidate_id,
            run_id=candidate.run_id,
            trace_id=candidate.trace_id,
            horizon=self.horizon,
        )

        # シグナル
        expr = candidate.compiled_expr
        try:
            signal = lower_and_rank_normalize(expr, self.feature_df)
        except Exception as e:
            result.metadata["error"] = str(e)
            return result

        target = self.target.reindex(signal.index)
        signal, target = signal.align(target, join="inner")
        signal = signal.dropna()
        target = target.reindex(signal.index).dropna()
        signal = signal.reindex(target.index)

        # ポジションとリターン
        pos = signal.clip(-1, 1)
        gross_ret  = pos * target
        turnover   = calc_turnover(pos)
        cost_drag  = turnover * self.cost_bps * 1e-4
        net_ret    = gross_ret - cost_drag
        cum_net    = (1 + net_ret).cumprod()

        # ---------- Predictive ----------
        result.rank_ic      = calc_rank_ic(signal, target)
        result.ic           = float(signal.corr(target) or 0)
        result.ic_t_stat    = calc_ic_t_stat(signal, target)
        result.hit_rate     = float((net_ret > 0).sum() / max(len(net_ret), 1))
        result.r2_oos       = _r2_oos(signal, target)
        if self.event_mask is not None:
            em = self.event_mask.reindex(signal.index, fill_value=False)
            result.event_window_ic = calc_rank_ic(signal[em], target[em])

        # ---------- Portfolio ----------
        result.sharpe       = calc_sharpe(net_ret)
        result.sortino      = _sortino(net_ret)
        result.max_drawdown = calc_max_drawdown(cum_net)
        result.calmar       = _calmar(net_ret, result.max_drawdown)
        result.recovery_period = _recovery_period(cum_net)
        result.tail_ratio   = _tail_ratio(net_ret)
        result.cvar_5       = _cvar(net_ret, 0.05)

        # ---------- Trading ----------
        result.turnover     = turnover
        result.trade_count  = int((pos.diff().abs() > 0.1).sum())
        result.avg_hold_days = 1.0 / max(turnover, 1e-6)
        wins = net_ret[net_ret > 0]
        losses = net_ret[net_ret < 0]
        result.win_loss_ratio = (
            float(wins.mean() / abs(losses.mean()))
            if len(losses) > 0 and losses.mean() != 0 else 0.0
        )
        result.expectancy   = float(net_ret.mean()) if len(net_ret) > 0 else 0.0
        result.cost_drag    = float(cost_drag.mean()) if hasattr(cost_drag, "mean") else cost_drag

        # ---------- Risk-adjusted ----------
        result.var_5        = float(net_ret.quantile(0.05)) if len(net_ret) > 0 else 0.0
        result.cvar_5       = _cvar(net_ret, 0.05)
        result.downside_vol = _downside_vol(net_ret)
        mean_r = float(net_ret.mean()) if len(net_ret) > 0 else 0.0
        result.risk_adjusted_return = (
            mean_r / result.downside_vol if result.downside_vol > 0 else 0.0
        )
        result.kelly_fraction = _kelly(net_ret)
        result.position_concentration = float(pos.abs().mean())

        # ---------- Regime-aware ----------
        if self.regime_mask is not None:
            cm = self.regime_mask.reindex(net_ret.index, fill_value=False)
            result.crisis_period_sharpe = calc_sharpe(net_ret[cm])
            result.regime_consistency_score = _regime_consistency(
                signal, target, cm
            )
        if self.liquidity_mask is not None:
            lm = self.liquidity_mask.reindex(net_ret.index, fill_value=False)
            result.low_liquidity_sharpe = calc_sharpe(net_ret[lm])
        # high_vol proxy: 上位 25% vol 期間
        rolling_vol = net_ret.rolling(20).std()
        hv_mask = rolling_vol > rolling_vol.quantile(0.75)
        result.high_vol_sharpe = calc_sharpe(net_ret[hv_mask])
        if self.event_mask is not None:
            em = self.event_mask.reindex(net_ret.index, fill_value=False)
            result.event_window_only_sharpe = calc_sharpe(net_ret[em])

        return result


# ------------------------------------------------------------------ #
# ヘルパー
# ------------------------------------------------------------------ #

def _r2_oos(signal: pd.Series, target: pd.Series) -> float:
    """OOS R^2。"""
    df = pd.DataFrame({"s": signal, "t": target}).dropna()
    if len(df) < 5:
        return 0.0
    ss_res = ((df["t"] - df["s"]) ** 2).sum()
    ss_tot = ((df["t"] - df["t"].mean()) ** 2).sum()
    return float(1 - ss_res / max(ss_tot, 1e-12))


def _sortino(returns: pd.Series, ann: int = 252) -> float:
    r = returns.dropna()
    downside = r[r < 0]
    if len(downside) < 2:
        return 0.0
    ds_vol = downside.std() * (ann ** 0.5)
    if ds_vol == 0:
        return 0.0
    return float(r.mean() * ann / ds_vol)


def _calmar(returns: pd.Series, mdd: float) -> float:
    if mdd == 0:
        return 0.0
    ann_ret = (1 + returns.mean()) ** 252 - 1
    return float(ann_ret / abs(mdd))


def _recovery_period(cum_ret: pd.Series) -> int:
    """ピークからの最大回復期間 (bars)。"""
    peak = cum_ret.cummax()
    underwater = cum_ret < peak
    if not underwater.any():
        return 0
    # 連続アンダーウォーター期間の最大値
    max_run = 0
    run = 0
    for v in underwater:
        if v:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    return max_run


def _tail_ratio(returns: pd.Series) -> float:
    r = returns.dropna()
    if len(r) < 10:
        return 0.0
    q95 = r.quantile(0.95)
    q05 = abs(r.quantile(0.05))
    return float(q95 / q05) if q05 > 0 else 0.0


def _cvar(returns: pd.Series, q: float) -> float:
    r = returns.dropna()
    threshold = r.quantile(q)
    tail = r[r <= threshold]
    return float(tail.mean()) if len(tail) > 0 else 0.0


def _downside_vol(returns: pd.Series, ann: int = 252) -> float:
    r = returns.dropna()
    downside = r[r < 0]
    if len(downside) < 2:
        return 0.0
    return float(downside.std() * (ann ** 0.5))


def _kelly(returns: pd.Series) -> float:
    r = returns.dropna()
    if len(r) < 5:
        return 0.0
    mu = r.mean()
    sigma2 = r.var()
    return float(mu / sigma2) if sigma2 > 0 else 0.0


def _regime_consistency(
    signal: pd.Series,
    target: pd.Series,
    crisis_mask: pd.Series,
) -> float:
    normal_mask = ~crisis_mask
    ic_n = calc_rank_ic(signal[normal_mask], target[normal_mask])
    ic_c = calc_rank_ic(signal[crisis_mask], target[crisis_mask])
    return min(ic_n, ic_c)
