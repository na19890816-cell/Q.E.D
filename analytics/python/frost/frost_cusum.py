"""
frost_cusum.py
--------------
Phase 3 (G3): CUSUM（累積和制御チャート）— 純 Python 実装

## 背景 (NOTE-003 / 憲法ギャップ G3)

シグナルライフサイクル憲法:
    Predict → Select → Execute → **Detect** → Kill

Detect フェーズの技術的中核として、昇格済みアルファの rolling IC 系列に
CUSUM を適用し、統計的に有意な性能劣化（下方ドリフト）を検知する。

## 設計原則

- **純 Python** — numpy/statistics 不使用 (ADR-001 完全準拠)
- **副作用なし** — 純関数 / dataclass メソッド
- **双方向 CUSUM** — 下方（IC 低下）と上方（IC 回復）の両方向を追跡
- **バッチ / ストリーミング両対応** — run() で全系列一括 / step() で 1 点ずつ

## 公開 API

    CusumDetector
        .step(value)  -> CusumStepResult    # ストリーミング: 1 点ずつ更新
        .run(values)  -> CusumRunResult     # バッチ: 系列全体を処理
        .reset()                            # 状態リセット

    CusumParams
        k : float  — 許容ドリフト（参照値 = μ0 ± k）
        h : float  — 警告閾値（累積和がこれを超えたら Detect）
        mu0 : float — 正常時の期待 IC（デフォルト 0.0）

    detect_degradation_cusum(rolling_ic, k, h) -> bool
        — 関数型ラッパー（NOTE-003 疑似コードと同一シグネチャ）

## 参照

- Page, E.S. (1954). "Continuous inspection schemes." Biometrika 41(1), 100-115.
- Hawkins, D.M. & Olwell, D.H. (1998). Cumulative Sum Charts and Charting for
  Quality Improvement. Springer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# CusumParams
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CusumParams:
    """
    CUSUM 制御チャートのパラメータ。

    Attributes
    ----------
    k : float
        許容ドリフト（slack / allowance）。
        下方 CUSUM: cusum_neg += max(0, cusum_neg - x + k)
        上方 CUSUM: cusum_pos += max(0, x - mu0 - k)
        通常は IC 標準偏差の 0.5 倍 (k = 0.5 * σ_IC) が推奨。
    h : float
        警告閾値。累積和がこれを超えると Detect トリガー。
        通常は IC 標準偏差の 4〜5 倍 (h = 4σ または 5σ) が推奨。
        デフォルト 5.0 は標準化済み IC (σ≈1) の想定。
    mu0 : float
        正常時の IC 期待値（ベースライン）。
        デフォルト 0.0 は「IC がゼロ近傍の状態が正常」の想定。
    """
    k: float = 0.5
    h: float = 5.0
    mu0: float = 0.0

    def __post_init__(self) -> None:
        if self.k < 0:
            raise ValueError(f"k は非負である必要があります: k={self.k}")
        if self.h <= 0:
            raise ValueError(f"h は正の値である必要があります: h={self.h}")

    @classmethod
    def from_config(cls, config: object) -> "CusumParams":
        """
        FrostConfig / PolicySpec から CusumParams を生成する。
        config に cusum_k / cusum_h / cusum_mu0 属性がない場合はデフォルト値を使用する。
        """
        return cls(
            k=float(getattr(config, "cusum_k", 0.5)),
            h=float(getattr(config, "cusum_h", 5.0)),
            mu0=float(getattr(config, "cusum_mu0", 0.0)),
        )


# ---------------------------------------------------------------------------
# CusumStepResult / CusumRunResult
# ---------------------------------------------------------------------------

@dataclass
class CusumStepResult:
    """
    CusumDetector.step() の 1 ステップ結果。
    """
    step_index: int       # 0 始まりのステップ番号
    value: float          # 入力値
    cusum_neg: float      # 下方累積和（IC 低下方向）
    cusum_pos: float      # 上方累積和（IC 回復方向）
    degraded: bool        # cusum_neg >= h → 下方 Detect トリガー
    recovered: bool       # cusum_pos >= h → 上方 Detect トリガー（IC 異常回復）

    @property
    def triggered(self) -> bool:
        """どちらかの方向で Detect トリガーされているか。"""
        return self.degraded or self.recovered


@dataclass
class CusumRunResult:
    """
    CusumDetector.run() のバッチ処理結果。
    """
    params: CusumParams
    steps: List[CusumStepResult] = field(default_factory=list)
    degradation_detected: bool = False   # いずれかの点で degraded=True
    recovery_detected: bool = False      # いずれかの点で recovered=True
    first_degradation_index: Optional[int] = None  # 最初に degraded になったステップ
    first_recovery_index: Optional[int] = None     # 最初に recovered になったステップ

    @property
    def triggered(self) -> bool:
        """下方 Detect または上方 Detect のいずれかがトリガーされたか。"""
        return self.degradation_detected or self.recovery_detected

    @property
    def n_steps(self) -> int:
        return len(self.steps)

    @property
    def final_cusum_neg(self) -> float:
        return self.steps[-1].cusum_neg if self.steps else 0.0

    @property
    def final_cusum_pos(self) -> float:
        return self.steps[-1].cusum_pos if self.steps else 0.0

    def to_dict(self) -> dict:
        """診断用辞書。FrostEvaluation.diagnostics_json への格納に使用。"""
        return {
            "detector": "cusum",
            "k": self.params.k,
            "h": self.params.h,
            "mu0": self.params.mu0,
            "n_steps": self.n_steps,
            "degradation_detected": self.degradation_detected,
            "recovery_detected": self.recovery_detected,
            "first_degradation_index": self.first_degradation_index,
            "first_recovery_index": self.first_recovery_index,
            "final_cusum_neg": round(self.final_cusum_neg, 6),
            "final_cusum_pos": round(self.final_cusum_pos, 6),
        }


# ---------------------------------------------------------------------------
# CusumDetector
# ---------------------------------------------------------------------------

@dataclass
class CusumDetector:
    """
    双方向 CUSUM 制御チャート。

    下方 CUSUM: IC の持続的低下（劣化）を検知する。
    上方 CUSUM: IC の異常な急回復（データ品質問題の代理指標）を検知する。

    **ストリーミング使用例**::

        detector = CusumDetector(CusumParams(k=0.5, h=5.0))
        for ic in rolling_ic_stream:
            result = detector.step(ic)
            if result.degraded:
                trigger_human_review(...)
                break

    **バッチ使用例**::

        result = CusumDetector(CusumParams()).run(rolling_ic_list)
        if result.degradation_detected:
            print(f"劣化検知: step={result.first_degradation_index}")

    Attributes
    ----------
    params : CusumParams
        CUSUM パラメータ (k, h, mu0)。
    """

    params: CusumParams = field(default_factory=CusumParams)

    # 内部状態（ストリーミング用）
    _cusum_neg: float = field(default=0.0, init=False, repr=False)
    _cusum_pos: float = field(default=0.0, init=False, repr=False)
    _step_count: int = field(default=0, init=False, repr=False)

    def reset(self) -> None:
        """CUSUM 累積和をリセットする（新しいウィンドウ開始時に使用）。"""
        self._cusum_neg = 0.0
        self._cusum_pos = 0.0
        self._step_count = 0

    def step(self, value: float) -> CusumStepResult:
        """
        1 点を処理してストリーミング更新する。

        Parameters
        ----------
        value : float
            現時点の IC 値。NaN / Inf は 0.0 として扱う（ガード済み）。

        Returns
        -------
        CusumStepResult
        """
        # NaN / Inf ガード
        v = value if math.isfinite(value) else 0.0

        k = self.params.k
        h = self.params.h
        mu0 = self.params.mu0

        # 下方 CUSUM (Page 1954): S_neg = max(0, S_neg - (v - mu0 + k))
        # = max(0, S_neg - v + mu0 - k)
        # v < mu0 - k のとき S_neg が増加する（IC が基準値 mu0 より k 以上下回ると蓄積）
        self._cusum_neg = max(0.0, self._cusum_neg - (v - mu0 + k))

        # 上方 CUSUM: S_pos += max(0, (v - mu0) - k)
        self._cusum_pos = max(0.0, self._cusum_pos + (v - mu0) - k)

        degraded = self._cusum_neg >= h
        recovered = self._cusum_pos >= h

        result = CusumStepResult(
            step_index=self._step_count,
            value=v,
            cusum_neg=self._cusum_neg,
            cusum_pos=self._cusum_pos,
            degraded=degraded,
            recovered=recovered,
        )
        self._step_count += 1
        return result

    def run(self, values: List[float]) -> CusumRunResult:
        """
        系列全体をバッチ処理する。

        内部状態はリセットしてから処理するため、前回の step() 呼び出し結果に
        影響されない。

        Parameters
        ----------
        values : List[float]
            rolling IC の時系列。空の場合は空の CusumRunResult を返す。

        Returns
        -------
        CusumRunResult
        """
        self.reset()

        steps: List[CusumStepResult] = []
        degradation_detected = False
        recovery_detected = False
        first_degradation_index: Optional[int] = None
        first_recovery_index: Optional[int] = None

        for v in values:
            s = self.step(v)
            steps.append(s)
            if s.degraded and not degradation_detected:
                degradation_detected = True
                first_degradation_index = s.step_index
            if s.recovered and not recovery_detected:
                recovery_detected = True
                first_recovery_index = s.step_index

        return CusumRunResult(
            params=self.params,
            steps=steps,
            degradation_detected=degradation_detected,
            recovery_detected=recovery_detected,
            first_degradation_index=first_degradation_index,
            first_recovery_index=first_recovery_index,
        )

    @classmethod
    def from_config(cls, config: object) -> "CusumDetector":
        """FrostConfig / PolicySpec から CusumDetector を生成する。"""
        return cls(params=CusumParams.from_config(config))


# ---------------------------------------------------------------------------
# 関数型ラッパー (NOTE-003 疑似コードと同一シグネチャ)
# ---------------------------------------------------------------------------

def detect_degradation_cusum(
    rolling_ic: List[float],
    k: float = 0.5,
    h: float = 5.0,
    mu0: float = 0.0,
) -> bool:
    """
    rolling IC に CUSUM を適用し、下方劣化を検知したら True を返す。

    NOTE-003 §3.1 の疑似コードと同一シグネチャで実装した関数型ラッパー。

    Parameters
    ----------
    rolling_ic : List[float]
        昇格済みアルファの rolling IC 時系列。
    k : float
        許容ドリフト (default: 0.5)。
    h : float
        警告閾値 (default: 5.0)。
    mu0 : float
        正常時の期待 IC (default: 0.0)。

    Returns
    -------
    bool
        True: 劣化検知 / False: 正常
    """
    params = CusumParams(k=k, h=h, mu0=mu0)
    detector = CusumDetector(params=params)
    result = detector.run(rolling_ic)
    return result.degradation_detected
