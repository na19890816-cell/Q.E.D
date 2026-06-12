"""
frost_surface_sampler.py
------------------------
Fragility Surface Index (FSI) のパラメータ摂動サンプラー。

各候補式のパラメータ（window長/カットオフ/閾値/ボラティリティスケーリング等）を
小幅摂動し、パフォーマンス変化の「曲面サンプル」を生成する。

設計原則:
  - pure Python（numpy不使用）
  - パラメータ空間を格子状にサンプリング（軽量）
  - 各サンプル点の評価は呼び出し元が担う（コールバック方式）
  - 副作用なし

環境変数:
  FROST_FSI_ENABLED=1
  FROST_FSI_MAX=0.40
  FROST_FSI_GRID_SIZE=5         (摂動格子のサイズ: 各軸の分割数)
  FROST_FSI_PERTURB_RATIO=0.20  (摂動幅: ±20%)
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 定数・環境変数
# ---------------------------------------------------------------------------

_FSI_ENABLED: bool = os.environ.get(
    "FROST_FSI_ENABLED", "1"
).strip().lower() in ("1", "true", "yes", "on")

_FSI_MAX: float = float(os.environ.get("FROST_FSI_MAX", "0.40"))

_FSI_GRID_SIZE: int = max(2, int(os.environ.get("FROST_FSI_GRID_SIZE", "5")))

_FSI_PERTURB_RATIO: float = float(
    os.environ.get("FROST_FSI_PERTURB_RATIO", "0.20")
)


# ---------------------------------------------------------------------------
# パラメータ定義
# ---------------------------------------------------------------------------

@dataclass
class ParameterSpec:
    """
    摂動対象のパラメータ仕様。

    Attributes
    ----------
    name : str
        パラメータ名（例: "window", "cutoff", "threshold"）
    base_value : float
        ベース値（オリジナル設定値）
    min_value : float
        摂動範囲の下限（物理的制約）
    max_value : float
        摂動範囲の上限
    perturb_ratio : float
        ±摂動幅の比率（ base_value * perturb_ratio）
    is_integer : bool
        True の場合、摂動値を整数に丸める
    log_scale : bool
        True の場合、対数スケールで摂動（乗法的摂動）
    """
    name: str
    base_value: float
    min_value: float = 0.0
    max_value: float = float("inf")
    perturb_ratio: float = _FSI_PERTURB_RATIO
    is_integer: bool = False
    log_scale: bool = False


@dataclass
class SurfaceSample:
    """
    単一の摂動サンプル点。

    Attributes
    ----------
    param_values : dict
        パラメータ名 → 適用値
    perturbation_vector : dict
        パラメータ名 → ベース値からの変化率 (例: +0.15 → +15%)
    sample_index : int
        サンプル通し番号
    is_baseline : bool
        ベース値のまま（摂動なし）の場合 True
    """
    param_values: Dict[str, float]
    perturbation_vector: Dict[str, float]
    sample_index: int
    is_baseline: bool = False


@dataclass
class PerturbationGrid:
    """
    パラメータ摂動格子の仕様。

    Attributes
    ----------
    specs : list[ParameterSpec]
        摂動対象のパラメータリスト
    grid_size : int
        各軸の格子分割数（摂動ステップ数）
    include_baseline : bool
        ベース（無摂動）サンプルを含めるか
    max_samples : int
        最大サンプル数（組み合わせ爆発防止）
    """
    specs: List[ParameterSpec]
    grid_size: int = _FSI_GRID_SIZE
    include_baseline: bool = True
    max_samples: int = 500


# ---------------------------------------------------------------------------
# 標準パラメータスペック
# ---------------------------------------------------------------------------

# FROST で想定される標準的なパラメータ仕様
STANDARD_PARAM_SPECS = {
    "window": ParameterSpec(
        name="window",
        base_value=20,
        min_value=5,
        max_value=252,
        perturb_ratio=0.30,
        is_integer=True,
        log_scale=False,
    ),
    "cutoff": ParameterSpec(
        name="cutoff",
        base_value=0.10,
        min_value=0.01,
        max_value=0.50,
        perturb_ratio=0.30,
        is_integer=False,
        log_scale=False,
    ),
    "threshold": ParameterSpec(
        name="threshold",
        base_value=0.50,
        min_value=0.10,
        max_value=0.90,
        perturb_ratio=0.20,
        is_integer=False,
        log_scale=False,
    ),
    "vol_scale": ParameterSpec(
        name="vol_scale",
        base_value=1.0,
        min_value=0.5,
        max_value=2.0,
        perturb_ratio=0.25,
        is_integer=False,
        log_scale=True,
    ),
    "lookback": ParameterSpec(
        name="lookback",
        base_value=60,
        min_value=10,
        max_value=504,
        perturb_ratio=0.30,
        is_integer=True,
        log_scale=False,
    ),
    "norm_span": ParameterSpec(
        name="norm_span",
        base_value=252,
        min_value=60,
        max_value=756,
        perturb_ratio=0.25,
        is_integer=True,
        log_scale=False,
    ),
}


# ---------------------------------------------------------------------------
# 格子生成
# ---------------------------------------------------------------------------

def _linspace(start: float, stop: float, n: int) -> List[float]:
    """等差数列を生成（numpy.linspace の純 Python 代替）。"""
    if n <= 1:
        return [(start + stop) / 2]
    step = (stop - start) / (n - 1)
    return [start + i * step for i in range(n)]


def _logspace(start: float, stop: float, n: int) -> List[float]:
    """対数等差数列を生成（start/stop は実値）。"""
    if start <= 0 or stop <= 0:
        return _linspace(start, stop, n)
    log_start = math.log(start)
    log_stop = math.log(stop)
    return [math.exp(v) for v in _linspace(log_start, log_stop, n)]


def generate_perturb_range(spec: ParameterSpec, grid_size: int) -> List[float]:
    """
    ParameterSpec に基づいて摂動値のリストを生成する。

    Parameters
    ----------
    spec : ParameterSpec
    grid_size : int

    Returns
    -------
    list[float]
        摂動後の値リスト（grid_size 点）
    """
    base = spec.base_value
    ratio = spec.perturb_ratio

    lo = base * (1.0 - ratio)
    hi = base * (1.0 + ratio)

    # 物理制約を適用
    lo = max(lo, spec.min_value)
    hi = min(hi, spec.max_value)

    if lo >= hi:
        # 摂動余地なし → ベース値のみ
        return [base]

    if spec.log_scale:
        values = _logspace(lo, hi, grid_size)
    else:
        values = _linspace(lo, hi, grid_size)

    # 整数型ならば整数化・重複除去
    if spec.is_integer:
        int_vals = sorted(set(int(round(v)) for v in values))
        # 最低 1 点確保
        if not int_vals:
            int_vals = [int(round(base))]
        return [float(v) for v in int_vals]

    return values


def generate_surface_samples(grid: PerturbationGrid) -> List[SurfaceSample]:
    """
    PerturbationGrid の定義に基づいてサンプル点リストを生成する。

    全パラメータの直積（デカルト積）を取るが、
    max_samples を超えた場合は均等間引きを行う。

    Parameters
    ----------
    grid : PerturbationGrid

    Returns
    -------
    list[SurfaceSample]
    """
    if not grid.specs:
        return []

    # 各パラメータの摂動値リストを生成
    param_ranges: Dict[str, List[float]] = {}
    for spec in grid.specs:
        param_ranges[spec.name] = generate_perturb_range(spec, grid.grid_size)

    # デカルト積を生成（再帰的）
    all_combinations: List[Dict[str, float]] = [{}]
    for spec in grid.specs:
        new_combos: List[Dict[str, float]] = []
        for val in param_ranges[spec.name]:
            for combo in all_combinations:
                new_combo = dict(combo)
                new_combo[spec.name] = val
                new_combos.append(new_combo)
        all_combinations = new_combos

    # max_samples に収める（均等間引き）
    total = len(all_combinations)
    if total > grid.max_samples:
        step = total / grid.max_samples
        indices = [int(i * step) for i in range(grid.max_samples)]
        all_combinations = [all_combinations[i] for i in indices]

    # SurfaceSample に変換
    samples: List[SurfaceSample] = []
    spec_map = {s.name: s for s in grid.specs}

    # baseline 追加
    if grid.include_baseline:
        baseline_params = {s.name: s.base_value for s in grid.specs}
        baseline_perturb = {s.name: 0.0 for s in grid.specs}
        samples.append(SurfaceSample(
            param_values=baseline_params,
            perturbation_vector=baseline_perturb,
            sample_index=0,
            is_baseline=True,
        ))

    for idx, combo in enumerate(all_combinations):
        perturb_vec: Dict[str, float] = {}
        for name, val in combo.items():
            spec = spec_map[name]
            base = spec.base_value
            if base != 0.0:
                perturb_vec[name] = (val - base) / base
            else:
                perturb_vec[name] = 0.0

        samples.append(SurfaceSample(
            param_values=combo,
            perturbation_vector=perturb_vec,
            sample_index=len(samples),
            is_baseline=False,
        ))

    return samples


def build_default_grid(
    param_names: Optional[List[str]] = None,
    base_values: Optional[Dict[str, float]] = None,
    grid_size: int = _FSI_GRID_SIZE,
    max_samples: int = 200,
) -> PerturbationGrid:
    """
    標準パラメータ仕様から PerturbationGrid を構築する便利関数。

    Parameters
    ----------
    param_names : list[str], optional
        使用するパラメータ名リスト。None の場合は全標準パラメータを使用。
    base_values : dict[str, float], optional
        ベース値の上書き辞書。
    grid_size : int
    max_samples : int

    Returns
    -------
    PerturbationGrid
    """
    if param_names is None:
        param_names = list(STANDARD_PARAM_SPECS.keys())

    specs: List[ParameterSpec] = []
    for name in param_names:
        if name in STANDARD_PARAM_SPECS:
            spec = STANDARD_PARAM_SPECS[name]
            # ベース値の上書き
            if base_values and name in base_values:
                import dataclasses
                spec = dataclasses.replace(spec, base_value=base_values[name])
            specs.append(spec)
        else:
            # 不明なパラメータは ±20% の汎用仕様として追加
            bv = (base_values or {}).get(name, 1.0)
            specs.append(ParameterSpec(
                name=name,
                base_value=bv,
                min_value=bv * 0.5,
                max_value=bv * 2.0,
                perturb_ratio=_FSI_PERTURB_RATIO,
            ))

    return PerturbationGrid(
        specs=specs,
        grid_size=grid_size,
        include_baseline=True,
        max_samples=max_samples,
    )
