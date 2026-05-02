"""
build_terminal_set.py
---------------------
ターミナルセット構築: event_study_summaries から
技術指標・イベント特徴量を計算し DataFrame を生成する。

Terminal set (12-20 indicators):
  r1, r5, r20, gap, range_norm, atr_norm, realized_vol_20,
  vol_surprise, bb_width, squeeze_flag, ppo, trend_slope_20,
  event_pre_drift_20, event_surprise, sector_residual, market_beta
"""
from __future__ import annotations

import os
from typing import List, Optional

import numpy as np
import pandas as pd


# デフォルトターミナルセット
DEFAULT_TERMINALS: List[str] = [
    "r1", "r5", "r20",
    "gap", "range_norm", "atr_norm",
    "realized_vol_20", "vol_surprise",
    "bb_width", "squeeze_flag",
    "ppo", "trend_slope_20",
    "event_pre_drift_20", "event_surprise",
    "sector_residual", "market_beta",
]


def get_terminal_set_from_env() -> List[str]:
    """環境変数 EML_ALPHA_TERMINAL_SET からターミナルセットを取得。"""
    raw = os.environ.get("EML_ALPHA_TERMINAL_SET", "")
    if raw.strip():
        return [t.strip() for t in raw.split(",") if t.strip()]
    return DEFAULT_TERMINALS


def build_terminal_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    event_study_summaries 形式の DataFrame からターミナル特徴量を構築。

    期待する入力列:
      date, symbol (任意), metric (異常収益率 = AR),
      その他 OHLCV 的な列 (open, high, low, close, volume 等) があれば使用

    Parameters
    ----------
    df : 入力 DataFrame

    Returns
    -------
    features : ターミナル列を持つ DataFrame (同じインデックス)
    """
    feat = pd.DataFrame(index=df.index)

    # --- リターン系 ---
    if "metric" in df.columns:
        ar = df["metric"].astype(float)
    elif "close" in df.columns:
        ar = df["close"].pct_change()
    else:
        ar = pd.Series(0.0, index=df.index)

    feat["r1"]  = ar
    feat["r5"]  = ar.rolling(5).sum()
    feat["r20"] = ar.rolling(20).sum()

    # --- Gap / Range ---
    if "open" in df.columns and "close" in df.columns:
        prev_close = df["close"].shift(1)
        feat["gap"] = (df["open"] - prev_close) / prev_close.replace(0, np.nan)
    else:
        feat["gap"] = ar.shift(1)

    if "high" in df.columns and "low" in df.columns and "close" in df.columns:
        rng = df["high"] - df["low"]
        feat["range_norm"] = rng / df["close"].replace(0, np.nan)
    else:
        feat["range_norm"] = ar.abs()

    # --- ATR / Vol ---
    if "high" in df.columns and "low" in df.columns and "close" in df.columns:
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"]  - df["close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr14 = tr.rolling(14).mean()
        feat["atr_norm"] = atr14 / df["close"].replace(0, np.nan)
    else:
        feat["atr_norm"] = ar.abs().rolling(14).mean()

    feat["realized_vol_20"] = ar.rolling(20).std()
    vol_long  = ar.rolling(60).std()
    feat["vol_surprise"] = (
        feat["realized_vol_20"] / vol_long.replace(0, np.nan) - 1.0
    )

    # --- Bollinger Band width ---
    bb_mid = ar.rolling(20).mean()
    bb_std = ar.rolling(20).std()
    feat["bb_width"]    = 2 * bb_std / bb_mid.abs().replace(0, np.nan)
    feat["squeeze_flag"] = (feat["bb_width"] < feat["bb_width"].rolling(50).mean()).astype(float)

    # --- PPO (Percentage Price Oscillator) ---
    ema12 = ar.ewm(span=12, adjust=False).mean()
    ema26 = ar.ewm(span=26, adjust=False).mean()
    feat["ppo"] = (ema12 - ema26) / ema26.abs().replace(0, np.nan)

    # --- Trend slope 20 ---
    def _rolling_slope(s: pd.Series, w: int) -> pd.Series:
        slopes = []
        arr = s.values
        for i in range(len(arr)):
            if i < w - 1:
                slopes.append(np.nan)
            else:
                y = arr[i - w + 1 : i + 1]
                x = np.arange(w)
                mask = ~np.isnan(y)
                if mask.sum() < 3:
                    slopes.append(np.nan)
                else:
                    p = np.polyfit(x[mask], y[mask], 1)
                    slopes.append(p[0])
        return pd.Series(slopes, index=s.index)

    feat["trend_slope_20"] = _rolling_slope(ar, 20)

    # --- Event pre-drift 20 ---
    feat["event_pre_drift_20"] = ar.rolling(20).sum().shift(1)

    # --- Event surprise (異常リターンからベースラインを引いた値) ---
    baseline = ar.rolling(60).mean()
    feat["event_surprise"] = ar - baseline

    # --- Sector residual & Market beta (プロキシ) ---
    mkt_beta_proxy = ar.rolling(60).corr(ar.rolling(60).mean())
    feat["market_beta"] = mkt_beta_proxy.fillna(1.0)

    # sector_residual = AR - market_beta * market_return
    mkt_ret = ar.rolling(5).mean()
    feat["sector_residual"] = ar - feat["market_beta"] * mkt_ret

    # NaN 埋め (0)
    feat = feat.fillna(0.0)

    return feat


def select_terminals(
    features: pd.DataFrame,
    terminals: Optional[List[str]] = None,
) -> pd.DataFrame:
    """
    features から指定されたターミナル列を抽出。
    存在しない列は 0 で補完。
    """
    if terminals is None:
        terminals = get_terminal_set_from_env()
    result = pd.DataFrame(index=features.index)
    for t in terminals:
        if t in features.columns:
            result[t] = features[t]
        else:
            result[t] = 0.0
    return result
