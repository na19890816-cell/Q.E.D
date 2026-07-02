"""
frost_crowding.py
-----------------
Crowding Detector: 候補シグナルの既知因子への露出を算出する。

目的:
  - 候補リターン/シグナルを既知因子群へ回帰し crowding 度を測定
  - crowding_r2 / beta_concentration / factor_overlap_score を算出
  - FROST v2 の crowding_penalty として統合

実装方針:
  - Phase 7 numpy 化 (ADR-001 対象): _ols_simple を np.linalg.lstsq に置換
  - 複数因子に対して 1 本ずつ回帰してスコアを集計

環境変数:
  FROST_CROWDING_ENABLED=1
  FROST_CROWDING_R2_MAX=0.80
  FROST_CROWDING_PENALTY_SCALE=0.30
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .frost_known_factor_library import (
    KNOWN_FACTOR_LIBRARY,
    KnownFactor,
    match_formula_to_factors,
)


# ---------------------------------------------------------------------------
# 定数・環境変数
# ---------------------------------------------------------------------------

_CROWDING_ENABLED: bool = os.environ.get(
    "FROST_CROWDING_ENABLED", "1"
).strip().lower() in ("1", "true", "yes", "on")

_CROWDING_R2_MAX: float = float(os.environ.get("FROST_CROWDING_R2_MAX", "0.80"))
_CROWDING_PENALTY_SCALE: float = float(
    os.environ.get("FROST_CROWDING_PENALTY_SCALE", "0.30")
)


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class FactorRegression:
    """
    1因子への単回帰結果。

    Attributes
    ----------
    factor_id : str
    beta : float
        回帰係数
    alpha : float
        切片（idiosyncratic return の代理）
    r_squared : float
        決定係数 (0〜1)
    residual_std : float
        残差の標準偏差
    n_obs : int
        観測数
    """
    factor_id: str
    beta: float
    alpha: float
    r_squared: float
    residual_std: float
    n_obs: int


@dataclass
class CrowdingScore:
    """
    Crowding Detector の候補別スコア。

    Attributes
    ----------
    candidate_id : str
    crowding_r2 : float
        全因子への最大 R² （候補が既知因子に"飲み込まれている"度合い）
    beta_concentration_score : float
        特定因子への beta 集中度スコア（0〜1）
    factor_overlap_score : float
        マッチした因子の数・強度に基づく重複スコア（0〜1）
    crowding_penalty : float
        FROST v2 ペナルティ値
    top_factor_id : Optional[str]
        最も類似した既知因子 ID
    top_factor_r2 : float
        top_factor の R²
    regressions : list[FactorRegression]
        全因子への回帰結果
    gate_pass : bool
        crowding_r2 <= _CROWDING_R2_MAX なら True
    gate_reason : str
    """
    candidate_id: str
    crowding_r2: float
    beta_concentration_score: float
    factor_overlap_score: float
    crowding_penalty: float
    top_factor_id: Optional[str]
    top_factor_r2: float
    regressions: List[FactorRegression]
    gate_pass: bool
    gate_reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "crowding_r2": self.crowding_r2,
            "beta_concentration_score": self.beta_concentration_score,
            "factor_overlap_score": self.factor_overlap_score,
            "crowding_penalty": self.crowding_penalty,
            "top_factor_id": self.top_factor_id,
            "top_factor_r2": self.top_factor_r2,
            "gate_pass": self.gate_pass,
            "gate_reason": self.gate_reason,
        }


# ---------------------------------------------------------------------------
# numpy OLS 単回帰 (Phase 7 高速化)
# ---------------------------------------------------------------------------

def _ols_simple(
    y: List[float],
    x: List[float],
) -> Tuple[float, float, float, float]:
    """
    単回帰 y = alpha + beta * x + epsilon を OLS で推定する。

    Phase 7 numpy 化: 純 Python ループを np.linalg.lstsq に置換。
    公開シグネチャ・戻り値型は変更なし。

    Returns
    -------
    (alpha, beta, r_squared, residual_std)
    """
    n = len(y)
    if n < 3:
        return 0.0, 0.0, 0.0, 0.0

    ya = np.array(y, dtype=np.float64)
    xa = np.array(x, dtype=np.float64)

    # デザイン行列 [1, x]
    A = np.column_stack([np.ones(n, dtype=np.float64), xa])
    result, _, _, _ = np.linalg.lstsq(A, ya, rcond=None)
    alpha_val, beta_val = float(result[0]), float(result[1])

    # R² 計算
    y_pred = alpha_val + beta_val * xa
    ss_res = float(np.sum((ya - y_pred) ** 2))
    ss_tot = float(np.sum((ya - ya.mean()) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 1e-15 else 0.0
    r_squared = max(0.0, min(1.0, r_squared))

    # 残差標準偏差
    residual_std = math.sqrt(ss_res / max(1, n - 2))

    return alpha_val, beta_val, r_squared, residual_std


# ---------------------------------------------------------------------------
# シグナルプロキシ（formula_text → 疑似シグナル列）
# ---------------------------------------------------------------------------

def _generate_factor_proxy_series(
    factor: KnownFactor,
    n: int = 60,
    seed_base: int = 12345,
) -> List[float]:
    """
    既知因子のプロキシリターン系列を生成する（LCG 疑似乱数）。

    実運用ではここを実際の因子リターン系列に差し替える。
    factor_id ごとに seed を変えて差別化する。

    Parameters
    ----------
    factor : KnownFactor
    n : int
        系列長
    seed_base : int

    Returns
    -------
    list[float]
    """
    # factor_id の文字コード和を seed に加算
    factor_seed = seed_base + sum(ord(c) for c in factor.factor_id)
    state = factor_seed % (2**31)

    series = []
    for _ in range(n):
        state = (1664525 * state + 1013904223) % (2**32)
        val = (state / (2**32)) * 2 - 1  # [-1, 1]
        # ファミリー別に傾向を付ける（簡易）
        if factor.factor_family == "momentum":
            val = val * 0.8 + 0.1
        elif factor.factor_family == "value":
            val = val * 0.7
        elif factor.factor_family == "quality":
            val = val * 0.6 + 0.05
        series.append(val)

    return series


def _generate_candidate_signal_series(
    formula_text: str,
    n: int = 60,
    seed_base: int = 99999,
) -> List[float]:
    """
    formula_text から疑似候補シグナル系列を生成する。

    実運用ではここを実際の OOS シグナル系列に差し替える。
    """
    seed = seed_base + sum(ord(c) for c in formula_text[:50])
    state = seed % (2**31)

    series = []
    for _ in range(n):
        state = (1664525 * state + 1013904223) % (2**32)
        series.append((state / (2**32)) * 2 - 1)

    return series


# ---------------------------------------------------------------------------
# Crowding スコア計算
# ---------------------------------------------------------------------------

def compute_crowding_score(
    candidate_id: str,
    formula_text: str,
    candidate_signal: Optional[List[float]] = None,
    factor_signals: Optional[Dict[str, List[float]]] = None,
    crowding_r2_max: float = _CROWDING_R2_MAX,
    n_series: int = 60,
) -> CrowdingScore:
    """
    候補シグナルの既知因子への crowding を計算する。

    Parameters
    ----------
    candidate_id : str
    formula_text : str
    candidate_signal : list[float], optional
        OOS シグナル系列。None の場合は formula_text から擬似生成。
    factor_signals : dict[str, list[float]], optional
        既知因子のシグナル系列。None の場合は擬似生成。
    crowding_r2_max : float
        hard gate 上限
    n_series : int
        シグナル系列長（擬似生成時）

    Returns
    -------
    CrowdingScore
    """
    if not _CROWDING_ENABLED:
        return _disabled_result(candidate_id)

    # 候補シグナル
    if candidate_signal is None:
        candidate_signal = _generate_candidate_signal_series(formula_text, n=n_series)

    # キーワードマッチで関連因子を絞り込む
    matched_factors = match_formula_to_factors(formula_text, min_match_score=0.0)
    # スコア上位因子と全因子を回帰対象とする
    target_factors = KNOWN_FACTOR_LIBRARY

    regressions: List[FactorRegression] = []
    for factor in target_factors:
        # 因子シグナル
        if factor_signals and factor.factor_id in factor_signals:
            fx = factor_signals[factor.factor_id]
        else:
            fx = _generate_factor_proxy_series(factor, n=len(candidate_signal))

        n_common = min(len(candidate_signal), len(fx))
        if n_common < 5:
            continue

        y = candidate_signal[:n_common]
        x = fx[:n_common]

        alpha, beta, r2, res_std = _ols_simple(y, x)
        regressions.append(FactorRegression(
            factor_id=factor.factor_id,
            beta=beta,
            alpha=alpha,
            r_squared=r2,
            residual_std=res_std,
            n_obs=n_common,
        ))

    if not regressions:
        return _no_data_result(candidate_id)

    # crowding_r2: 全因子の最大 R²
    crowding_r2 = max(r.r_squared for r in regressions)
    top_regression = max(regressions, key=lambda r: r.r_squared)
    top_factor_id = top_regression.factor_id
    top_factor_r2 = top_regression.r_squared

    # beta 集中度スコア: 上位 beta が全体に占める比率
    betas_abs = sorted([abs(r.beta) for r in regressions], reverse=True)
    total_beta = sum(betas_abs)
    if total_beta > 1e-10 and len(betas_abs) >= 2:
        # 上位 3 因子の beta が全体の何割を占めるか
        top3_beta = sum(betas_abs[:3])
        beta_concentration = top3_beta / total_beta
    else:
        beta_concentration = 1.0

    # factor_overlap_score: キーワードマッチスコアを正規化
    matched_score_sum = sum(s for _, s in matched_factors)
    factor_overlap = min(1.0, matched_score_sum / max(1, len(KNOWN_FACTOR_LIBRARY)))

    # crowding_penalty
    crowding_penalty = _compute_crowding_penalty(crowding_r2, crowding_r2_max)

    # gate 判定
    if crowding_r2 <= crowding_r2_max:
        gate_pass = True
        gate_reason = f"crowding_r2={crowding_r2:.4f} <= max={crowding_r2_max:.4f}: OK"
    else:
        gate_pass = False
        gate_reason = (
            f"crowding_r2={crowding_r2:.4f} > max={crowding_r2_max:.4f}: "
            f"CROWDED (top_factor={top_factor_id})"
        )

    return CrowdingScore(
        candidate_id=candidate_id,
        crowding_r2=crowding_r2,
        beta_concentration_score=beta_concentration,
        factor_overlap_score=factor_overlap,
        crowding_penalty=crowding_penalty,
        top_factor_id=top_factor_id,
        top_factor_r2=top_factor_r2,
        regressions=regressions,
        gate_pass=gate_pass,
        gate_reason=gate_reason,
    )


def _compute_crowding_penalty(r2: float, r2_max: float) -> float:
    """crowding_r2 からペナルティ値を計算する。"""
    if r2 <= r2_max:
        return 0.0
    denom = 1.0 - r2_max
    if denom < 1e-10:
        return _CROWDING_PENALTY_SCALE
    ratio = (r2 - r2_max) / denom
    return min(_CROWDING_PENALTY_SCALE, ratio * _CROWDING_PENALTY_SCALE)


def _disabled_result(candidate_id: str) -> CrowdingScore:
    return CrowdingScore(
        candidate_id=candidate_id,
        crowding_r2=0.0,
        beta_concentration_score=0.0,
        factor_overlap_score=0.0,
        crowding_penalty=0.0,
        top_factor_id=None,
        top_factor_r2=0.0,
        regressions=[],
        gate_pass=True,
        gate_reason="CROWDING_ENABLED=0: スキップ",
    )


def _no_data_result(candidate_id: str) -> CrowdingScore:
    return CrowdingScore(
        candidate_id=candidate_id,
        crowding_r2=0.0,
        beta_concentration_score=0.0,
        factor_overlap_score=0.0,
        crowding_penalty=0.0,
        top_factor_id=None,
        top_factor_r2=0.0,
        regressions=[],
        gate_pass=True,
        gate_reason="データ不足: crowding 評価スキップ",
    )


# ---------------------------------------------------------------------------
# FROST v2 統合ヘルパー
# ---------------------------------------------------------------------------

def crowding_to_frost_features(score: CrowdingScore) -> Dict[str, float]:
    """
    CrowdingScore を FROST v2 特徴量辞書に変換する。
    """
    return {
        "crowding_r2": score.crowding_r2,
        "beta_concentration_score": score.beta_concentration_score,
        "factor_overlap_score": score.factor_overlap_score,
        "crowding_penalty": score.crowding_penalty,
    }


def summarize_crowding_batch(
    scores: List[CrowdingScore],
) -> Dict[str, Any]:
    """複数候補の crowding スコアをバッチ集計する。"""
    if not scores:
        return {"count": 0}

    r2_vals = [s.crowding_r2 for s in scores]
    passed = [s for s in scores if s.gate_pass]
    n = len(scores)

    return {
        "count": n,
        "mean_crowding_r2": sum(r2_vals) / n,
        "max_crowding_r2": max(r2_vals),
        "gate_pass_count": len(passed),
        "gate_fail_count": n - len(passed),
        "gate_pass_ratio": len(passed) / n,
        "top_crowded": [
            {"candidate_id": s.candidate_id, "r2": s.crowding_r2, "top_factor": s.top_factor_id}
            for s in sorted(scores, key=lambda x: -x.crowding_r2)[:5]
        ],
    }
