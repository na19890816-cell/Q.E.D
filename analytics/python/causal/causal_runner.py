"""
causal_runner.py
----------------
Causal Discovery Layer のオーケストレーター。

フロー:
  1. 候補シグナル + リターン系列を受け取る
  2. compute_causal_direction() で因果方向性スコアを計算
  3. compute_invariance() で不変性スコアを計算
  4. compute_causal_diagnostics() で総合診断
  5. FROST v2 特徴量として返す

設計原則:
  - trace_id end-to-end
  - dry_run 対応
  - 副作用なし（DB 書き込みは causal_bridge.py）
  - 環境変数: CAUSAL_DISCOVERY_ENABLED=1
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .causal_direction import (
    CausalDirectionResult,
    compute_causal_direction,
)
from .causal_invariance import (
    InvarianceResult,
    compute_invariance,
)
from .causal_diagnostics import (
    CausalDiagnostics,
    causal_diagnostics_to_frost_features,
    compute_causal_diagnostics,
)


# ---------------------------------------------------------------------------
# 定数・環境変数
# ---------------------------------------------------------------------------

_CAUSAL_ENABLED: bool = os.environ.get(
    "CAUSAL_DISCOVERY_ENABLED", "1"
).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# 入力候補
# ---------------------------------------------------------------------------

@dataclass
class CausalCandidate:
    """
    Causal Discovery 評価対象の候補。

    Attributes
    ----------
    candidate_id : str
    trace_id : str
    run_id : str
    signal : list[float]
        OOS シグナル系列
    returns : list[float]
        OOS リターン系列
    regime_masks : dict[str, list[bool]], optional
        外部から渡すレジームマスク
    """
    candidate_id: str
    trace_id: str
    run_id: str
    signal: List[float]
    returns: List[float]
    regime_masks: Optional[Dict[str, List[bool]]] = None


# ---------------------------------------------------------------------------
# 実行結果
# ---------------------------------------------------------------------------

@dataclass
class CausalRunResult:
    """
    Causal Layer の単候補評価結果。
    """
    candidate_id: str
    trace_id: str
    run_id: str
    direction_result: CausalDirectionResult
    invariance_result: InvarianceResult
    diagnostics: CausalDiagnostics
    frost_features: Dict[str, float]
    gate_pass: bool
    dry_run: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "gate_pass": self.gate_pass,
            "dry_run": self.dry_run,
            **self.diagnostics.to_dict(),
        }


@dataclass
class CausalBatchResult:
    """
    複数候補の Causal Discovery バッチ実行結果。
    """
    run_id: str
    trace_id: str
    results: List[CausalRunResult]
    pass_count: int
    fail_count: int
    pass_ratio: float
    dry_run: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "n_candidates": len(self.results),
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "pass_ratio": self.pass_ratio,
            "dry_run": self.dry_run,
        }


# ---------------------------------------------------------------------------
# Causal Runner
# ---------------------------------------------------------------------------

def run_causal_layer(
    candidate: CausalCandidate,
    lag: int = 1,
    n_regimes: int = 4,
    dry_run: bool = False,
) -> CausalRunResult:
    """
    単候補の Causal Discovery を実行する。

    Parameters
    ----------
    candidate : CausalCandidate
    lag : int
        因果方向性評価のラグ
    n_regimes : int
        不変性検定のサブサンプル分割数
    dry_run : bool

    Returns
    -------
    CausalRunResult
    """
    if not _CAUSAL_ENABLED:
        return _disabled_run_result(candidate, dry_run)

    # Step 1: 因果方向性
    direction_result = compute_causal_direction(
        signal=candidate.signal,
        returns=candidate.returns,
        lag=lag,
    )

    # Step 2: 不変性
    invariance_result = compute_invariance(
        signal=candidate.signal,
        returns=candidate.returns,
        regime_masks=candidate.regime_masks,
        n_regimes=n_regimes,
    )

    # Step 3: 総合診断
    diagnostics = compute_causal_diagnostics(direction_result, invariance_result)

    # FROST v2 特徴量
    frost_features = causal_diagnostics_to_frost_features(diagnostics)

    gate_pass = diagnostics.all_gates_pass

    return CausalRunResult(
        candidate_id=candidate.candidate_id,
        trace_id=candidate.trace_id,
        run_id=candidate.run_id,
        direction_result=direction_result,
        invariance_result=invariance_result,
        diagnostics=diagnostics,
        frost_features=frost_features,
        gate_pass=gate_pass,
        dry_run=dry_run,
    )


def run_causal_batch(
    candidates: List[CausalCandidate],
    run_id: str,
    trace_id: str,
    lag: int = 1,
    n_regimes: int = 4,
    dry_run: bool = False,
) -> CausalBatchResult:
    """
    複数候補の Causal Discovery バッチ実行。
    """
    results: List[CausalRunResult] = []
    for cand in candidates:
        r = run_causal_layer(cand, lag=lag, n_regimes=n_regimes, dry_run=dry_run)
        results.append(r)

    pass_count = sum(1 for r in results if r.gate_pass)
    fail_count = len(results) - pass_count
    pass_ratio = pass_count / len(results) if results else 0.0

    return CausalBatchResult(
        run_id=run_id,
        trace_id=trace_id,
        results=results,
        pass_count=pass_count,
        fail_count=fail_count,
        pass_ratio=pass_ratio,
        dry_run=dry_run,
    )


def _disabled_run_result(candidate: CausalCandidate, dry_run: bool) -> CausalRunResult:
    from .causal_direction import _disabled_direction_result
    from .causal_invariance import _disabled_invariance_result
    from .causal_diagnostics import CausalDiagnostics

    dr = _disabled_direction_result()
    ir = _disabled_invariance_result()
    diag = CausalDiagnostics(
        causal_direction_score=1.0, invariance_pass_ratio=1.0,
        intervention_consistency_score=1.0, confounding_risk_score=0.0,
        causal_composite_score=1.0, all_gates_pass=True, failure_reasons=[],
    )
    return CausalRunResult(
        candidate_id=candidate.candidate_id,
        trace_id=candidate.trace_id,
        run_id=candidate.run_id,
        direction_result=dr,
        invariance_result=ir,
        diagnostics=diag,
        frost_features=causal_diagnostics_to_frost_features(diag),
        gate_pass=True,
        dry_run=dry_run,
    )
