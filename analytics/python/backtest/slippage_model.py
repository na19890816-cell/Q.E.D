"""
slippage_model.py
-----------------
スリッページモデル: 片道スリッページ (bps) をポジション変化量に乗じる。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import pandas as pd


@dataclass
class SlippageModelConfig:
    slippage_bps: float = 2.0

    @classmethod
    def from_env(cls) -> "SlippageModelConfig":
        return cls(
            slippage_bps=float(os.environ.get("EML_BACKTEST_SLIPPAGE_BPS", "2.0")),
        )


def apply_slippage(
    position: pd.Series,
    config: SlippageModelConfig | None = None,
) -> pd.Series:
    """
    スリッページコスト系列を返す。

    Parameters
    ----------
    position : ポジションサイズ [-1, 1]
    config   : None のとき環境変数から生成
    """
    if config is None:
        config = SlippageModelConfig.from_env()
    delta = position.diff().abs().fillna(0.0)
    return delta * config.slippage_bps * 1e-4
