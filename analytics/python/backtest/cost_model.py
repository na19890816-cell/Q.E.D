"""
cost_model.py
-------------
コストモデル: 片道コスト (bps) を環境変数から取得し、
ポジション変化量に乗じてコスト系列を返す。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import pandas as pd


@dataclass
class CostModelConfig:
    cost_bps: float = 2.0   # 片道コスト (basis points)

    @classmethod
    def from_env(cls) -> "CostModelConfig":
        return cls(
            cost_bps=float(os.environ.get("EML_BACKTEST_COST_BPS", "2.0")),
        )


def apply_cost(
    position: pd.Series,
    config: CostModelConfig | None = None,
) -> pd.Series:
    """
    ターンオーバーに基づくコスト系列を返す。

    Parameters
    ----------
    position : ポジションサイズ [-1, 1]
    config   : None のとき環境変数から生成
    """
    if config is None:
        config = CostModelConfig.from_env()
    delta = position.diff().abs().fillna(0.0)
    return delta * config.cost_bps * 1e-4
