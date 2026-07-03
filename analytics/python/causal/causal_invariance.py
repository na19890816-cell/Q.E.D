"""
causal_invariance.py
--------------------
不変性検定（Invariance Test）モジュール。

因果関係の特徴のひとつは「介入や環境変化に対して不変」であること。
本モジュールは、複数レジーム/サブサンプルにわたって
X → Return の関係が安定しているかを検定する。

指標:
  - invariance_pass_ratio: 全レジームで有意な正関係を持つ割合
  - coefficient_stability: 各レジームの回帰係数の変動係数
  - regime_consistency_score: レジーム間の整合性

環境変数: CAUSAL_INVARIANCE_MIN_PASS_RATIO=0.70
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

_INVARIANCE_MIN_PASS_RATIO: float = float(
    os.environ.get("CAUSAL_INVARIANCE_MIN_PASS_RATIO", "0.70")
)


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class RegimeTestResult:
    """単一レジームでの不変性テスト結果。"""
    regime_name: str
    n_obs: int
    beta: float
    alpha: float
    r_squared: float
    correlation: float
    is_positive: bool   # beta > 0
    is_significant: bool  # |correlation| > threshold

    def to_dict(self) -> Dict:
        return {
            "regime_name": self.regime_name,
            "n_obs": self.n_obs,
            "beta": self.beta,
            "alpha": self.alpha,
            "r_squared": self.r_squared,
            "correlation": self.correlation,
            "is_positive": self.is_positive,
            "is_significant": self.is_significant,
        }


@dataclass
class InvarianceResult:
    """
    不変性検定の全体結果。

    Attributes
    ----------
    invariance_pass_ratio : float
        通過したレジーム数 / 全レジーム数 (0〜1)
    coefficient_stability : float
        回帰係数の安定性スコア（0〜1）
    regime_consistency_score : float
        レジーム間整合性スコア（0〜1）
    regime_results : list[RegimeTestResult]
    n_regimes_tested : int
    n_regimes_passed : int
    gate_pass : bool
    gate_reason : str
    """
    invariance_pass_ratio: float
    coefficient_stability: float
    regime_consistency_score: float
    regime_results: List[RegimeTestResult]
    n_regimes_tested: int
    n_regimes_passed: int
    gate_pass: bool
    gate_reason: str

    def to_dict(self) -> Dict:
        return {
            "invariance_pass_ratio": self.invariance_pass_ratio,
            "coefficient_stability": self.coefficient_stability,
            "regime_consistency_score": self.regime_consistency_score,
            "n_regimes_tested": self.n_regimes_tested,
            "n_regimes_passed": self.n_regimes_passed,
            "gate_pass": self.gate_pass,
            "gate_reason": self.gate_reason,
            "regime_results": [r.to_dict() for r in self.regime_results],
        }


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _simple_ols(x: List[float], y: List[float]) -> Tuple[float, float, float, float]:
    """
    単回帰 y = alpha + beta * x。
    Returns (alpha, beta, r_squared, correlation)
    """
    n = len(x)
    if n < 3:
        return 0.0, 0.0, 0.0, 0.0

    mx = sum(x) / n
    my = sum(y) / n
    ss_xx = sum((xi - mx) ** 2 for xi in x)
    ss_xy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    ss_yy = sum((yi - my) ** 2 for yi in y)

    if ss_xx < 1e-15:
        return my, 0.0, 0.0, 0.0

    beta = ss_xy / ss_xx
    alpha = my - beta * mx

    ss_res = sum((yi - (alpha + beta * xi)) ** 2 for xi, yi in zip(x, y))
    r2 = max(0.0, min(1.0, 1.0 - ss_res / ss_yy)) if ss_yy > 1e-15 else 0.0

    # 相関
    denom = math.sqrt(ss_xx * ss_yy)
    corr = max(-1.0, min(1.0, ss_xy / denom)) if denom > 1e-15 else 0.0

    return alpha, beta, r2, corr


def _split_into_regimes(
    signal: List[float],
    returns: List[float],
    n_regimes: int = 4,
) -> List[Tuple[str, List[float], List[float]]]:
    """
    時系列を n 個のサブサンプルに分割してレジームとして扱う。

    Returns list of (regime_name, signal_sub, returns_sub)
    """
    n = min(len(signal), len(returns))
    if n < n_regimes * 5:
        n_regimes = max(2, n // 5)

    chunk = n // n_regimes
    regimes = []
    for i in range(n_regimes):
        start = i * chunk
        end = (i + 1) * chunk if i < n_regimes - 1 else n
        name = f"regime_{i+1}_of_{n_regimes}"
        regimes.append((name, signal[start:end], returns[start:end]))

    return regimes


# ---------------------------------------------------------------------------
# 不変性検定
# ---------------------------------------------------------------------------

def compute_invariance(
    signal: List[float],
    returns: List[float],
    regime_masks: Optional[Dict[str, List[bool]]] = None,
    n_regimes: int = 4,
    significance_threshold: float = 0.05,
    min_pass_ratio: float = _INVARIANCE_MIN_PASS_RATIO,
) -> InvarianceResult:
    """
    X → Return の不変性を複数レジームにわたって検定する。

    Parameters
    ----------
    signal : list[float]
    returns : list[float]
    regime_masks : dict[str, list[bool]], optional
        レジーム名 → bool マスク。None なら時系列を等分割する。
    n_regimes : int
        時系列分割数（regime_masks がない場合）
    significance_threshold : float
        有意性判定の相関係数閾値
    min_pass_ratio : float
        gate 通過最低不変性比率

    Returns
    -------
    InvarianceResult
    """
    if not _CAUSAL_ENABLED:
        return _disabled_invariance_result()

    n = min(len(signal), len(returns))
    if n < 20:
        return _insufficient_data_invariance(n)

    # レジーム分割
    if regime_masks:
        regimes = []
        for name, mask in regime_masks.items():
            sig_sub = [signal[i] for i in range(min(n, len(mask))) if mask[i]]
            ret_sub = [returns[i] for i in range(min(n, len(mask))) if mask[i]]
            regimes.append((name, sig_sub, ret_sub))
    else:
        regimes = _split_into_regimes(signal[:n], returns[:n], n_regimes=n_regimes)

    # 各レジームで OLS
    regime_results: List[RegimeTestResult] = []
    for regime_name, sig_sub, ret_sub in regimes:
        if len(sig_sub) < 3:
            continue
        alpha, beta, r2, corr = _simple_ols(sig_sub, ret_sub)
        is_positive = beta > 0
        is_significant = abs(corr) >= significance_threshold
        regime_results.append(RegimeTestResult(
            regime_name=regime_name,
            n_obs=len(sig_sub),
            beta=beta,
            alpha=alpha,
            r_squared=r2,
            correlation=corr,
            is_positive=is_positive,
            is_significant=is_significant,
        ))

    if not regime_results:
        return _insufficient_data_invariance(n)

    # 通過率（正かつ有意）
    passed = [r for r in regime_results if r.is_positive and r.is_significant]
    pass_ratio = len(passed) / len(regime_results)

    # 回帰係数の安定性（変動係数の逆数）— pure Python (ADR-001 準拠)
    betas = [r.beta for r in regime_results]
    if len(betas) >= 2:
        n_b = len(betas)
        mean_beta = sum(betas) / n_b
        # 母標準偏差 (pstdev: 分母 n)
        std_beta = math.sqrt(sum((b - mean_beta) ** 2 for b in betas) / n_b)
        cv = std_beta / abs(mean_beta) if abs(mean_beta) > 1e-10 else std_beta
        coeff_stability = max(0.0, min(1.0, 1.0 - cv))
    else:
        coeff_stability = 1.0 if (betas and betas[0] > 0) else 0.0

    # レジーム整合性スコア（全レジームで同じ方向なら高）
    positive_count = sum(1 for r in regime_results if r.is_positive)
    regime_consistency = positive_count / len(regime_results)

    # gate 判定
    gate_pass = pass_ratio >= min_pass_ratio
    gate_reason = (
        f"invariance_pass_ratio={pass_ratio:.4f} >= min={min_pass_ratio:.4f}: OK"
        if gate_pass else
        f"invariance_pass_ratio={pass_ratio:.4f} < min={min_pass_ratio:.4f}: FAIL"
    )

    return InvarianceResult(
        invariance_pass_ratio=pass_ratio,
        coefficient_stability=coeff_stability,
        regime_consistency_score=regime_consistency,
        regime_results=regime_results,
        n_regimes_tested=len(regime_results),
        n_regimes_passed=len(passed),
        gate_pass=gate_pass,
        gate_reason=gate_reason,
    )


def _disabled_invariance_result() -> InvarianceResult:
    return InvarianceResult(
        invariance_pass_ratio=1.0, coefficient_stability=1.0,
        regime_consistency_score=1.0, regime_results=[], n_regimes_tested=0,
        n_regimes_passed=0, gate_pass=True,
        gate_reason="CAUSAL_DISCOVERY_ENABLED=0: スキップ",
    )


def _insufficient_data_invariance(n: int) -> InvarianceResult:
    return InvarianceResult(
        invariance_pass_ratio=0.5, coefficient_stability=0.5,
        regime_consistency_score=0.5, regime_results=[], n_regimes_tested=0,
        n_regimes_passed=0, gate_pass=True,
        gate_reason=f"データ不足 n={n}: 不変性評価スキップ",
    )
