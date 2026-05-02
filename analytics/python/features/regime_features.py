"""
regime_features.py
------------------
レジーム特徴量: crisis / low_liquidity / high_vol マスクを生成。

crisis レジームの定義:
  - realized_vol_20 が long_vol_threshold 以上
  OR
  - drawdown が drawdown_threshold 以下

low_liquidity の定義:
  - volume が rolling 平均の liquidity_ratio 以下
"""
from __future__ import annotations

import os
from typing import Optional

import numpy as np
import pandas as pd


def build_crisis_mask(
    returns: pd.Series,
    vol_threshold: Optional[float] = None,
    drawdown_threshold: Optional[float] = None,
    vol_window: int = 20,
) -> pd.Series:
    """
    crisis regime マスクを生成 (True = crisis)。

    Parameters
    ----------
    returns            : 日次リターン
    vol_threshold      : 年率ボラが この値以上 → crisis (デフォルト: 0.30)
    drawdown_threshold : この DD 以下 → crisis (デフォルト: -0.15)
    vol_window         : ボラ計算ウィンドウ
    """
    if vol_threshold is None:
        vol_threshold = float(os.environ.get("EML_CRISIS_VOL_THRESHOLD", "0.30"))
    if drawdown_threshold is None:
        drawdown_threshold = float(os.environ.get("EML_CRISIS_DD_THRESHOLD", "-0.15"))

    ann_factor = (252 ** 0.5)
    rolling_vol = returns.rolling(vol_window).std() * ann_factor

    cum = (1 + returns).cumprod()
    peak = cum.cummax()
    dd = (cum - peak) / peak.replace(0, np.nan)

    crisis_vol = rolling_vol >= vol_threshold
    crisis_dd  = dd <= drawdown_threshold

    mask = (crisis_vol | crisis_dd).fillna(False)
    return mask


def build_low_liquidity_mask(
    volume: pd.Series,
    liquidity_ratio: float = 0.5,
    vol_window: int = 20,
) -> pd.Series:
    """
    low liquidity マスクを生成 (True = low liquidity)。
    volume が rolling 平均 × liquidity_ratio 以下の日。
    """
    rolling_avg = volume.rolling(vol_window).mean()
    mask = (volume < rolling_avg * liquidity_ratio).fillna(False)
    return mask


def build_high_vol_mask(
    returns: pd.Series,
    quantile: float = 0.75,
    vol_window: int = 20,
) -> pd.Series:
    """
    high vol マスク (rolling vol の上位 quantile 以上の期間)。
    """
    rolling_vol = returns.rolling(vol_window).std()
    threshold   = rolling_vol.quantile(quantile)
    return (rolling_vol >= threshold).fillna(False)


def build_regime_features(
    returns: pd.Series,
    volume: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """
    全レジームマスクを DataFrame にまとめて返す。

    Columns: crisis, low_liquidity, high_vol
    """
    feat = pd.DataFrame(index=returns.index)
    feat["crisis"] = build_crisis_mask(returns).astype(int)

    if volume is not None:
        feat["low_liquidity"] = build_low_liquidity_mask(volume).astype(int)
    else:
        feat["low_liquidity"] = 0

    feat["high_vol"] = build_high_vol_mask(returns).astype(int)

    return feat
