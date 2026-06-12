"""
causal_direction.py
-------------------
因果方向性スコア（causal_direction_score）計算モジュール。

X → Return（シグナルがリターンを引き起こす）方向の証拠を
複数の統計的指標で計算する。

指標:
  1. Granger 方向性スコア（単純版: ラグ相関の方向性）
  2. 時間非対称相関（先行 vs 遅行の比較）
  3. 残差相関の比較

設計原則:
  - pure Python（numpy/scipy不使用）
  - 近似実装（軽量重視）
  - 0〜1 スコアで返す（高いほど X→Return の証拠が強い）
  - 環境変数: CAUSAL_DISCOVERY_ENABLED=1, CAUSAL_DIRECTION_MIN_SCORE=0.60
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 定数・環境変数
# ---------------------------------------------------------------------------

_CAUSAL_ENABLED: bool = os.environ.get(
    "CAUSAL_DISCOVERY_ENABLED", "1"
).strip().lower() in ("1", "true", "yes", "on")

_CAUSAL_DIR_MIN_SCORE: float = float(
    os.environ.get("CAUSAL_DIRECTION_MIN_SCORE", "0.60")
)


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class CausalDirectionResult:
    """
    因果方向性スコアの計算結果。

    Attributes
    ----------
    causal_direction_score : float
        X→Return の方向性スコア（0〜1）
    forward_correlation : float
        X[t] と Return[t+lag] の相関（先行相関）
    backward_correlation : float
        Return[t] と X[t+lag] の相関（遅行相関）
    direction_asymmetry : float
        forward - backward（正なら X→R が強い）
    granger_proxy_score : float
        Granger 方向性の近似スコア（0〜1）
    lag_used : int
    n_obs : int
    gate_pass : bool
    gate_reason : str
    """
    causal_direction_score: float
    forward_correlation: float
    backward_correlation: float
    direction_asymmetry: float
    granger_proxy_score: float
    lag_used: int
    n_obs: int
    gate_pass: bool
    gate_reason: str

    def to_dict(self) -> Dict:
        return {
            "causal_direction_score": self.causal_direction_score,
            "forward_correlation": self.forward_correlation,
            "backward_correlation": self.backward_correlation,
            "direction_asymmetry": self.direction_asymmetry,
            "granger_proxy_score": self.granger_proxy_score,
            "lag_used": self.lag_used,
            "n_obs": self.n_obs,
            "gate_pass": self.gate_pass,
            "gate_reason": self.gate_reason,
        }


# ---------------------------------------------------------------------------
# 統計ユーティリティ
# ---------------------------------------------------------------------------

def _pearson(x: List[float], y: List[float]) -> float:
    """ピアソン相関係数（pure Python）。"""
    n = len(x)
    if n < 3:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    sx = math.sqrt(sum((xi - mx) ** 2 for xi in x))
    sy = math.sqrt(sum((yi - my) ** 2 for yi in y))
    if sx < 1e-15 or sy < 1e-15:
        return 0.0
    return max(-1.0, min(1.0, num / (sx * sy)))


def _lagged_correlation(
    x: List[float],
    y: List[float],
    lag: int,
) -> float:
    """
    x[t] と y[t + lag] の相関を計算する。

    lag > 0 → x が y より先行
    lag < 0 → y が x より先行
    """
    if lag == 0:
        return _pearson(x, y)
    n = min(len(x), len(y))
    if abs(lag) >= n:
        return 0.0
    if lag > 0:
        xi = x[:n - lag]
        yi = y[lag:n]
    else:
        xi = x[-lag:n]
        yi = y[:n + lag]
    return _pearson(xi, yi)


# ---------------------------------------------------------------------------
# 因果方向性スコア計算
# ---------------------------------------------------------------------------

def compute_causal_direction(
    signal: List[float],
    returns: List[float],
    lag: int = 1,
    min_score: float = _CAUSAL_DIR_MIN_SCORE,
) -> CausalDirectionResult:
    """
    X → Return の因果方向性スコアを計算する。

    Parameters
    ----------
    signal : list[float]
        候補シグナル系列 (X)
    returns : list[float]
        リターン系列 (Y)
    lag : int
        評価ラグ（デフォルト 1 期）
    min_score : float
        gate 通過最低スコア

    Returns
    -------
    CausalDirectionResult
    """
    if not _CAUSAL_ENABLED:
        return _disabled_direction_result()

    n = min(len(signal), len(returns))
    if n < 10:
        return _insufficient_data_direction(n)

    # 先行相関: signal[t] → returns[t+lag]（信号が先行してリターンを予測）
    fwd_corr = _lagged_correlation(signal, returns, lag=lag)

    # 遅行相関: returns[t] → signal[t+lag]（リターンが先行してシグナルを予測）
    bwd_corr = _lagged_correlation(returns, signal, lag=lag)

    # 方向非対称性（正 → X→R の証拠）
    direction_asym = abs(fwd_corr) - abs(bwd_corr)

    # Granger 近似スコア: 遅行の abs を引いた先行の強さ（0〜1）
    granger_proxy = max(0.0, min(1.0, (abs(fwd_corr) + direction_asym / 2) / max(0.01, abs(fwd_corr) + abs(bwd_corr) + 0.01)))

    # 総合スコア（先行相関の絶対値 + 方向非対称性ボーナス）
    base = min(1.0, abs(fwd_corr) / 0.20)  # fwd_corr=0.20 → score=1.0
    asym_bonus = max(0.0, direction_asym) * 0.5
    causal_score = min(1.0, base + asym_bonus)

    # gate 判定
    gate_pass = causal_score >= min_score
    gate_reason = (
        f"causal_direction_score={causal_score:.4f} >= min={min_score:.4f}: OK"
        if gate_pass else
        f"causal_direction_score={causal_score:.4f} < min={min_score:.4f}: FAIL"
    )

    return CausalDirectionResult(
        causal_direction_score=causal_score,
        forward_correlation=fwd_corr,
        backward_correlation=bwd_corr,
        direction_asymmetry=direction_asym,
        granger_proxy_score=granger_proxy,
        lag_used=lag,
        n_obs=n,
        gate_pass=gate_pass,
        gate_reason=gate_reason,
    )


def _disabled_direction_result() -> CausalDirectionResult:
    return CausalDirectionResult(
        causal_direction_score=1.0, forward_correlation=0.0,
        backward_correlation=0.0, direction_asymmetry=0.0,
        granger_proxy_score=1.0, lag_used=1, n_obs=0,
        gate_pass=True, gate_reason="CAUSAL_DISCOVERY_ENABLED=0: スキップ",
    )


def _insufficient_data_direction(n: int) -> CausalDirectionResult:
    return CausalDirectionResult(
        causal_direction_score=0.5, forward_correlation=0.0,
        backward_correlation=0.0, direction_asymmetry=0.0,
        granger_proxy_score=0.5, lag_used=1, n_obs=n,
        gate_pass=True, gate_reason=f"データ不足 n={n}: 因果方向評価スキップ",
    )
