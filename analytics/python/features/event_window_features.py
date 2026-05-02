"""
event_window_features.py
------------------------
イベントウィンドウ特徴量: イベント周辺 [-pre, +post] の特徴量を抽出。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def build_event_window_mask(
    index: pd.Index,
    event_dates: pd.Index,
    pre_days: int = 5,
    post_days: int = 20,
) -> pd.Series:
    """
    event_dates 周辺 [-pre_days, +post_days] を True とするマスクを返す。

    Parameters
    ----------
    index       : 全期間インデックス
    event_dates : イベント発生日インデックス
    pre_days    : イベント前日数
    post_days   : イベント後日数
    """
    mask = pd.Series(False, index=index)
    sorted_idx = sorted(index.tolist())
    idx_pos = {d: i for i, d in enumerate(sorted_idx)}

    for ed in event_dates:
        if ed not in idx_pos:
            continue
        pos = idx_pos[ed]
        lo = max(0, pos - pre_days)
        hi = min(len(sorted_idx) - 1, pos + post_days)
        for p in range(lo, hi + 1):
            mask.iloc[p] = True

    return mask


def extract_event_window_features(
    feature_df: pd.DataFrame,
    event_mask: pd.Series,
) -> pd.DataFrame:
    """
    event_mask が True の行のみを抽出する。
    """
    return feature_df[event_mask.reindex(feature_df.index, fill_value=False)]


def compute_pre_event_drift(
    returns: pd.Series,
    event_mask: pd.Series,
    pre_window: int = 20,
) -> pd.Series:
    """
    イベント前 pre_window 日間の累積リターン (ドリフト) を返す。
    event_mask が True の日について backward fill で返す。
    """
    pre_drift = returns.rolling(pre_window).sum()
    drift_at_event = pre_drift.where(event_mask, other=np.nan)
    return drift_at_event.fillna(0.0)
