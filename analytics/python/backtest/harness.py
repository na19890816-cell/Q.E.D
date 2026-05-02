"""
harness.py
----------
Walk-forward バックテストハーネス。

2 モード:
  - expanding : 学習期間を拡大しながら前進 (デフォルト)
  - rolling   : 固定長ウィンドウで前進 (クライシスストレス用)

使用:
  harness = WalkForwardHarness(config)
  result  = harness.run(signal, returns, regime_mask, crisis_mask)
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from .portfolio_simulator import SimulationResult, simulate_portfolio
from .cost_model import CostModelConfig
from .slippage_model import SlippageModelConfig
from .risk_gate import RiskGateConfig, apply_risk_gate, GateTrigger


# ------------------------------------------------------------------ #
# 設定
# ------------------------------------------------------------------ #

@dataclass
class WalkForwardConfig:
    mode: str            = "expanding"   # "expanding" | "rolling"
    min_train_days: int  = 750
    step_days: int       = 5
    horizons: List[int]  = field(default_factory=lambda: [1, 5, 20])
    cost_config: CostModelConfig         = field(default_factory=CostModelConfig.from_env)
    slippage_config: SlippageModelConfig = field(default_factory=SlippageModelConfig.from_env)
    risk_config: RiskGateConfig          = field(default_factory=RiskGateConfig.from_env)
    rolling_window_days: int = 750       # rolling モード時の学習ウィンドウ

    @classmethod
    def from_env(cls) -> "WalkForwardConfig":
        raw_horizons = os.environ.get("EML_METRICS_HORIZONS", "1,5,20")
        horizons = [int(h) for h in raw_horizons.split(",") if h.strip()]
        return cls(
            mode=os.environ.get("EML_BACKTEST_MODE", "expanding"),
            min_train_days=int(os.environ.get("EML_BACKTEST_MIN_TRAIN_DAYS", "750")),
            step_days=int(os.environ.get("EML_BACKTEST_STEP_DAYS", "5")),
            horizons=horizons,
        )


# ------------------------------------------------------------------ #
# フォールド結果
# ------------------------------------------------------------------ #

@dataclass
class FoldResult:
    fold_id: str
    backtest_run_id: str
    fold_index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    horizon: int
    net_returns: pd.Series
    gross_returns: pd.Series
    position: pd.Series
    cost_series: pd.Series
    slippage_series: pd.Series
    gate_triggers: List[GateTrigger]
    turnover: float
    sharpe: float
    max_drawdown: float
    total_bars: int
    status: str = "ok"   # ok / gate_fired / insufficient_data


@dataclass
class BacktestRunResult:
    backtest_run_id: str
    run_id: str           # EML alpha run_id
    trace_id: str
    mode: str
    total_folds: int
    folds: List[FoldResult]
    combined_net_returns: pd.Series
    combined_gross_returns: pd.Series
    combined_position: pd.Series
    gate_trigger_count: int
    overall_sharpe: float
    overall_max_drawdown: float
    metadata: dict = field(default_factory=dict)


# ------------------------------------------------------------------ #
# ハーネス
# ------------------------------------------------------------------ #

class WalkForwardHarness:
    def __init__(self, config: Optional[WalkForwardConfig] = None):
        self.config = config or WalkForwardConfig.from_env()

    def run(
        self,
        signal: pd.Series,
        returns: pd.Series,
        run_id: str,
        trace_id: str,
        crisis_mask: Optional[pd.Series] = None,
        liquidity_mask: Optional[pd.Series] = None,
    ) -> BacktestRunResult:
        import math

        cfg = self.config
        backtest_run_id = str(uuid.uuid4())

        # アライン
        sig, ret = signal.align(returns, join="inner")
        sig = sig.dropna()
        ret = ret.reindex(sig.index).fillna(0.0)
        dates = sig.index.tolist()
        n = len(dates)

        if n < cfg.min_train_days + 1:
            # データ不足: 全期間でシングルフォールドを実行
            return self._single_fold_run(
                sig, ret, backtest_run_id, run_id, trace_id,
                crisis_mask, liquidity_mask,
            )

        folds: List[FoldResult] = []
        fold_idx = 0

        # フォールド生成
        test_start_i = cfg.min_train_days
        while test_start_i < n:
            test_end_i = min(test_start_i + cfg.step_days, n)

            if cfg.mode == "expanding":
                train_start_i = 0
            else:  # rolling
                train_start_i = max(0, test_start_i - cfg.rolling_window_days)

            train_dates = dates[train_start_i:test_start_i]
            test_dates  = dates[test_start_i:test_end_i]

            if len(test_dates) == 0:
                break

            test_sig = sig.reindex(test_dates)
            test_ret = ret.reindex(test_dates)

            # リスクゲート適用
            gate_sig, gate_triggers = apply_risk_gate(
                signal=test_sig,
                returns=test_ret,
                config=cfg.risk_config,
                crisis_mask=crisis_mask.reindex(test_dates, fill_value=False) if crisis_mask is not None else None,
                liquidity_mask=liquidity_mask.reindex(test_dates, fill_value=False) if liquidity_mask is not None else None,
            )

            # シミュレーション
            sim: SimulationResult = simulate_portfolio(
                signal=gate_sig,
                returns=test_ret,
                cost_config=cfg.cost_config,
                slippage_config=cfg.slippage_config,
            )

            # Sharpe, MDD
            sharpe = _sharpe(sim.net_returns)
            cum = (1 + sim.net_returns).cumprod()
            mdd = _mdd(cum)

            fold = FoldResult(
                fold_id=str(uuid.uuid4()),
                backtest_run_id=backtest_run_id,
                fold_index=fold_idx,
                train_start=str(dates[train_start_i]),
                train_end=str(dates[test_start_i - 1]),
                test_start=str(test_dates[0]),
                test_end=str(test_dates[-1]),
                horizon=cfg.step_days,
                net_returns=sim.net_returns,
                gross_returns=sim.gross_returns,
                position=sim.position,
                cost_series=sim.cost_series,
                slippage_series=sim.slippage_series,
                gate_triggers=gate_triggers,
                turnover=sim.turnover,
                sharpe=sharpe,
                max_drawdown=mdd,
                total_bars=len(test_dates),
                status="gate_fired" if gate_triggers else "ok",
            )
            folds.append(fold)

            fold_idx += 1
            test_start_i += cfg.step_days

        if not folds:
            return self._single_fold_run(
                sig, ret, backtest_run_id, run_id, trace_id,
                crisis_mask, liquidity_mask,
            )

        # 結合
        all_net = pd.concat([f.net_returns for f in folds]).sort_index()
        all_gross = pd.concat([f.gross_returns for f in folds]).sort_index()
        all_pos = pd.concat([f.position for f in folds]).sort_index()
        total_triggers = sum(len(f.gate_triggers) for f in folds)

        cum_combined = (1 + all_net).cumprod()

        return BacktestRunResult(
            backtest_run_id=backtest_run_id,
            run_id=run_id,
            trace_id=trace_id,
            mode=cfg.mode,
            total_folds=len(folds),
            folds=folds,
            combined_net_returns=all_net,
            combined_gross_returns=all_gross,
            combined_position=all_pos,
            gate_trigger_count=total_triggers,
            overall_sharpe=_sharpe(all_net),
            overall_max_drawdown=_mdd(cum_combined),
            metadata={
                "min_train_days": cfg.min_train_days,
                "step_days": cfg.step_days,
            },
        )

    def _single_fold_run(
        self,
        sig: pd.Series,
        ret: pd.Series,
        backtest_run_id: str,
        run_id: str,
        trace_id: str,
        crisis_mask: Optional[pd.Series],
        liquidity_mask: Optional[pd.Series],
    ) -> BacktestRunResult:
        cfg = self.config
        gate_sig, triggers = apply_risk_gate(
            signal=sig, returns=ret, config=cfg.risk_config,
            crisis_mask=crisis_mask, liquidity_mask=liquidity_mask,
        )
        sim = simulate_portfolio(gate_sig, ret, cfg.cost_config, cfg.slippage_config)
        cum = (1 + sim.net_returns).cumprod()
        dates = sig.index.tolist()

        fold = FoldResult(
            fold_id=str(uuid.uuid4()),
            backtest_run_id=backtest_run_id,
            fold_index=0,
            train_start=str(dates[0]) if dates else "",
            train_end=str(dates[-1]) if dates else "",
            test_start=str(dates[0]) if dates else "",
            test_end=str(dates[-1]) if dates else "",
            horizon=cfg.step_days,
            net_returns=sim.net_returns,
            gross_returns=sim.gross_returns,
            position=sim.position,
            cost_series=sim.cost_series,
            slippage_series=sim.slippage_series,
            gate_triggers=triggers,
            turnover=sim.turnover,
            sharpe=_sharpe(sim.net_returns),
            max_drawdown=_mdd(cum),
            total_bars=len(dates),
            status="gate_fired" if triggers else "ok",
        )

        return BacktestRunResult(
            backtest_run_id=backtest_run_id,
            run_id=run_id,
            trace_id=trace_id,
            mode=cfg.mode,
            total_folds=1,
            folds=[fold],
            combined_net_returns=sim.net_returns,
            combined_gross_returns=sim.gross_returns,
            combined_position=sim.position,
            gate_trigger_count=len(triggers),
            overall_sharpe=fold.sharpe,
            overall_max_drawdown=fold.max_drawdown,
            metadata={"single_fold": True},
        )


# ------------------------------------------------------------------ #
# ヘルパー
# ------------------------------------------------------------------ #

def _sharpe(r: pd.Series, ann: int = 252) -> float:
    import math
    r = r.dropna()
    if len(r) < 5 or r.std() == 0:
        return 0.0
    return float(r.mean() / r.std() * math.sqrt(ann))


def _mdd(cum: pd.Series) -> float:
    import numpy as np
    peak = cum.cummax()
    dd = (cum - peak) / peak.replace(0, np.nan)
    return float(dd.min()) if not dd.empty else 0.0
