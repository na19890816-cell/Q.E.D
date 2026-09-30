"""
portfolio_correlation_gate.py
------------------------------
Phase 2 (G2): 採用済みポートフォリオとの相関ゲート (r < max_portfolio_corr)

## 背景 (NOTE-002 / 憲法ギャップ G2)

設計憲法は「採用済みポートフォリオとの相関 r < 0.6」の昇格ゲートを要求しているが、
既存の max_signal_corr (≦ 0.90) は **候補同士** の重複排除 (DedupStage) であり、
**候補 対 採用済みアルファ** の相関検査は存在しなかった。

本モジュールはこのギャップを埋める単一責任の昇格前ゲートである。

## 設計原則

- 副作用なし (純関数)
- ADR-001 準拠: numpy のみ使用 (statistics モジュール不使用)
- FrostConfig / PolicySpec 両方を config として受け付ける
- DB 接続不要: knowledge_artifacts の OOS シグナルをリスト形式で受け取る
- 昇格 Bridge レイヤーから呼ぶ設計 (FROST 評価時ではなく昇格決定時)

## 公開 API

    PortfolioCorrelationGate
        .check(candidate_signal, promoted_signals) -> PortfolioGateResult
        .check_one(candidate_signal, artifact_id, artifact_signal) -> SingleCorrResult

    check_portfolio_correlation_gate(
        candidate_signal,
        promoted_signals,
        threshold,
    ) -> PortfolioGateResult   # 関数型ラッパー (後方互換)

## 使用例

    from analytics.python.frost.portfolio_correlation_gate import (
        PortfolioCorrelationGate,
        check_portfolio_correlation_gate,
    )

    gate = PortfolioCorrelationGate(config=policy_spec)  # max_portfolio_corr=0.60
    result = gate.check(
        candidate_signal=[0.1, -0.2, 0.3, ...],
        promoted_signals={"artifact_a": [0.05, -0.15, 0.28, ...], ...},
    )
    if not result.passed:
        print(result.failure_reason)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

#: 憲法デフォルト: 採用済みポートフォリオとの相関上限
_DEFAULT_MAX_PORTFOLIO_CORR: float = 0.60

#: 相関計算に必要な最小データ点数
_MIN_SIGNAL_LEN: int = 3


# ---------------------------------------------------------------------------
# 相関ユーティリティ (numpy 版 / ADR-001 準拠)
# ---------------------------------------------------------------------------

def _pearson_numpy(xs: List[float], ys: List[float]) -> float:
    """
    ピアソン相関係数を numpy で計算する。

    ADR-001 準拠: statistics.correlation() 不使用。
    短すぎる / 定数列 / NaN・Inf 混入の場合は 0.0 を返す。

    Parameters
    ----------
    xs, ys : List[float]
        等長のシグナル列。異なる長さの場合は短い方に truncate する。

    Returns
    -------
    float in [-1.0, 1.0]
    """
    n = min(len(xs), len(ys))
    if n < _MIN_SIGNAL_LEN:
        return 0.0

    a = np.array(xs[:n], dtype=np.float64)
    b = np.array(ys[:n], dtype=np.float64)

    # NaN / Inf → 0.0 で置換
    a = np.where(np.isfinite(a), a, 0.0)
    b = np.where(np.isfinite(b), b, 0.0)

    std_a = float(a.std())
    std_b = float(b.std())
    if std_a < 1e-10 or std_b < 1e-10:
        return 0.0

    r = float(np.corrcoef(a, b)[0, 1])
    if not math.isfinite(r):
        return 0.0
    return float(np.clip(r, -1.0, 1.0))


# ---------------------------------------------------------------------------
# 結果 Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SingleCorrResult:
    """
    候補シグナルと採用済みアルファ 1 本との相関結果。
    """
    artifact_id: str
    correlation: float
    threshold: float
    exceeded: bool  # abs(correlation) >= threshold のとき True

    @property
    def abs_corr(self) -> float:
        return abs(self.correlation)

    def __repr__(self) -> str:
        mark = "FAIL" if self.exceeded else "PASS"
        return (
            f"SingleCorrResult({mark} artifact={self.artifact_id!r} "
            f"r={self.correlation:.4f} thr={self.threshold:.4f})"
        )


@dataclass
class PortfolioGateResult:
    """
    PortfolioCorrelationGate.check() の結果。

    passed=False のとき昇格ゲートが失敗であり、failure_reason に詳細が入る。
    all_results には採用済み全アルファとの相関一覧が格納される。
    """
    passed: bool
    threshold: float
    candidate_signal_len: int
    promoted_count: int
    all_results: List[SingleCorrResult] = field(default_factory=list)
    failure_reason: str = ""

    @property
    def max_correlation(self) -> Optional[float]:
        """全採用済みアルファとの相関絶対値の最大値。promoted_count=0 の場合は None。"""
        if not self.all_results:
            return None
        return max(r.abs_corr for r in self.all_results)

    @property
    def failed_artifacts(self) -> List[SingleCorrResult]:
        """ゲート失敗した採用済みアルファの一覧。"""
        return [r for r in self.all_results if r.exceeded]

    def to_dict(self) -> Dict[str, Any]:
        """診断用辞書に変換する。FrostEvaluation.diagnostics_json への格納に使用。"""
        return {
            "gate": "portfolio_correlation",
            "passed": self.passed,
            "threshold": self.threshold,
            "candidate_signal_len": self.candidate_signal_len,
            "promoted_count": self.promoted_count,
            "max_correlation": self.max_correlation,
            "failure_reason": self.failure_reason,
            "failed_artifacts": [
                {
                    "artifact_id": r.artifact_id,
                    "correlation": round(r.correlation, 6),
                }
                for r in self.failed_artifacts
            ],
        }


# ---------------------------------------------------------------------------
# PortfolioCorrelationGate
# ---------------------------------------------------------------------------

@dataclass
class PortfolioCorrelationGate:
    """
    採用済みポートフォリオとの相関ゲート。

    憲法ギャップ G2 の実装。候補の OOS シグナルと、採用済みアルファ全本の
    OOS シグナルとの相関を検査し、閾値を超えた場合に昇格を阻止する。

    Attributes
    ----------
    threshold : float
        相関上限値。abs(r) >= threshold でゲート失敗。
        PolicySpec.max_portfolio_corr から設定される (デフォルト 0.60)。
    """

    threshold: float = _DEFAULT_MAX_PORTFOLIO_CORR

    def check_one(
        self,
        candidate_signal: List[float],
        artifact_id: str,
        artifact_signal: List[float],
    ) -> SingleCorrResult:
        """
        採用済みアルファ 1 本との相関を検査する。

        Parameters
        ----------
        candidate_signal : List[float]
            候補の OOS シグナル時系列。
        artifact_id : str
            採用済みアルファの識別子。
        artifact_signal : List[float]
            採用済みアルファの OOS シグナル時系列。

        Returns
        -------
        SingleCorrResult
        """
        r = _pearson_numpy(candidate_signal, artifact_signal)
        exceeded = abs(r) >= self.threshold
        return SingleCorrResult(
            artifact_id=artifact_id,
            correlation=r,
            threshold=self.threshold,
            exceeded=exceeded,
        )

    def check(
        self,
        candidate_signal: List[float],
        promoted_signals: Dict[str, List[float]],
    ) -> PortfolioGateResult:
        """
        採用済みポートフォリオ全本との相関を一括検査する。

        Parameters
        ----------
        candidate_signal : List[float]
            候補の OOS シグナル時系列。
        promoted_signals : Dict[str, List[float]]
            {artifact_id: oos_signal_list} — knowledge_artifacts から取得した
            採用済みアルファの OOS シグナル。空辞書の場合はゲートをスキップ (passed=True)。

        Returns
        -------
        PortfolioGateResult
        """
        cand_len = len(candidate_signal)

        # 採用済みアルファが 0 本の場合はゲートスキップ
        if not promoted_signals:
            return PortfolioGateResult(
                passed=True,
                threshold=self.threshold,
                candidate_signal_len=cand_len,
                promoted_count=0,
            )

        all_results: List[SingleCorrResult] = []
        for artifact_id, artifact_signal in promoted_signals.items():
            result = self.check_one(candidate_signal, artifact_id, artifact_signal)
            all_results.append(result)

        # ゲート判定: いずれか 1 本でも超えたら失敗
        failed = [r for r in all_results if r.exceeded]
        if failed:
            # 最も相関が高いものを failure_reason に記載
            worst = max(failed, key=lambda r: r.abs_corr)
            failure_reason = (
                f"OOS 相関 r={worst.correlation:.4f} が閾値 {self.threshold:.4f} を超過 "
                f"(artifact={worst.artifact_id!r}, |r|={worst.abs_corr:.4f})"
            )
            return PortfolioGateResult(
                passed=False,
                threshold=self.threshold,
                candidate_signal_len=cand_len,
                promoted_count=len(promoted_signals),
                all_results=all_results,
                failure_reason=failure_reason,
            )

        return PortfolioGateResult(
            passed=True,
            threshold=self.threshold,
            candidate_signal_len=cand_len,
            promoted_count=len(promoted_signals),
            all_results=all_results,
        )

    # ------------------------------------------------------------------ #
    # クラスメソッド
    # ------------------------------------------------------------------ #

    @classmethod
    def from_config(cls, config: Any) -> "PortfolioCorrelationGate":
        """
        FrostConfig / PolicySpec から PortfolioCorrelationGate を生成する。

        config に max_portfolio_corr 属性がない場合はデフォルト値を使用する
        (後方互換: max_portfolio_corr 追加前の FrostConfig への対応)。
        """
        threshold = float(
            getattr(config, "max_portfolio_corr", _DEFAULT_MAX_PORTFOLIO_CORR)
        )
        return cls(threshold=threshold)


# ---------------------------------------------------------------------------
# 関数型ラッパー (後方互換 / シンプルな呼び出し用)
# ---------------------------------------------------------------------------

def check_portfolio_correlation_gate(
    candidate_signal: List[float],
    promoted_signals: Dict[str, List[float]],
    threshold: float = _DEFAULT_MAX_PORTFOLIO_CORR,
) -> PortfolioGateResult:
    """
    採用済みポートフォリオとの相関ゲートを検査する関数型ラッパー。

    Parameters
    ----------
    candidate_signal : List[float]
        候補の OOS シグナル時系列。
    promoted_signals : Dict[str, List[float]]
        {artifact_id: oos_signal_list}
    threshold : float
        相関上限値 (default: 0.60)

    Returns
    -------
    PortfolioGateResult
    """
    gate = PortfolioCorrelationGate(threshold=threshold)
    return gate.check(candidate_signal, promoted_signals)
