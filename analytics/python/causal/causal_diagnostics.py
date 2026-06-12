"""
causal_diagnostics.py
---------------------
因果検定の診断・サマリー情報を生成するモジュール。

複数の causal 指標を統合して:
  - intervention_consistency_score
  - confounding_risk_score
  - causal_composite_score

を計算する。

設計原則:
  - pure Python
  - causal_direction.py + causal_invariance.py の結果を集約
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .causal_direction import CausalDirectionResult
from .causal_invariance import InvarianceResult


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class CausalDiagnostics:
    """
    因果検定の総合診断結果。

    Attributes
    ----------
    causal_direction_score : float
        X→Return の方向性スコア（causal_direction_result から）
    invariance_pass_ratio : float
        不変性通過率（causal_invariance_result から）
    intervention_consistency_score : float
        介入整合性スコア（方向性 × 不変性の幾何平均）
    confounding_risk_score : float
        交絡リスクスコア（0〜1、低いほど交絡リスクが小さい）
    causal_composite_score : float
        総合因果スコア（0〜1）
    all_gates_pass : bool
        全ての因果 gate が pass かどうか
    failure_reasons : list[str]
    """
    causal_direction_score: float
    invariance_pass_ratio: float
    intervention_consistency_score: float
    confounding_risk_score: float
    causal_composite_score: float
    all_gates_pass: bool
    failure_reasons: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "causal_direction_score": self.causal_direction_score,
            "invariance_pass_ratio": self.invariance_pass_ratio,
            "intervention_consistency_score": self.intervention_consistency_score,
            "confounding_risk_score": self.confounding_risk_score,
            "causal_composite_score": self.causal_composite_score,
            "all_gates_pass": self.all_gates_pass,
            "failure_reasons": self.failure_reasons,
        }


# ---------------------------------------------------------------------------
# 診断計算
# ---------------------------------------------------------------------------

def compute_causal_diagnostics(
    direction_result: CausalDirectionResult,
    invariance_result: InvarianceResult,
) -> CausalDiagnostics:
    """
    因果方向性 + 不変性結果から総合診断を計算する。

    Parameters
    ----------
    direction_result : CausalDirectionResult
    invariance_result : InvarianceResult

    Returns
    -------
    CausalDiagnostics
    """
    dir_score = direction_result.causal_direction_score
    inv_ratio = invariance_result.invariance_pass_ratio

    # 介入整合性スコア（方向性 × 不変性の幾何平均）
    intervention_consistency = math.sqrt(dir_score * inv_ratio)

    # 交絡リスクスコア
    # backward_correlation が高い → 逆方向の相関 → 交絡リスク高
    bwd = abs(direction_result.backward_correlation)
    fwd = abs(direction_result.forward_correlation)
    if fwd > 1e-10:
        confounding_risk = min(1.0, bwd / fwd)
    else:
        confounding_risk = 1.0 if bwd > 0.05 else 0.0

    # 係数不安定性も交絡リスクに加算
    coeff_instability = 1.0 - invariance_result.coefficient_stability
    confounding_risk = min(1.0, (confounding_risk + coeff_instability * 0.3) / 1.3)

    # 総合スコア
    causal_composite = (
        dir_score * 0.40
        + inv_ratio * 0.40
        + (1.0 - confounding_risk) * 0.20
    )

    # gate 集約
    failure_reasons = []
    if not direction_result.gate_pass:
        failure_reasons.append(direction_result.gate_reason)
    if not invariance_result.gate_pass:
        failure_reasons.append(invariance_result.gate_reason)
    all_gates_pass = len(failure_reasons) == 0

    return CausalDiagnostics(
        causal_direction_score=dir_score,
        invariance_pass_ratio=inv_ratio,
        intervention_consistency_score=intervention_consistency,
        confounding_risk_score=confounding_risk,
        causal_composite_score=causal_composite,
        all_gates_pass=all_gates_pass,
        failure_reasons=failure_reasons,
    )


def causal_diagnostics_to_frost_features(d: CausalDiagnostics) -> Dict[str, float]:
    """CausalDiagnostics を FROST v2 特徴量辞書に変換する。"""
    return {
        "causal_direction_score": d.causal_direction_score,
        "invariance_pass_ratio": d.invariance_pass_ratio,
        "intervention_consistency_score": d.intervention_consistency_score,
        "confounding_risk_score": d.confounding_risk_score,
        "causal_validity_score": d.causal_composite_score,  # FROST v2 軸名
    }
