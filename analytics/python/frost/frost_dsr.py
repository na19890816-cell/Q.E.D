"""
frost_dsr.py
------------
Phase 4b (G1): Deflated Sharpe Ratio (DSR) — 純 Python 実装

## 背景 (NOTE-001 / 憲法ギャップ G1)

設計憲法は「シグナル採用前の Deflated Sharpe Ratio 算出」を義務付けている。
単純な Sharpe Ratio は N 個の候補を探索して最良のものを選ぶ選択バイアスを
補正しないため、多重試行下では楽観的に偏る。DSR はその探索コスト N と
リターン分布の非正規性 (歪度・尖度) を同時に補正する。

## 数理仕様 (Bailey & López de Prado 2014)

    SR^    : 観測 Sharpe Ratio (非年率・1 期間あたり)
    T      : 観測数
    g3     : 歪度 (skewness)
    g4     : 尖度 (kurtosis, 非超過。正規分布 = 3)

    Probabilistic Sharpe Ratio:
        PSR(SR*) = Φ( (SR^ - SR*) * sqrt(T - 1)
                      / sqrt(1 - g3*SR^ + (g4 - 1)/4 * SR^²) )

    期待最大 SR (N 個の独立試行の下での SR の期待最大値):
        SR0 = sqrt(V[SR]) * ( (1 - γ) Φ⁻¹(1 - 1/N) + γ Φ⁻¹(1 - 1/(N e)) )
        γ   = Euler–Mascheroni 定数 ≈ 0.5772156649

    Deflated Sharpe Ratio:
        DSR = PSR(SR0)                 ∈ [0, 1]

  - N = 1 のとき SR0 = 0 と定義 → DSR = PSR(0) (退化版)
  - V[SR] (試行間 SR 分散) が未知の場合は SR 推定量の漸近分散
        (1 - g3*SR^ + (g4 - 1)/4 * SR^²) / (T - 1)
    で代用する。

  NOTE: NOTE-001 / HANDOVER_2026-09-30 に記載された簡略式は論文の定義と一致しない
  ため、本モジュールは原論文の定義に従う (docs/notes/NOTE-001 §8 参照)。

## 設計原則

- **純 Python** — numpy / statistics 不使用 (決定経路のため ADR-001 ホワイトリスト外)
- **副作用なし** — 純関数 / dataclass
- **frozen=True** — DsrParams は不変値オブジェクト
- **ADR-002 未整備への対処** — 試行回数 N が与えられない場合は N=1 と仮定し、
  結果に n_trials_source="assumed" と review_required=True を付与する
  (N を過少申告すると DSR は楽観的になるため、仮定値での合格は人間レビュー必須)

## 公開 API

    norm_cdf(x) / norm_ppf(p)
    sample_moments(returns) -> ReturnMoments
    sharpe_ratio(returns) -> float
    probabilistic_sharpe_ratio(sr, n_obs, skew, kurt, sr_benchmark) -> float
    expected_max_sharpe(n_trials, sr_variance) -> float
    deflated_sharpe_ratio(sr, n_obs, n_trials, skew, kurt, sr_variance) -> float

    DsrParams(min_dsr=0.95, default_n_trials=1)
    DsrGate.check(returns, n_trials, trial_sharpes) -> DsrGateResult
    DsrGate.check_stats(sr, n_obs, skew, kurt, n_trials, ...) -> DsrGateResult
    check_dsr_gate(returns, n_trials, min_dsr) -> DsrGateResult

## 参照

- Bailey, D.H., López de Prado, M. (2012). "The Sharpe Ratio Efficient Frontier."
  Journal of Risk 15(2).
- Bailey, D.H., López de Prado, M. (2014). "The Deflated Sharpe Ratio: Correcting for
  Selection Bias, Backtest Overfitting, and Non-Normality."
  Journal of Portfolio Management 40(5), 94-107.
- Acklam, P.J. (2003). "An algorithm for computing the inverse normal cumulative
  distribution function."
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

#: Euler–Mascheroni 定数
EULER_GAMMA: float = 0.5772156649015329

#: 憲法デフォルト: DSR 合格閾値 (95% 信頼で真の SR > 期待最大 SR)
_DEFAULT_MIN_DSR: float = 0.95

#: 試行回数 N 未知時の仮定値 (ADR-002 系譜ログ整備までの暫定)
_DEFAULT_N_TRIALS: int = 1

#: DSR 計算に必要な最小観測数
MIN_OBS: int = 3

N_TRIALS_SOURCE_PROVIDED = "provided"
N_TRIALS_SOURCE_ASSUMED = "assumed"


# ---------------------------------------------------------------------------
# 正規分布ユーティリティ (純 Python)
# ---------------------------------------------------------------------------

def norm_cdf(x: float) -> float:
    """標準正規分布の累積分布関数 Φ(x)。math.erfc を用いて裾でも精度を保つ。"""
    if math.isnan(x):
        return float("nan")
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def norm_pdf(x: float) -> float:
    """標準正規分布の確率密度関数 φ(x)。"""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# Acklam (2003) 係数
_A = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
      1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
_B = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
      6.680131188771972e+01, -1.328068155288572e+01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
      -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
      3.754408661907416e+00)
_P_LOW = 0.02425


def norm_ppf(p: float) -> float:
    """
    標準正規分布の逆累積分布関数 Φ⁻¹(p)。

    Acklam (2003) 有理近似 (相対誤差 ~1.15e-9) に Newton 法 1 ステップの
    補正を加え、倍精度近くまで精度を高める。

    p <= 0 → -inf, p >= 1 → +inf。
    """
    if math.isnan(p):
        return float("nan")
    if p <= 0.0:
        return float("-inf")
    if p >= 1.0:
        return float("inf")

    if p < _P_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    elif p <= 1.0 - _P_LOW:
        q = p - 0.5
        r = q * q
        x = (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q / \
            (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)

    # Newton 補正 (Halley 形式)
    e = norm_cdf(x) - p
    u = e * math.sqrt(2.0 * math.pi) * math.exp(0.5 * x * x)
    x = x - u / (1.0 + 0.5 * x * u)
    return x


# ---------------------------------------------------------------------------
# 標本モーメント
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReturnMoments:
    """リターン系列の標本モーメント。"""
    n_obs: int
    mean: float
    std: float          # 標本標準偏差 (ddof=1)
    skew: float         # 歪度 (母集団モーメント比, 正規 = 0)
    kurt: float         # 尖度 (非超過, 正規 = 3)

    @property
    def sharpe(self) -> float:
        """非年率 Sharpe Ratio (mean / std)。std=0 の場合は 0.0。"""
        if self.std <= 0.0:
            return 0.0
        return self.mean / self.std


def _clean(returns: Sequence[Any]) -> List[float]:
    """None / NaN / Inf / 非数値を除外する。"""
    out: List[float] = []
    for v in returns or []:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def sample_moments(returns: Sequence[Any]) -> ReturnMoments:
    """
    リターン系列から標本モーメントを計算する (純 Python / statistics 不使用)。

    - std は ddof=1 (標本標準偏差)
    - skew / kurt は母集団中心モーメント比 m3/m2^1.5, m4/m2^2
      (Bailey & López de Prado の PSR 定義で用いられる形式)
    - 定数系列 (分散 0) の場合は skew=0, kurt=3 (正規と同等) とする
    """
    xs = _clean(returns)
    n = len(xs)
    if n == 0:
        return ReturnMoments(0, 0.0, 0.0, 0.0, 3.0)
    mean = math.fsum(xs) / n
    devs = [x - mean for x in xs]
    m2 = math.fsum(d * d for d in devs) / n
    if n < 2 or m2 <= 1e-300:
        return ReturnMoments(n, mean, 0.0, 0.0, 3.0)
    m3 = math.fsum(d * d * d for d in devs) / n
    m4 = math.fsum(d * d * d * d for d in devs) / n
    std = math.sqrt(m2 * n / (n - 1))
    skew = m3 / (m2 ** 1.5)
    kurt = m4 / (m2 * m2)
    return ReturnMoments(n, mean, std, skew, kurt)


def sharpe_ratio(returns: Sequence[Any]) -> float:
    """非年率 Sharpe Ratio。"""
    return sample_moments(returns).sharpe


# ---------------------------------------------------------------------------
# PSR / 期待最大 SR / DSR
# ---------------------------------------------------------------------------

def _sr_variance_term(sr: float, skew: float, kurt: float) -> float:
    """1 - g3*SR + (g4 - 1)/4 * SR²  (SR 推定量の分散の分子)。"""
    return 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr


def sharpe_estimator_variance(sr: float, n_obs: int, skew: float = 0.0, kurt: float = 3.0) -> float:
    """SR 推定量の漸近分散 (1 - g3*SR + (g4-1)/4*SR²) / (T - 1)。"""
    if n_obs < 2:
        return float("inf")
    return max(_sr_variance_term(sr, skew, kurt), 0.0) / (n_obs - 1)


def probabilistic_sharpe_ratio(
    sr: float,
    n_obs: int,
    skew: float = 0.0,
    kurt: float = 3.0,
    sr_benchmark: float = 0.0,
) -> float:
    """
    Probabilistic Sharpe Ratio: P(真の SR > sr_benchmark)。

    分母項が非正 (極端な歪度・尖度) の場合は計算不能として 0.0 を返す
    (保守側に倒す)。
    """
    if n_obs < 2 or not math.isfinite(sr):
        return 0.0
    denom_sq = _sr_variance_term(sr, skew, kurt)
    if not math.isfinite(denom_sq) or denom_sq <= 0.0:
        return 0.0
    if not math.isfinite(sr_benchmark):
        return 0.0 if sr_benchmark > 0 else 1.0
    z = (sr - sr_benchmark) * math.sqrt(n_obs - 1) / math.sqrt(denom_sq)
    return norm_cdf(z)


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """
    N 個の独立試行における SR の期待最大値 SR0 (False Strategy Theorem)。

        SR0 = sqrt(V) * ((1-γ) Φ⁻¹(1 - 1/N) + γ Φ⁻¹(1 - 1/(N e)))

    N <= 1 または V <= 0 の場合は 0.0。
    """
    if n_trials is None or int(n_trials) <= 1:
        return 0.0
    if not math.isfinite(sr_variance) or sr_variance <= 0.0:
        return 0.0
    n = float(int(n_trials))
    z1 = norm_ppf(1.0 - 1.0 / n)
    z2 = norm_ppf(1.0 - 1.0 / (n * math.e))
    return math.sqrt(sr_variance) * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def deflated_sharpe_ratio(
    sr: float,
    n_obs: int,
    n_trials: int = 1,
    skew: float = 0.0,
    kurt: float = 3.0,
    sr_variance: Optional[float] = None,
) -> float:
    """
    Deflated Sharpe Ratio = PSR(SR0)。

    Parameters
    ----------
    sr : float
        観測 SR (非年率)。
    n_obs : int
        観測数 T。
    n_trials : int
        探索した独立試行数 N (ADR-002 系譜ログから集計)。
    skew, kurt : float
        リターン歪度 / 尖度 (非超過)。
    sr_variance : float, optional
        試行間 SR 分散 V[SR]。None なら SR 推定量の漸近分散で代用。
    """
    if sr_variance is None:
        sr_variance = sharpe_estimator_variance(sr, n_obs, skew, kurt)
    sr0 = expected_max_sharpe(n_trials, sr_variance)
    return probabilistic_sharpe_ratio(sr, n_obs, skew, kurt, sr_benchmark=sr0)


def cross_trial_sr_variance(trial_sharpes: Sequence[Any]) -> Optional[float]:
    """試行群の SR 分散 (ddof=1)。2 未満なら None。"""
    xs = _clean(trial_sharpes)
    if len(xs) < 2:
        return None
    m = math.fsum(xs) / len(xs)
    return math.fsum((x - m) ** 2 for x in xs) / (len(xs) - 1)


# ---------------------------------------------------------------------------
# DsrParams / DsrGateResult / DsrGate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DsrParams:
    """
    DSR ゲートのパラメータ。

    Attributes
    ----------
    min_dsr : float
        合格閾値。DSR >= min_dsr で PASS (デフォルト 0.95)。
    default_n_trials : int
        n_trials 未指定時に仮定する試行回数 (ADR-002 整備までの暫定)。
    """
    min_dsr: float = _DEFAULT_MIN_DSR
    default_n_trials: int = _DEFAULT_N_TRIALS

    def __post_init__(self) -> None:
        if not (0.0 <= self.min_dsr <= 1.0):
            raise ValueError(f"min_dsr は [0, 1] である必要があります: {self.min_dsr}")
        if int(self.default_n_trials) < 1:
            raise ValueError(f"default_n_trials は 1 以上である必要があります: {self.default_n_trials}")

    @classmethod
    def from_config(cls, config: object) -> "DsrParams":
        """FrostConfig / PolicySpec から生成。属性がなければデフォルト。"""
        return cls(
            min_dsr=float(getattr(config, "min_dsr", _DEFAULT_MIN_DSR)),
            default_n_trials=int(getattr(config, "dsr_default_n_trials", _DEFAULT_N_TRIALS)),
        )


@dataclass
class DsrGateResult:
    """DSR ゲート判定結果。"""
    passed: bool
    dsr: float
    psr_zero: float           # PSR(0): N=1 時の DSR と一致
    sharpe: float             # 非年率 SR
    sr0: float                # 期待最大 SR (ベンチマーク)
    n_obs: int
    n_trials: int
    n_trials_source: str      # "provided" | "assumed"
    skew: float
    kurt: float
    sr_variance: float
    sr_variance_source: str   # "cross_trial" | "provided" | "estimator"
    threshold: float
    review_required: bool
    failure_reason: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        def r(v: float) -> Optional[float]:
            return round(v, 10) if math.isfinite(v) else None
        return {
            "gate": "dsr",
            "passed": self.passed,
            "dsr": r(self.dsr),
            "psr_zero": r(self.psr_zero),
            "sharpe": r(self.sharpe),
            "sr0": r(self.sr0),
            "n_obs": self.n_obs,
            "n_trials": self.n_trials,
            "n_trials_source": self.n_trials_source,
            "skew": r(self.skew),
            "kurt": r(self.kurt),
            "sr_variance": r(self.sr_variance),
            "sr_variance_source": self.sr_variance_source,
            "threshold": self.threshold,
            "review_required": self.review_required,
            "failure_reason": self.failure_reason,
            "notes": list(self.notes),
        }


@dataclass
class DsrGate:
    """
    G1: Deflated Sharpe Ratio 昇格前ゲート。

    PortfolioCorrelationGate (G2) と同様、FROST 評価時ではなく昇格決定時に
    呼び出すスタンドアロンゲート。gate_engine への統合は P8 ablation 後に判断する。
    """
    params: DsrParams = field(default_factory=DsrParams)

    @property
    def threshold(self) -> float:
        return self.params.min_dsr

    def check_stats(
        self,
        sr: float,
        n_obs: int,
        skew: float = 0.0,
        kurt: float = 3.0,
        n_trials: Optional[int] = None,
        sr_variance: Optional[float] = None,
        trial_sharpes: Optional[Sequence[Any]] = None,
    ) -> DsrGateResult:
        """要約統計量から DSR ゲートを判定する。"""
        notes: List[str] = []

        # --- N の決定 ---------------------------------------------------
        if n_trials is None:
            n = int(self.params.default_n_trials)
            n_src = N_TRIALS_SOURCE_ASSUMED
            notes.append(
                f"n_trials 未提供: N={n} を仮定 (ADR-002 系譜ログ未整備)。"
                "N の過少申告は DSR を楽観化するため人間レビュー必須"
            )
        else:
            n = int(n_trials)
            n_src = N_TRIALS_SOURCE_PROVIDED
            if n < 1:
                raise ValueError(f"n_trials は 1 以上である必要があります: {n_trials}")

        # --- V[SR] の決定 ------------------------------------------------
        var_src = "estimator"
        var: Optional[float] = None
        if trial_sharpes is not None:
            var = cross_trial_sr_variance(trial_sharpes)
            if var is not None:
                var_src = "cross_trial"
                if n_trials is None:
                    # 試行群が与えられているならその本数は下限として使える
                    n_obs_trials = len(_clean(trial_sharpes))
                    if n_obs_trials > n:
                        n = n_obs_trials
                        notes.append(f"trial_sharpes 本数から N={n} を下限採用")
            else:
                notes.append("trial_sharpes が 2 本未満のため推定量分散で代用")
        if var is None and sr_variance is not None:
            if math.isfinite(sr_variance) and sr_variance >= 0.0:
                var = float(sr_variance)
                var_src = "provided"
        if var is None:
            var = sharpe_estimator_variance(sr, n_obs, skew, kurt)

        base = dict(
            sharpe=sr, n_obs=int(n_obs), n_trials=n, n_trials_source=n_src,
            skew=skew, kurt=kurt, sr_variance=var, sr_variance_source=var_src,
            threshold=self.threshold, notes=notes,
        )

        # --- 観測数不足 -------------------------------------------------
        if n_obs < MIN_OBS or not math.isfinite(sr):
            return DsrGateResult(
                passed=False, dsr=0.0, psr_zero=0.0, sr0=0.0,
                review_required=True,
                failure_reason=f"INSUFFICIENT_OBS: n_obs={n_obs} < {MIN_OBS}",
                **base,
            )

        sr0 = expected_max_sharpe(n, var)
        dsr = probabilistic_sharpe_ratio(sr, n_obs, skew, kurt, sr_benchmark=sr0)
        psr0 = probabilistic_sharpe_ratio(sr, n_obs, skew, kurt, sr_benchmark=0.0)
        if _sr_variance_term(sr, skew, kurt) <= 0.0:
            notes.append("PSR 分母項が非正 (極端な歪度/尖度): DSR=0 として保守判定")

        passed = dsr >= self.threshold
        failure = None
        if not passed:
            failure = (
                f"DSR_BELOW_THRESHOLD: dsr={dsr:.4f} < {self.threshold:.4f} "
                f"(SR={sr:.4f}, SR0={sr0:.4f}, N={n})"
            )
        review = (not passed) or n_src == N_TRIALS_SOURCE_ASSUMED
        return DsrGateResult(
            passed=passed, dsr=dsr, psr_zero=psr0, sr0=sr0,
            review_required=review, failure_reason=failure, **base,
        )

    def check(
        self,
        returns: Sequence[Any],
        n_trials: Optional[int] = None,
        trial_sharpes: Optional[Sequence[Any]] = None,
        sr_variance: Optional[float] = None,
    ) -> DsrGateResult:
        """OOS リターン系列から DSR ゲートを判定する。"""
        m = sample_moments(returns)
        return self.check_stats(
            sr=m.sharpe, n_obs=m.n_obs, skew=m.skew, kurt=m.kurt,
            n_trials=n_trials, sr_variance=sr_variance, trial_sharpes=trial_sharpes,
        )

    @classmethod
    def from_config(cls, config: object) -> "DsrGate":
        return cls(params=DsrParams.from_config(config))


# ---------------------------------------------------------------------------
# 関数型ラッパー
# ---------------------------------------------------------------------------

def check_dsr_gate(
    returns: Sequence[Any],
    n_trials: Optional[int] = None,
    min_dsr: float = _DEFAULT_MIN_DSR,
    trial_sharpes: Optional[Sequence[Any]] = None,
) -> DsrGateResult:
    """DsrGate(DsrParams(min_dsr)).check(...) の関数型ラッパー。"""
    return DsrGate(DsrParams(min_dsr=min_dsr)).check(
        returns, n_trials=n_trials, trial_sharpes=trial_sharpes,
    )
