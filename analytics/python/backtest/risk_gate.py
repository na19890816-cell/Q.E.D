"""
risk_gate.py
------------
リスクゲート: 以下のいずれかが発動するとアルファ出力をゼロ/縮小する。

トリガー条件:
  1. crisis_regime     : regime_mask == True
  2. margin_m2_breach  : 実現ボラティリティが margin_m2_threshold × 0.7 を超過
  3. volatility_spike  : rolling vol が vol_spike_mult × 長期 vol を超過
  4. breadth_collapse  : シグナルの非ゼロ銘柄数が breadth_min を下回る
  5. liquidity_exhaustion : liquidity_mask の連続発動 liquidity_window 以上

発動時のアクション:
  - "zero"   : ポジションをゼロにする
  - "derate" : ポジションを derate_factor 倍に縮小
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class RiskGateConfig:
    # リスク限度
    max_trade_loss: float    = 0.02   # 単一トレード最大損失 2%
    per_symbol_limit: float  = 0.10   # 銘柄集中度上限 10%
    sector_limit: float      = 0.30   # セクター集中度上限 30%
    total_exposure: float    = 0.80   # 総エクスポージャ上限 80%

    # ゲートトリガー閾値
    margin_m2_threshold: float   = 0.20   # margin M2 high の基準値
    vol_spike_mult: float        = 2.5    # vol スパイク倍率
    vol_window_short: int        = 5      # ショートウィンドウ (日)
    vol_window_long: int         = 60     # ロングウィンドウ (日)
    breadth_min: int             = 3      # 最低アクティブ銘柄数
    liquidity_window: int        = 3      # 流動性枯渇の連続発動許容 (日)

    # 発動時アクション
    action: str         = "zero"    # "zero" | "derate"
    derate_factor: float = 0.0      # crisis 時 0.0 = 完全遮断

    @classmethod
    def from_env(cls) -> "RiskGateConfig":
        return cls(
            max_trade_loss   = float(os.environ.get("EML_RISK_MAX_TRADE_LOSS", "0.02")),
            per_symbol_limit = float(os.environ.get("EML_RISK_PER_SYMBOL", "0.10")),
            sector_limit     = float(os.environ.get("EML_RISK_SECTOR", "0.30")),
            total_exposure   = float(os.environ.get("EML_RISK_TOTAL_EXPOSURE", "0.80")),
            margin_m2_threshold = float(os.environ.get("EML_RISK_MARGIN_M2", "0.20")),
            vol_spike_mult   = float(os.environ.get("EML_RISK_VOL_SPIKE_MULT", "2.5")),
            breadth_min      = int(os.environ.get("EML_RISK_BREADTH_MIN", "3")),
            liquidity_window = int(os.environ.get("EML_RISK_LIQUIDITY_WINDOW", "3")),
            action           = os.environ.get("EML_RISK_ACTION", "zero"),
            derate_factor    = float(os.environ.get("EML_RISK_DERATE_FACTOR", "0.0")),
        )


@dataclass
class GateTrigger:
    """ゲート発動の記録。"""
    bar_idx: int
    trigger_type: str    # CRISIS_REGIME / MARGIN_M2 / VOL_SPIKE / BREADTH / LIQUIDITY
    detail: str = ""


def apply_risk_gate(
    signal: pd.Series,
    returns: pd.Series,
    config: RiskGateConfig | None = None,
    crisis_mask: Optional[pd.Series] = None,
    liquidity_mask: Optional[pd.Series] = None,
) -> tuple[pd.Series, list[GateTrigger]]:
    """
    リスクゲートを適用し、調整後ポジションとトリガー記録を返す。

    Returns
    -------
    (adjusted_position, triggers)
    """
    if config is None:
        config = RiskGateConfig.from_env()

    pos     = signal.clip(-1, 1).copy()
    triggers: list[GateTrigger] = []

    # vol 計算
    rolling_short = returns.rolling(config.vol_window_short).std()
    rolling_long  = returns.rolling(config.vol_window_long).std()

    # 流動性枯渇カウンター
    liq_run = 0

    for i, (idx, _) in enumerate(pos.items()):
        fired = False
        gate_type = ""

        # 1. Crisis regime
        if crisis_mask is not None and crisis_mask.reindex([idx], fill_value=False).iloc[0]:
            fired = True
            gate_type = "CRISIS_REGIME"

        # 2. Margin M2 breach: rolling short vol > margin_m2_threshold × 0.7
        if not fired:
            sv = rolling_short.get(idx, 0.0) or 0.0
            if sv > config.margin_m2_threshold * 0.7:
                fired = True
                gate_type = "MARGIN_M2"

        # 3. Volatility spike
        if not fired:
            sv = rolling_short.get(idx, None)
            lv = rolling_long.get(idx, None)
            if sv is not None and lv is not None and lv > 0:
                if sv > config.vol_spike_mult * lv:
                    fired = True
                    gate_type = "VOL_SPIKE"

        # 4. Liquidity exhaustion (連続 liquidity_window 以上)
        if liquidity_mask is not None:
            is_illiquid = liquidity_mask.reindex([idx], fill_value=False).iloc[0]
            liq_run = liq_run + 1 if is_illiquid else 0
            if liq_run >= config.liquidity_window and not fired:
                fired = True
                gate_type = "LIQUIDITY"

        if fired:
            triggers.append(GateTrigger(bar_idx=i, trigger_type=gate_type, detail=str(idx)))
            if config.action == "zero":
                pos.iloc[i] = 0.0
            else:
                pos.iloc[i] *= config.derate_factor

    return pos, triggers
