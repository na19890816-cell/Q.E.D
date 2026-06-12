"""
frost_fragility_surface.py
--------------------------
Fragility Surface Index (FSI) 計算本体。

frost_surface_sampler.py が生成したサンプル点に対して
評価関数を適用し、パフォーマンス変動の「局所安定曲面スコア」を算出する。

設計原則:
  - pure Python（numpy不使用）
  - コールバック方式: 評価関数は呼び出し元が差し込む
  - 1値 fragility_score の代替として「曲面」的なスコアを提供
  - 副作用なし

環境変数:
  FROST_FSI_ENABLED=1
  FROST_FSI_MAX=0.40          (FSI ハードゲート上限)
  FROST_FSI_PENALTY_SCALE=0.25 (ペナルティスケール)

関連:
  frost_surface_sampler.py  サンプル点生成
  frost_config.py           設定
  frost_metrics.py          FROST v2 スコア統合先
"""
from __future__ import annotations

import math
import os
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .frost_surface_sampler import (
    PerturbationGrid,
    SurfaceSample,
    build_default_grid,
    generate_surface_samples,
)


# ---------------------------------------------------------------------------
# 定数・環境変数
# ---------------------------------------------------------------------------

_FSI_ENABLED: bool = os.environ.get(
    "FROST_FSI_ENABLED", "1"
).strip().lower() in ("1", "true", "yes", "on")

_FSI_MAX: float = float(os.environ.get("FROST_FSI_MAX", "0.40"))
_FSI_PENALTY_SCALE: float = float(os.environ.get("FROST_FSI_PENALTY_SCALE", "0.25"))


# ---------------------------------------------------------------------------
# 評価結果データクラス
# ---------------------------------------------------------------------------

@dataclass
class SampleEvaluation:
    """
    単一サンプル点のパフォーマンス評価結果。

    Attributes
    ----------
    sample : SurfaceSample
        対応するサンプル点
    sharpe : float
        そのパラメータ設定での OOS Sharpe 比
    rank_ic : float
        そのパラメータ設定での Rank IC
    is_valid : bool
        評価が有効かどうか（データ不足等でスキップした場合 False）
    extra : dict
        追加メトリクス（任意）
    """
    sample: SurfaceSample
    sharpe: float
    rank_ic: float
    is_valid: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FragilitySurfaceResult:
    """
    Fragility Surface Index の完全な計算結果。

    Attributes
    ----------
    fragility_surface_index : float
        FSI メインスコア（0〜1、低いほど安定）
    local_stability_score : float
        局所安定スコア（0〜1、高いほど安定）
    fragility_penalty : float
        FROST v2 ペナルティ値
    baseline_sharpe : float
        ベース（無摂動）Sharpe
    baseline_rank_ic : float
        ベース Rank IC
    mean_sharpe : float
        全サンプル平均 Sharpe
    std_sharpe : float
        全サンプル Sharpe の標準偏差
    cv_sharpe : float
        変動係数 (std/mean の絶対値)
    min_sharpe : float
        最小 Sharpe
    max_sharpe : float
        最大 Sharpe
    sharpe_degradation_ratio : float
        ベース比でどれだけ劣化した（マイナス方向の最大値 / ベース）
    n_samples : int
        有効サンプル数
    n_invalid : int
        無効（スキップ）サンプル数
    breakdown : dict
        パラメータ別の感度内訳
    gate_pass : bool
        FSI <= _FSI_MAX であれば True
    gate_reason : str
        gate_pass の理由
    """
    fragility_surface_index: float
    local_stability_score: float
    fragility_penalty: float
    baseline_sharpe: float
    baseline_rank_ic: float
    mean_sharpe: float
    std_sharpe: float
    cv_sharpe: float
    min_sharpe: float
    max_sharpe: float
    sharpe_degradation_ratio: float
    n_samples: int
    n_invalid: int
    breakdown: Dict[str, Any]
    gate_pass: bool
    gate_reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fragility_surface_index": self.fragility_surface_index,
            "local_stability_score": self.local_stability_score,
            "fragility_penalty": self.fragility_penalty,
            "baseline_sharpe": self.baseline_sharpe,
            "baseline_rank_ic": self.baseline_rank_ic,
            "mean_sharpe": self.mean_sharpe,
            "std_sharpe": self.std_sharpe,
            "cv_sharpe": self.cv_sharpe,
            "min_sharpe": self.min_sharpe,
            "max_sharpe": self.max_sharpe,
            "sharpe_degradation_ratio": self.sharpe_degradation_ratio,
            "n_samples": self.n_samples,
            "n_invalid": self.n_invalid,
            "breakdown": self.breakdown,
            "gate_pass": self.gate_pass,
            "gate_reason": self.gate_reason,
        }


# ---------------------------------------------------------------------------
# FSI 計算コア
# ---------------------------------------------------------------------------

EvalFunc = Callable[[Dict[str, float]], Tuple[float, float]]
"""
評価コールバック型。

Parameters
----------
param_values : dict[str, float]
    サンプル点のパラメータ値

Returns
-------
(sharpe: float, rank_ic: float)
"""


def compute_fragility_surface(
    eval_func: EvalFunc,
    grid: Optional[PerturbationGrid] = None,
    param_names: Optional[List[str]] = None,
    base_values: Optional[Dict[str, float]] = None,
    fsi_max: float = _FSI_MAX,
) -> FragilitySurfaceResult:
    """
    Fragility Surface Index を計算する。

    Parameters
    ----------
    eval_func : callable
        (param_values: dict) → (sharpe: float, rank_ic: float) を返す評価関数。
        評価不能の場合は (float('nan'), float('nan')) を返すこと。
    grid : PerturbationGrid, optional
        摂動格子。None の場合は build_default_grid() で自動構築。
    param_names : list[str], optional
        使用するパラメータ名（grid=None の場合のみ使用）
    base_values : dict[str, float], optional
        ベース値（grid=None の場合のみ使用）
    fsi_max : float
        FSI hard gate 上限値

    Returns
    -------
    FragilitySurfaceResult
    """
    if grid is None:
        grid = build_default_grid(
            param_names=param_names,
            base_values=base_values,
        )

    samples = generate_surface_samples(grid)
    if not samples:
        return _empty_result(fsi_max=fsi_max, reason="サンプルなし")

    # 各サンプルを評価
    evaluations: List[SampleEvaluation] = []
    for sample in samples:
        try:
            sharpe_val, ric_val = eval_func(sample.param_values)
        except Exception:
            evaluations.append(SampleEvaluation(
                sample=sample, sharpe=0.0, rank_ic=0.0, is_valid=False
            ))
            continue

        is_valid = not (math.isnan(sharpe_val) or math.isinf(sharpe_val))
        evaluations.append(SampleEvaluation(
            sample=sample,
            sharpe=sharpe_val if is_valid else 0.0,
            rank_ic=ric_val if (is_valid and not math.isnan(ric_val)) else 0.0,
            is_valid=is_valid,
        ))

    valid_evals = [e for e in evaluations if e.is_valid]
    n_invalid = len(evaluations) - len(valid_evals)

    if not valid_evals:
        return _empty_result(fsi_max=fsi_max, reason="全サンプル評価失敗", n_invalid=n_invalid)

    # ベースライン取得
    baseline_evals = [e for e in valid_evals if e.sample.is_baseline]
    if baseline_evals:
        baseline_sharpe = baseline_evals[0].sharpe
        baseline_rank_ic = baseline_evals[0].rank_ic
    else:
        # baseline がない場合は中央値を使う
        sharpes = sorted(e.sharpe for e in valid_evals)
        n = len(sharpes)
        baseline_sharpe = sharpes[n // 2]
        baseline_rank_ic = 0.0

    # 非ベースラインサンプルのみで統計を計算
    non_baseline = [e for e in valid_evals if not e.sample.is_baseline]
    if not non_baseline:
        # baseline のみなら FSI=0（摂動サンプルなし）
        return _trivial_result(baseline_sharpe, baseline_rank_ic, len(valid_evals), n_invalid, fsi_max)

    sharpe_vals = [e.sharpe for e in non_baseline]
    mean_sharpe = statistics.mean(sharpe_vals)
    std_sharpe = statistics.pstdev(sharpe_vals) if len(sharpe_vals) > 1 else 0.0
    min_sharpe = min(sharpe_vals)
    max_sharpe = max(sharpe_vals)

    # 変動係数（低いほど安定）
    cv_sharpe = std_sharpe / abs(mean_sharpe) if abs(mean_sharpe) > 1e-10 else std_sharpe

    # Sharpe 劣化率（ベース比でどれだけ悪化したか、0〜1に正規化）
    if baseline_sharpe > 1e-10:
        degradation = max(0.0, baseline_sharpe - min_sharpe) / baseline_sharpe
    else:
        degradation = 0.0 if min_sharpe > 0 else 1.0
    sharpe_degradation_ratio = min(1.0, degradation)

    # FSI 計算 (0〜1): CV と劣化率の加重平均
    # CV はスケールが大きくなりすぎるため cap する
    cv_capped = min(1.0, cv_sharpe)
    fsi = 0.5 * cv_capped + 0.5 * sharpe_degradation_ratio

    # 局所安定スコア（FSI の逆数的）
    local_stability = 1.0 - fsi

    # ペナルティ
    fragility_penalty = _compute_fsi_penalty(fsi, fsi_max)

    # パラメータ別感度分析
    breakdown = _compute_param_breakdown(non_baseline, baseline_sharpe)

    # Gate 判定
    if fsi <= fsi_max:
        gate_pass = True
        gate_reason = f"FSI={fsi:.4f} <= max={fsi_max:.4f}: OK"
    else:
        gate_pass = False
        gate_reason = f"FSI={fsi:.4f} > max={fsi_max:.4f}: FRAGILE"

    return FragilitySurfaceResult(
        fragility_surface_index=fsi,
        local_stability_score=local_stability,
        fragility_penalty=fragility_penalty,
        baseline_sharpe=baseline_sharpe,
        baseline_rank_ic=baseline_rank_ic,
        mean_sharpe=mean_sharpe,
        std_sharpe=std_sharpe,
        cv_sharpe=cv_sharpe,
        min_sharpe=min_sharpe,
        max_sharpe=max_sharpe,
        sharpe_degradation_ratio=sharpe_degradation_ratio,
        n_samples=len(valid_evals),
        n_invalid=n_invalid,
        breakdown=breakdown,
        gate_pass=gate_pass,
        gate_reason=gate_reason,
    )


def _compute_fsi_penalty(fsi: float, fsi_max: float) -> float:
    """
    FSI からペナルティ値を計算する。

    FSI <= fsi_max → ペナルティなし
    FSI > fsi_max → 超過分に比例してペナルティ (0〜_FSI_PENALTY_SCALE)
    """
    if fsi <= fsi_max:
        return 0.0
    denom = 1.0 - fsi_max
    if denom < 1e-10:
        return _FSI_PENALTY_SCALE
    ratio = (fsi - fsi_max) / denom
    return min(_FSI_PENALTY_SCALE, ratio * _FSI_PENALTY_SCALE)


def _compute_param_breakdown(
    evals: List[SampleEvaluation],
    baseline_sharpe: float,
) -> Dict[str, Any]:
    """
    パラメータ別の感度を分析する。

    各パラメータを 1 軸ずつ変化させた際の Sharpe 変動幅を計算。
    """
    if not evals:
        return {}

    # パラメータ名を収集
    all_params = set()
    for e in evals:
        all_params.update(e.sample.param_values.keys())

    breakdown: Dict[str, Any] = {}
    for param_name in all_params:
        param_sharpes: Dict[float, List[float]] = {}
        for e in evals:
            pval = e.sample.param_values.get(param_name)
            if pval is not None:
                param_sharpes.setdefault(pval, []).append(e.sharpe)

        if len(param_sharpes) < 2:
            continue

        # 各パラメータ値での平均 Sharpe
        mean_by_param = {pv: statistics.mean(vs) for pv, vs in param_sharpes.items()}
        sharpe_list = list(mean_by_param.values())

        sensitivity = max(sharpe_list) - min(sharpe_list)  # Sharpe 変動幅
        if baseline_sharpe > 1e-10:
            relative_sensitivity = sensitivity / abs(baseline_sharpe)
        else:
            relative_sensitivity = 0.0

        breakdown[param_name] = {
            "sensitivity_abs": sensitivity,
            "sensitivity_rel": relative_sensitivity,
            "mean_sharpe_by_value": {
                f"{pv:.4g}": round(ms, 6) for pv, ms in sorted(mean_by_param.items())
            },
        }

    return breakdown


def _empty_result(
    fsi_max: float,
    reason: str = "データなし",
    n_invalid: int = 0,
) -> FragilitySurfaceResult:
    """データが取れなかった場合のデフォルト結果（FSI=0、stable 扱い）。"""
    return FragilitySurfaceResult(
        fragility_surface_index=0.0,
        local_stability_score=1.0,
        fragility_penalty=0.0,
        baseline_sharpe=0.0,
        baseline_rank_ic=0.0,
        mean_sharpe=0.0,
        std_sharpe=0.0,
        cv_sharpe=0.0,
        min_sharpe=0.0,
        max_sharpe=0.0,
        sharpe_degradation_ratio=0.0,
        n_samples=0,
        n_invalid=n_invalid,
        breakdown={},
        gate_pass=True,
        gate_reason=f"FSI 評価スキップ: {reason}",
    )


def _trivial_result(
    baseline_sharpe: float,
    baseline_rank_ic: float,
    n_samples: int,
    n_invalid: int,
    fsi_max: float,
) -> FragilitySurfaceResult:
    """摂動サンプルなし（baseline のみ）の場合のデフォルト結果。"""
    return FragilitySurfaceResult(
        fragility_surface_index=0.0,
        local_stability_score=1.0,
        fragility_penalty=0.0,
        baseline_sharpe=baseline_sharpe,
        baseline_rank_ic=baseline_rank_ic,
        mean_sharpe=baseline_sharpe,
        std_sharpe=0.0,
        cv_sharpe=0.0,
        min_sharpe=baseline_sharpe,
        max_sharpe=baseline_sharpe,
        sharpe_degradation_ratio=0.0,
        n_samples=n_samples,
        n_invalid=n_invalid,
        breakdown={},
        gate_pass=True,
        gate_reason="摂動サンプルなし: FSI=0.0 (trivial stable)",
    )


# ---------------------------------------------------------------------------
# FROST v2 統合ヘルパー
# ---------------------------------------------------------------------------

def fsi_to_score_components(
    result: FragilitySurfaceResult,
) -> Dict[str, float]:
    """
    FragilitySurfaceResult を FROST v2 スコアコンポーネントに変換する。

    Returns
    -------
    dict with keys:
      - fragility_surface_index : float (0〜1)
      - local_stability_score   : float (0〜1)
      - fragility_penalty       : float (0〜FSI_PENALTY_SCALE)
      - gate_pass               : bool
    """
    return {
        "fragility_surface_index": result.fragility_surface_index,
        "local_stability_score": result.local_stability_score,
        "fragility_penalty": result.fragility_penalty,
        "gate_pass": result.gate_pass,
    }


def fsi_hard_gate_pass(
    result: FragilitySurfaceResult,
    fsi_max: Optional[float] = None,
) -> Tuple[bool, str]:
    """
    FSI の hard gate 判定。

    FROST_FSI_ENABLED=0 の場合は常に pass。

    Parameters
    ----------
    result : FragilitySurfaceResult
    fsi_max : float, optional
        上限値。None の場合は環境変数 FROST_FSI_MAX を使用。

    Returns
    -------
    (pass_flag: bool, reason: str)
    """
    if not _FSI_ENABLED:
        return True, "FSI_ENABLED=0: スキップ"

    if fsi_max is None:
        fsi_max = _FSI_MAX

    if result.fragility_surface_index > fsi_max:
        return (
            False,
            f"FSI={result.fragility_surface_index:.4f} > max={fsi_max:.4f}: FRAGILE",
        )
    return True, f"FSI OK: {result.fragility_surface_index:.4f} <= {fsi_max:.4f}"


# ---------------------------------------------------------------------------
# シンプル評価関数ファクトリ（テスト・簡易利用向け）
# ---------------------------------------------------------------------------

def make_simple_eval_func(
    base_sharpe: float,
    sensitivity: float = 0.1,
    noise_seed: int = 42,
) -> EvalFunc:
    """
    テスト・簡易利用向けのシンプルな評価関数を生成する。

    パラメータ摂動に対してランダムに Sharpe が変動するシミュレーション。

    Parameters
    ----------
    base_sharpe : float
        ベース Sharpe 比
    sensitivity : float
        摂動 1 単位あたりの Sharpe 変動量
    noise_seed : int
        疑似乱数シード（再現性確保用）

    Returns
    -------
    callable
    """
    # LCG 疑似乱数（pure Python）
    _state = [noise_seed % (2**31)]

    def _lcg() -> float:
        _state[0] = (1664525 * _state[0] + 1013904223) % (2**32)
        return (_state[0] / (2**32)) * 2 - 1  # [-1, 1]

    def eval_func(param_values: Dict[str, float]) -> Tuple[float, float]:
        total_perturb = sum(abs(v) for v in param_values.values())
        noise = _lcg() * sensitivity * total_perturb
        sharpe = base_sharpe - sensitivity * total_perturb * 0.5 + noise
        rank_ic = max(-0.1, min(0.3, sharpe * 0.02 + _lcg() * 0.01))
        return sharpe, rank_ic

    return eval_func


# ---------------------------------------------------------------------------
# バッチ集計
# ---------------------------------------------------------------------------

def summarize_fsi_batch(
    results: List[FragilitySurfaceResult],
) -> Dict[str, Any]:
    """
    複数候補の FSI 結果をバッチ集計する。
    """
    if not results:
        return {"count": 0}

    fsi_vals = [r.fragility_surface_index for r in results]
    stability_vals = [r.local_stability_score for r in results]
    passed = [r for r in results if r.gate_pass]
    n = len(results)

    sorted_fsi = sorted(fsi_vals)
    mid = n // 2
    median_fsi = sorted_fsi[mid] if n % 2 == 1 else (sorted_fsi[mid - 1] + sorted_fsi[mid]) / 2

    return {
        "count": n,
        "mean_fsi": sum(fsi_vals) / n,
        "median_fsi": median_fsi,
        "max_fsi": max(fsi_vals),
        "min_fsi": min(fsi_vals),
        "mean_stability": sum(stability_vals) / n,
        "gate_pass_count": len(passed),
        "gate_fail_count": n - len(passed),
        "gate_pass_ratio": len(passed) / n,
    }
