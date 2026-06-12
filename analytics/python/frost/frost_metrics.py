"""
frost_metrics.py
----------------
FROST スコア計算モジュール。

frost_features.py で抽出した生特徴量から、
正規化済みスコアおよびペナルティ値を計算する。

設計原則:
  - 副作用なし: 計算のみ
  - 入力は extract_all_features() の戻り dict
  - 出力は FrostEvaluation に詰め込む形式の dict
  - 正規化: ロバスト z-score (median / IQR) をバッチ単位で適用
  - ペナルティ: ソフト (0〜1 のスケール) と hard gate 判定は frost_selector.py が担う
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 内部ユーティリティ
# ---------------------------------------------------------------------------

def _safe(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


def _safe_opt(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def _clip(v: float, lo: float = -3.0, hi: float = 3.0) -> float:
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# ロバスト正規化 (バッチ単位)
# ---------------------------------------------------------------------------

def robust_normalize(
    values: List[float],
    clip_min: float = -3.0,
    clip_max: float = 3.0,
) -> List[float]:
    """
    ロバスト z-score 正規化: (x - median) / (IQR/1.35)

    IQR=0 の場合は std にフォールバック。全部同値なら 0 を返す。

    Parameters
    ----------
    values : list of float
    clip_min, clip_max : float
        クリッピング範囲 (z-score)

    Returns
    -------
    list of float
        正規化・クリッピング済みスコアリスト (0〜1 ではなく z-score)
    """
    if not values:
        return []

    n = len(values)
    if n == 1:
        return [0.0]

    median = statistics.median(values)
    sorted_v = sorted(values)

    q1_idx = int(0.25 * (n - 1))
    q3_idx = int(0.75 * (n - 1))
    q1 = sorted_v[q1_idx]
    q3 = sorted_v[q3_idx]
    iqr = q3 - q1

    if iqr > 1e-10:
        scale = iqr / 1.35
    else:
        try:
            scale = statistics.stdev(values)
        except statistics.StatisticsError:
            scale = 0.0

    if scale < 1e-10:
        return [0.0] * n

    return [_clip((v - median) / scale, clip_min, clip_max) for v in values]


def zscore_to_0_1(z: float, clip_min: float = -3.0, clip_max: float = 3.0) -> float:
    """
    z-score を 0〜1 に線形変換する。
    clip_min → 0, clip_max → 1
    """
    return (z - clip_min) / (clip_max - clip_min)


# ---------------------------------------------------------------------------
# 個別スコア計算
# ---------------------------------------------------------------------------

def compute_predictive_score(feat: Dict[str, Any]) -> float:
    """
    予測力スコアを計算する (0〜1)。

    rank_ic (絶対値) をメインにし、ic で補完。
    t-stat > 2 なら信頼性ボーナス。

    Parameters
    ----------
    feat : dict
        extract_all_features() の戻り値。

    Returns
    -------
    float
        予測力スコア (0〜1)
    """
    rank_ic  = _safe_opt(feat.get("rank_ic"))
    ic       = _safe_opt(feat.get("ic"))
    ic_t     = _safe_opt(feat.get("ic_t_stat"))
    hit_rate = _safe_opt(feat.get("hit_rate"))

    if rank_ic is not None:
        base = min(abs(rank_ic) / 0.10, 1.0)   # IC=0.10 → score=1.0
    elif ic is not None:
        base = min(abs(ic) / 0.10, 1.0)
    else:
        base = 0.0

    # t-stat ボーナス (t > 2 で信頼性加点)
    t_bonus = 0.0
    if ic_t is not None and abs(ic_t) > 2.0:
        t_bonus = min((abs(ic_t) - 2.0) * 0.05, 0.1)

    # hit_rate ボーナス (0.55 超で加点)
    hit_bonus = 0.0
    if hit_rate is not None and hit_rate > 0.55:
        hit_bonus = min((hit_rate - 0.55) * 0.5, 0.1)

    return min(1.0, base + t_bonus + hit_bonus)


def compute_oos_sharpe_score(feat: Dict[str, Any], min_sharpe: float = 0.5) -> float:
    """
    OOS Sharpe → 正規化スコア (0〜1)。

    min_sharpe 以下は 0 に近づく。2.0 以上で 1.0 付近。

    Parameters
    ----------
    feat : dict
    min_sharpe : float
        最低 Sharpe (hard gate 閾値に揃える)

    Returns
    -------
    float
    """
    oos_sharpe = _safe_opt(feat.get("oos_sharpe"))
    if oos_sharpe is None:
        return 0.0

    if oos_sharpe <= 0:
        return 0.0

    # Sharpe を 0〜1 にマッピング: [0, 2.0] → [0, 1]
    return min(1.0, max(0.0, oos_sharpe / 2.0))


def compute_regime_stability_score(feat: Dict[str, Any]) -> float:
    """
    レジーム安定性スコアを計算する (0〜1)。

    全レジームで Sharpe > 0 なら高スコア。
    crisis_sharpe が高ければボーナス。

    Returns
    -------
    float
    """
    regime_pass_ratio = _safe_opt(feat.get("regime_pass_ratio_raw"))
    crisis_sharpe     = _safe_opt(feat.get("crisis_sharpe"))
    bull_sharpe       = _safe_opt(feat.get("bull_sharpe"))

    if regime_pass_ratio is None:
        # regime_breakdown がない場合は中立スコア
        return 0.5

    base = regime_pass_ratio  # 0〜1

    # crisis でも正 Sharpe ならボーナス
    crisis_bonus = 0.0
    if crisis_sharpe is not None and crisis_sharpe > 0.0:
        crisis_bonus = min(crisis_sharpe / 4.0, 0.2)

    return min(1.0, base + crisis_bonus)


def compute_capacity_score(feat: Dict[str, Any]) -> float:
    """
    キャパシティスコアを返す (frost_features.py で推定済み)。

    Returns
    -------
    float
    """
    return min(1.0, max(0.0, _safe(feat.get("capacity_score_raw", 0.5))))


# ---------------------------------------------------------------------------
# ペナルティ計算 (0〜1、小さいほど良い)
# ---------------------------------------------------------------------------

def compute_pbo_penalty(feat: Dict[str, Any], pbo_score: float) -> float:
    """
    PBO ペナルティ (0〜1)。
    pbo_score は frost_pbo.py が計算した 0〜1 の過学習確率。

    Returns
    -------
    float
    """
    return min(1.0, max(0.0, _safe(pbo_score)))


def compute_turnover_penalty(feat: Dict[str, Any], max_turnover: float = 4.0) -> float:
    """
    ターンオーバーペナルティ (0〜1)。
    max_turnover 以上で 1.0 (最大ペナルティ)。

    Returns
    -------
    float
    """
    turnover = _safe_opt(feat.get("turnover"))
    if turnover is None:
        return 0.0
    if turnover <= 0:
        return 0.0
    # 正規化: [0, max_turnover] → [0, 1]
    penalty = min(1.0, turnover / max_turnover)
    return penalty


def compute_complexity_penalty(complexity_score: float) -> float:
    """
    複雑度ペナルティ (0〜1)。
    complexity_score (0〜1) をそのままペナルティとして使用。

    Returns
    -------
    float
    """
    return min(1.0, max(0.0, _safe(complexity_score)))


def compute_drawdown_penalty(feat: Dict[str, Any], max_drawdown: float = 0.20) -> float:
    """
    ドローダウンペナルティ (0〜1)。
    oos_max_drawdown を max_drawdown で正規化。

    Returns
    -------
    float
    """
    mdd = _safe_opt(feat.get("oos_max_drawdown"))
    if mdd is None:
        return 0.0
    # drawdown は通常負値、絶対値で評価
    abs_mdd = abs(mdd)
    return min(1.0, abs_mdd / max(max_drawdown, 0.01))


def compute_fragility_penalty(
    feat: Dict[str, Any],
    fold_sharpe_std: float = 0.0,
) -> float:
    """
    脆弱性ペナルティ (0〜1)。
    fold 間の Sharpe 標準偏差を使用。

    Parameters
    ----------
    feat : dict
    fold_sharpe_std : float
        frost_stability.py が計算した fold 間 Sharpe 標準偏差。

    Returns
    -------
    float
    """
    # 標準偏差 0.5 超で高ペナルティ (基準: Sharpe ~1.0 に対して ±0.5 以内が安定)
    penalty = min(1.0, _safe(fold_sharpe_std) / 1.0)
    return penalty


# ---------------------------------------------------------------------------
# FROST スコア計算 (最終集約)
# ---------------------------------------------------------------------------

def compute_frost_score(
    predictive_score: float,
    oos_sharpe_score: float,
    regime_stability_score: float,
    selection_consistency_score: float,
    capacity_score: float,
    pbo_score: float,
    turnover_penalty: float,
    complexity_penalty: float,
    drawdown_penalty: float,
    fragility_penalty: float,
    # 重み
    w_predictive: float = 0.20,
    w_oos_sharpe: float = 0.15,
    w_regime_stability: float = 0.15,
    w_selection_consistency: float = 0.10,
    w_capacity: float = 0.10,
    w_pbo_penalty: float = 0.02,
    w_turnover_penalty: float = 0.10,
    w_complexity_penalty: float = 0.05,
    w_drawdown_penalty: float = 0.05,
    w_fragility_penalty: float = 0.03,
) -> float:
    """
    FROST meta-fitness スコアを計算する。

    frost_score = a1*pred + a2*oos_sharpe + a3*regime + a4*selection + a5*capacity
                - b1*pbo - b2*turnover - b3*complexity - b4*drawdown - b5*fragility

    Returns
    -------
    float
        frost_score (理論値: -∞ 〜 1.0、通常 0.0〜0.8 付近)
    """
    positive = (
        w_predictive           * _clip(predictive_score,            0.0, 1.0)
        + w_oos_sharpe         * _clip(oos_sharpe_score,            0.0, 1.0)
        + w_regime_stability   * _clip(regime_stability_score,      0.0, 1.0)
        + w_selection_consistency * _clip(selection_consistency_score, 0.0, 1.0)
        + w_capacity           * _clip(capacity_score,              0.0, 1.0)
    )

    negative = (
        w_pbo_penalty         * _clip(pbo_score,          0.0, 1.0)
        + w_turnover_penalty  * _clip(turnover_penalty,   0.0, 1.0)
        + w_complexity_penalty * _clip(complexity_penalty, 0.0, 1.0)
        + w_drawdown_penalty  * _clip(drawdown_penalty,   0.0, 1.0)
        + w_fragility_penalty * _clip(fragility_penalty,  0.0, 1.0)
    )

    return positive - negative


# ---------------------------------------------------------------------------
# バッチ正規化 + スコア計算
# ---------------------------------------------------------------------------

def compute_scores_for_features(
    feat: Dict[str, Any],
    pbo_score: float = 0.0,
    fold_sharpe_std: float = 0.0,
    config_dict: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """
    1 候補の特徴量辞書からスコア・ペナルティを一括計算する。

    Parameters
    ----------
    feat : dict
        extract_all_features() の戻り値
    pbo_score : float
        frost_pbo.py が計算した PBO スコア
    fold_sharpe_std : float
        frost_stability.py が計算した fold Sharpe 標準偏差
    config_dict : dict, optional
        FrostConfig.to_dict()["hard_gates"] や重み (未指定時はデフォルト)

    Returns
    -------
    dict
        FrostEvaluation に詰め込む各スコア・ペナルティ値
    """
    cfg = config_dict or {}
    max_turnover = _safe(cfg.get("max_turnover", 4.0))
    max_drawdown = _safe(cfg.get("max_drawdown", 0.20))

    predictive_score              = compute_predictive_score(feat)
    oos_sharpe_score              = compute_oos_sharpe_score(feat, cfg.get("min_oos_sharpe", 0.5))
    regime_stability_score        = compute_regime_stability_score(feat)
    capacity_score                = compute_capacity_score(feat)

    pbo_pen        = compute_pbo_penalty(feat, pbo_score)
    turnover_pen   = compute_turnover_penalty(feat, max_turnover)
    complexity_pen = compute_complexity_penalty(_safe(feat.get("complexity_score", 0.0)))
    drawdown_pen   = compute_drawdown_penalty(feat, max_drawdown)
    fragility_pen  = compute_fragility_penalty(feat, fold_sharpe_std)

    return {
        "predictive_score":     predictive_score,
        "oos_sharpe_score":     oos_sharpe_score,
        "regime_stability_score": regime_stability_score,
        "capacity_score":       capacity_score,
        "pbo_score":            pbo_pen,
        "turnover_penalty":     turnover_pen,
        "complexity_penalty":   complexity_pen,
        "drawdown_penalty":     drawdown_pen,
        "fragility_penalty":    fragility_pen,
    }


# ---------------------------------------------------------------------------
# FROST v2 スコア計算
# ---------------------------------------------------------------------------

def compute_frost_score_v2(
    # v1 継承: 正スコア軸
    predictive_score: float,
    oos_sharpe_score: float,
    regime_stability_score: float,
    selection_consistency_score: float,
    capacity_score: float,
    # v2 追加: 正スコア軸
    genome_novelty_score: float = 0.5,
    causal_validity_score: float = 0.5,
    regime_entropy_score: float = 0.5,
    # v1 継承: ペナルティ軸
    pbo_penalty: float = 0.0,
    turnover_penalty: float = 0.0,
    complexity_penalty: float = 0.0,
    drawdown_penalty: float = 0.0,
    fragility_penalty: float = 0.0,
    # v2 追加: ペナルティ軸
    crowding_penalty: float = 0.0,
    signal_duplication_penalty: float = 0.0,
    fragility_surface_penalty: float = 0.0,
    # 重み: v1 正スコア (a1–a5)
    w_predictive: float = 0.20,
    w_oos_sharpe: float = 0.15,
    w_regime_stability: float = 0.15,
    w_selection_consistency: float = 0.10,
    w_capacity: float = 0.10,
    # 重み: v2 追加正スコア (a6–a8)
    w_genome_novelty: float = 0.05,
    w_causal_validity: float = 0.05,
    w_regime_entropy: float = 0.05,
    # 重み: v1 ペナルティ (b1–b4)
    w_pbo: float = 0.10,
    w_turnover: float = 0.05,
    w_complexity: float = 0.03,
    w_drawdown: float = 0.05,
    # 重み: v1 fragility ペナルティ (b5-v1)
    w_fragility: float = 0.02,
    # 重み: v2 追加ペナルティ (b5-v2 / b6 / b7)
    w_crowding_penalty: float = 0.05,
    w_signal_duplication_penalty: float = 0.03,
    w_fragility_surface_penalty: float = 0.02,
) -> float:
    """
    FROST v2 スコア計算。

    式:
        frost_score_v2
            = a1*predictive + a2*oos_sharpe + a3*regime_stability
            + a4*selection_consistency + a5*capacity
            + a6*genome_novelty + a7*causal_validity + a8*regime_entropy
            - b1*pbo - b2*turnover - b3*complexity - b4*drawdown
            - b5v1*fragility
            - b5v2*fragility_surface - b6*crowding - b7*signal_duplication

    Parameters
    ----------
    predictive_score : float
        予測力スコア (0–1)
    oos_sharpe_score : float
        OOS Sharpe スコア (0–1)
    regime_stability_score : float
        レジーム安定性スコア (0–1)
    selection_consistency_score : float
        選択一貫性スコア (0–1)
    capacity_score : float
        容量スコア (0–1)
    genome_novelty_score : float
        Genome 新規性スコア (0–1)。デフォルト 0.5 = 中立
    causal_validity_score : float
        因果有効性スコア (0–1)。デフォルト 0.5 = 中立
    regime_entropy_score : float
        Regime エントロピースコア (0–1)。デフォルト 0.5 = 中立
    pbo_penalty : float
        PBO ペナルティ (0–1)
    turnover_penalty : float
        ターンオーバーペナルティ (0–1)
    complexity_penalty : float
        複雑度ペナルティ (0–1)
    drawdown_penalty : float
        ドローダウンペナルティ (0–1)
    fragility_penalty : float
        fragility ペナルティ v1 (0–1)
    crowding_penalty : float
        混雑ペナルティ (0–1)
    signal_duplication_penalty : float
        シグナル重複ペナルティ (0–1)
    fragility_surface_penalty : float
        FSI ペナルティ (0–1)

    Returns
    -------
    float
        FROST v2 総合スコア（クランプなし。通常 -1 〜 +1 程度）
    """
    # 入力を安全な float に変換
    def _s(v: Any, d: float = 0.0) -> float:
        try:
            f = float(v)
            return d if (math.isnan(f) or math.isinf(f)) else f
        except (TypeError, ValueError):
            return d

    # ── 正方向スコアの加重合計 ─────────────────────────────────────────
    positive_sum = (
        _s(w_predictive)            * _s(predictive_score)
        + _s(w_oos_sharpe)          * _s(oos_sharpe_score)
        + _s(w_regime_stability)    * _s(regime_stability_score)
        + _s(w_selection_consistency) * _s(selection_consistency_score)
        + _s(w_capacity)            * _s(capacity_score)
        + _s(w_genome_novelty)      * _s(genome_novelty_score, 0.5)
        + _s(w_causal_validity)     * _s(causal_validity_score, 0.5)
        + _s(w_regime_entropy)      * _s(regime_entropy_score, 0.5)
    )

    # ── ペナルティの加重合計 ──────────────────────────────────────────
    penalty_sum = (
        _s(w_pbo)                        * _s(pbo_penalty)
        + _s(w_turnover)                 * _s(turnover_penalty)
        + _s(w_complexity)               * _s(complexity_penalty)
        + _s(w_drawdown)                 * _s(drawdown_penalty)
        + _s(w_fragility)                * _s(fragility_penalty)
        + _s(w_crowding_penalty)         * _s(crowding_penalty)
        + _s(w_signal_duplication_penalty) * _s(signal_duplication_penalty)
        + _s(w_fragility_surface_penalty)  * _s(fragility_surface_penalty)
    )

    return positive_sum - penalty_sum


def compute_scores_for_features_v2(
    feat: Dict[str, Any],
    pbo_score: float = 0.0,
    fold_sharpe_std: float = 0.0,
    config_dict: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """
    1 候補の特徴量辞書から FROST v2 スコア・ペナルティを一括計算する。

    v1 の compute_scores_for_features() を拡張し、以下の v2 追加軸を含む:
    - genome_novelty_score    (feat["genome_novelty_score"])
    - causal_validity_score   (feat["causal_validity_score"])
    - regime_entropy_score    (feat["regime_entropy_score"])
    - crowding_penalty        (feat["crowding_penalty"])
    - signal_duplication_penalty (feat["signal_duplication_penalty"])
    - fragility_surface_penalty  (feat["fragility_surface_penalty"])

    v2 追加キーが feat に存在しない場合はデフォルト中立値を使用する。

    Parameters
    ----------
    feat : dict
        extract_all_features() の戻り値に v2 特徴量を追加したもの
    pbo_score : float
        frost_pbo.py が計算した PBO スコア
    fold_sharpe_std : float
        frost_stability.py が計算した fold Sharpe 標準偏差
    config_dict : dict, optional
        FrostConfig のフィールドを flat dict にしたもの（重み / gate 閾値含む）

    Returns
    -------
    dict
        FrostEvaluation v2 に詰め込む各スコア・ペナルティ値
    """
    cfg = config_dict or {}
    max_turnover = _safe(cfg.get("max_turnover", 4.0))
    max_drawdown = _safe(cfg.get("max_drawdown", 0.20))

    # ── v1 継承スコア ────────────────────────────────────────────────
    predictive_score          = compute_predictive_score(feat)
    oos_sharpe_score          = compute_oos_sharpe_score(feat, cfg.get("min_oos_sharpe", 0.5))
    regime_stability_score    = compute_regime_stability_score(feat)
    capacity_score            = compute_capacity_score(feat)
    selection_consistency_score = _safe(feat.get("selection_consistency_score", 0.5))

    pbo_pen        = compute_pbo_penalty(feat, pbo_score)
    turnover_pen   = compute_turnover_penalty(feat, max_turnover)
    complexity_pen = compute_complexity_penalty(_safe(feat.get("complexity_score", 0.0)))
    drawdown_pen   = compute_drawdown_penalty(feat, max_drawdown)
    fragility_pen  = compute_fragility_penalty(feat, fold_sharpe_std)

    # ── v2 追加スコア ────────────────────────────────────────────────
    genome_novelty_score  = _safe(feat.get("genome_novelty_score",  0.5))
    causal_validity_score = _safe(feat.get("causal_validity_score", 0.5))
    regime_entropy_score  = _safe(feat.get("regime_entropy_score",  0.5))

    crowding_penalty             = _safe(feat.get("crowding_penalty",              0.0))
    signal_duplication_penalty   = _safe(feat.get("signal_duplication_penalty",    0.0))
    fragility_surface_penalty    = _safe(feat.get("fragility_surface_penalty",     0.0))

    # ── 重みを config から取得（デフォルトは FrostConfig デフォルト値）───
    w = {
        "w_predictive":                 _safe(cfg.get("w_predictive",                 0.20)),
        "w_oos_sharpe":                 _safe(cfg.get("w_oos_sharpe",                 0.15)),
        "w_regime_stability":           _safe(cfg.get("w_regime_stability",           0.15)),
        "w_selection_consistency":      _safe(cfg.get("w_selection_consistency",      0.10)),
        "w_capacity":                   _safe(cfg.get("w_capacity",                   0.10)),
        "w_genome_novelty":             _safe(cfg.get("w_genome_novelty",             0.05)),
        "w_causal_validity":            _safe(cfg.get("w_causal_validity",            0.05)),
        "w_regime_entropy":             _safe(cfg.get("w_regime_entropy",             0.05)),
        "w_pbo":                        _safe(cfg.get("w_pbo",                        0.10)),
        "w_turnover":                   _safe(cfg.get("w_turnover",                   0.05)),
        "w_complexity":                 _safe(cfg.get("w_complexity",                 0.03)),
        "w_drawdown":                   _safe(cfg.get("w_drawdown",                   0.05)),
        "w_fragility":                  _safe(cfg.get("w_fragility",                  0.02)),
        "w_crowding_penalty":           _safe(cfg.get("w_crowding_penalty",           0.05)),
        "w_signal_duplication_penalty": _safe(cfg.get("w_signal_duplication_penalty", 0.03)),
        "w_fragility_surface_penalty":  _safe(cfg.get("w_fragility_surface_penalty",  0.02)),
    }

    frost_v2 = compute_frost_score_v2(
        predictive_score=predictive_score,
        oos_sharpe_score=oos_sharpe_score,
        regime_stability_score=regime_stability_score,
        selection_consistency_score=selection_consistency_score,
        capacity_score=capacity_score,
        genome_novelty_score=genome_novelty_score,
        causal_validity_score=causal_validity_score,
        regime_entropy_score=regime_entropy_score,
        pbo_penalty=pbo_pen,
        turnover_penalty=turnover_pen,
        complexity_penalty=complexity_pen,
        drawdown_penalty=drawdown_pen,
        fragility_penalty=fragility_pen,
        crowding_penalty=crowding_penalty,
        signal_duplication_penalty=signal_duplication_penalty,
        fragility_surface_penalty=fragility_surface_penalty,
        **w,
    )

    return {
        # v1 継承
        "predictive_score":              predictive_score,
        "oos_sharpe_score":              oos_sharpe_score,
        "regime_stability_score":        regime_stability_score,
        "selection_consistency_score":   selection_consistency_score,
        "capacity_score":                capacity_score,
        "pbo_score":                     pbo_pen,
        "turnover_penalty":              turnover_pen,
        "complexity_penalty":            complexity_pen,
        "drawdown_penalty":              drawdown_pen,
        "fragility_penalty":             fragility_pen,
        # v2 追加
        "genome_novelty_score":          genome_novelty_score,
        "causal_validity_score":         causal_validity_score,
        "regime_entropy_score":          regime_entropy_score,
        "crowding_penalty":              crowding_penalty,
        "signal_duplication_penalty":    signal_duplication_penalty,
        "fragility_surface_penalty":     fragility_surface_penalty,
        # v2 総合スコア
        "frost_score_v2":                frost_v2,
    }
