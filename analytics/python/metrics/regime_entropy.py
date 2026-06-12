"""
regime_entropy.py
-----------------
Regime Entropy（レジームエントロピー）計算モジュール。

既存の regime_pass_ratio が「どのレジームでも通ったか」を示すのに対し、
regime_entropy は「どのレジームに偏らず均等に通ったか」を情報理論的に測定する。

設計原則:
  - pure Python（numpy/pandas 不使用）
  - 副作用なし
  - FROST v2 への統合が主目的
  - 環境変数: FROST_REGIME_ENTROPY_ENABLED, FROST_REGIME_ENTROPY_MIN

関連モジュール:
  - analytics/python/metrics/regime.py  (既存レジーム指標)
  - analytics/python/frost/frost_config.py (設定)
  - analytics/python/frost/frost_metrics.py (FROST スコアへの統合先)
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 定数・環境変数
# ---------------------------------------------------------------------------

_REGIME_ENTROPY_ENABLED: bool = os.environ.get(
    "FROST_REGIME_ENTROPY_ENABLED", "1"
).strip().lower() in ("1", "true", "yes", "on")

_REGIME_ENTROPY_MIN: float = float(
    os.environ.get("FROST_REGIME_ENTROPY_MIN", "0.60")
)

# エントロピー計算に含める最小レジーム数
_MIN_REGIME_COUNT: int = 2


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class RegimeEntropyResult:
    """
    レジームエントロピー計算の完全な結果。

    Attributes
    ----------
    regime_sharpes : dict
        レジーム名 → Sharpe 比 のマッピング
    regime_weights : dict
        レジーム名 → 相対重み（正規化済み確率）のマッピング
    raw_entropy : float
        未正規化エントロピー（ナット）: -Σ p_i * ln(p_i)
    normalized_entropy : float
        正規化エントロピー（0〜1）: raw_entropy / ln(N)
    balance_score : float
        FROST v2 統合用バランススコア（0〜1）
    entropy_bonus : float
        FROST v2 へのボーナス加点（normalized_entropy > threshold の分）
    imbalance_penalty : float
        FROST v2 へのペナルティ（normalized_entropy < threshold の分）
    n_regimes : int
        有効レジーム数
    max_entropy : float
        理論最大エントロピー: ln(N)
    threshold_used : float
        ボーナス/ペナルティ判定に使用した閾値
    dominant_regime : Optional[str]
        最も重みが大きいレジーム名（偏在の可視化用）
    dominant_weight : float
        dominant_regime の重み
    is_balanced : bool
        normalized_entropy >= threshold_used であれば True
    """
    regime_sharpes: Dict[str, float]
    regime_weights: Dict[str, float]
    raw_entropy: float
    normalized_entropy: float
    balance_score: float
    entropy_bonus: float
    imbalance_penalty: float
    n_regimes: int
    max_entropy: float
    threshold_used: float
    dominant_regime: Optional[str]
    dominant_weight: float
    is_balanced: bool

    def to_dict(self) -> Dict[str, object]:
        return {
            "regime_sharpes": self.regime_sharpes,
            "regime_weights": self.regime_weights,
            "raw_entropy": self.raw_entropy,
            "normalized_entropy": self.normalized_entropy,
            "balance_score": self.balance_score,
            "entropy_bonus": self.entropy_bonus,
            "imbalance_penalty": self.imbalance_penalty,
            "n_regimes": self.n_regimes,
            "max_entropy": self.max_entropy,
            "threshold_used": self.threshold_used,
            "dominant_regime": self.dominant_regime,
            "dominant_weight": self.dominant_weight,
            "is_balanced": self.is_balanced,
        }


# ---------------------------------------------------------------------------
# コア計算関数
# ---------------------------------------------------------------------------

def compute_regime_entropy(regime_sharpes: Dict[str, float]) -> float:
    """
    レジームシャープ比マッピングからレジームエントロピーを計算する。

    各レジームの「重み」は abs(sharpe) をソフトマックス的に正規化したもの。
    sharpe が正のレジームのみを有効とし、通過できたレジームの分布の偏りを測定。

    式: H = -Σ p_i * ln(p_i)

    Parameters
    ----------
    regime_sharpes : dict[str, float]
        レジーム名 → Sharpe 比（正 = そのレジームで機能する）

    Returns
    -------
    float
        未正規化エントロピー（0 以上）。
        レジームが 1 つ以下または全 sharpe ≤ 0 の場合は 0.0。

    Examples
    --------
    >>> compute_regime_entropy({"bull": 1.0, "bear": 1.0, "crisis": 1.0})
    1.0986...  # ln(3) ≈ 1.0986（最大エントロピー）

    >>> compute_regime_entropy({"bull": 2.0, "bear": 0.0, "crisis": -0.5})
    0.0  # bull のみ有効 → 偏在 → entropy=0
    """
    if not regime_sharpes:
        return 0.0

    # 正の Sharpe のみを有効レジームとして扱う
    positive = {k: v for k, v in regime_sharpes.items() if v > 0.0}
    n = len(positive)

    if n < _MIN_REGIME_COUNT:
        return 0.0

    total = sum(positive.values())
    if total <= 0.0:
        return 0.0

    # 確率分布として正規化
    probs = [v / total for v in positive.values()]

    # Shannon エントロピー（ナット単位）
    entropy = 0.0
    for p in probs:
        if p > 1e-15:
            entropy -= p * math.log(p)

    return entropy


def compute_normalized_regime_entropy(regime_sharpes: Dict[str, float]) -> float:
    """
    正規化レジームエントロピーを計算する（0〜1）。

    H_norm = H / ln(N)  where N = 有効レジーム数

    N = 1 の場合は 0.0、全均等分布なら 1.0。

    Parameters
    ----------
    regime_sharpes : dict[str, float]
        レジーム名 → Sharpe 比

    Returns
    -------
    float
        正規化エントロピー（0〜1）

    Examples
    --------
    >>> compute_normalized_regime_entropy({"a": 1.0, "b": 1.0})
    1.0  # 均等 2レジーム → 最大

    >>> compute_normalized_regime_entropy({"a": 10.0, "b": 0.001})
    # b の影響がほぼゼロ → 低エントロピー → 0 に近い
    """
    if not regime_sharpes:
        return 0.0

    positive = {k: v for k, v in regime_sharpes.items() if v > 0.0}
    n = len(positive)

    if n < _MIN_REGIME_COUNT:
        return 0.0

    max_entropy = math.log(n)
    if max_entropy < 1e-15:
        return 0.0

    raw = compute_regime_entropy(positive)
    return min(1.0, raw / max_entropy)


def compute_regime_balance_score(regime_sharpes: Dict[str, float]) -> float:
    """
    FROST v2 統合用のレジームバランススコアを計算する（0〜1）。

    正規化エントロピーをベースに、有効レジーム数によるスケール補正を行う。
    少ないレジーム数でも均等なら中程度のスコアを返す。

    スケール補正:
      N=1 → max_factor=0.3
      N=2 → max_factor=0.7
      N≥3 → max_factor=1.0

    Parameters
    ----------
    regime_sharpes : dict[str, float]
        レジーム名 → Sharpe 比

    Returns
    -------
    float
        バランススコア（0〜1）。FROST v2 の w_regime_entropy_score に使用。
    """
    if not regime_sharpes:
        return 0.0

    positive = {k: v for k, v in regime_sharpes.items() if v > 0.0}
    n = len(positive)

    if n == 0:
        return 0.0
    if n == 1:
        # 1レジームのみでは本質的に偏在
        return 0.0

    norm_entropy = compute_normalized_regime_entropy(positive)

    # レジーム数による係数
    if n >= 3:
        max_factor = 1.0
    else:
        # n=2
        max_factor = 0.85

    return min(1.0, norm_entropy * max_factor)


def compute_regime_entropy_bonus(
    normalized_entropy: float,
    threshold: float = _REGIME_ENTROPY_MIN,
    bonus_scale: float = 0.20,
) -> float:
    """
    正規化エントロピーが閾値を超えた場合のボーナス値を計算する。

    ボーナス式: max(0, (normalized_entropy - threshold) / (1 - threshold)) * bonus_scale

    Parameters
    ----------
    normalized_entropy : float
        compute_normalized_regime_entropy() の戻り値（0〜1）
    threshold : float
        ボーナス発生閾値（デフォルト: FROST_REGIME_ENTROPY_MIN）
    bonus_scale : float
        ボーナスの最大値スケール（デフォルト: 0.20）

    Returns
    -------
    float
        ボーナス値（0〜bonus_scale）

    Examples
    --------
    >>> compute_regime_entropy_bonus(0.80, threshold=0.60, bonus_scale=0.20)
    0.10  # (0.80 - 0.60) / (1.0 - 0.60) * 0.20 = 0.10
    """
    if normalized_entropy <= threshold:
        return 0.0
    denom = 1.0 - threshold
    if denom < 1e-15:
        return 0.0
    ratio = (normalized_entropy - threshold) / denom
    return min(bonus_scale, ratio * bonus_scale)


def compute_regime_imbalance_penalty(
    normalized_entropy: float,
    threshold: float = _REGIME_ENTROPY_MIN,
    penalty_scale: float = 0.30,
) -> float:
    """
    正規化エントロピーが閾値を下回った場合のペナルティ値を計算する。

    ペナルティ式: max(0, (threshold - normalized_entropy) / threshold) * penalty_scale

    Parameters
    ----------
    normalized_entropy : float
        compute_normalized_regime_entropy() の戻り値（0〜1）
    threshold : float
        ペナルティ発生閾値（デフォルト: FROST_REGIME_ENTROPY_MIN）
    penalty_scale : float
        ペナルティの最大値スケール（デフォルト: 0.30）

    Returns
    -------
    float
        ペナルティ値（0〜penalty_scale）

    Examples
    --------
    >>> compute_regime_imbalance_penalty(0.30, threshold=0.60, penalty_scale=0.30)
    0.15  # (0.60 - 0.30) / 0.60 * 0.30 = 0.15
    """
    if normalized_entropy >= threshold:
        return 0.0
    if threshold < 1e-15:
        return 0.0
    ratio = (threshold - normalized_entropy) / threshold
    return min(penalty_scale, ratio * penalty_scale)


# ---------------------------------------------------------------------------
# 集約関数
# ---------------------------------------------------------------------------

def build_regime_entropy_result(
    regime_sharpes: Dict[str, float],
    threshold: Optional[float] = None,
    bonus_scale: float = 0.20,
    penalty_scale: float = 0.30,
) -> RegimeEntropyResult:
    """
    レジームシャープ比から RegimeEntropyResult を構築する。

    Parameters
    ----------
    regime_sharpes : dict[str, float]
        レジーム名 → Sharpe 比
    threshold : float, optional
        ボーナス/ペナルティ閾値。None の場合は環境変数 FROST_REGIME_ENTROPY_MIN を使用。
    bonus_scale : float
        ボーナス最大スケール
    penalty_scale : float
        ペナルティ最大スケール

    Returns
    -------
    RegimeEntropyResult
    """
    if threshold is None:
        threshold = _REGIME_ENTROPY_MIN

    # 有効（正 Sharpe）レジームを抽出
    positive = {k: v for k, v in regime_sharpes.items() if v > 0.0}
    n = len(positive)

    # 重みの正規化（確率分布）
    total = sum(positive.values()) if positive else 0.0
    if total > 0.0:
        regime_weights: Dict[str, float] = {k: v / total for k, v in positive.items()}
    else:
        regime_weights = {k: 0.0 for k in positive}

    # エントロピー計算
    raw_entropy = compute_regime_entropy(positive) if n >= _MIN_REGIME_COUNT else 0.0
    max_entropy = math.log(n) if n >= _MIN_REGIME_COUNT else 0.0
    normalized_entropy = compute_normalized_regime_entropy(positive) if n >= _MIN_REGIME_COUNT else 0.0
    balance_score = compute_regime_balance_score(positive) if n >= _MIN_REGIME_COUNT else 0.0

    # ボーナス / ペナルティ
    entropy_bonus = compute_regime_entropy_bonus(
        normalized_entropy, threshold=threshold, bonus_scale=bonus_scale
    )
    imbalance_penalty = compute_regime_imbalance_penalty(
        normalized_entropy, threshold=threshold, penalty_scale=penalty_scale
    )

    # 支配レジーム（最大重み）
    if regime_weights:
        dominant_regime = max(regime_weights, key=lambda k: regime_weights[k])
        dominant_weight = regime_weights[dominant_regime]
    else:
        dominant_regime = None
        dominant_weight = 0.0

    is_balanced = normalized_entropy >= threshold

    return RegimeEntropyResult(
        regime_sharpes=dict(regime_sharpes),
        regime_weights=regime_weights,
        raw_entropy=raw_entropy,
        normalized_entropy=normalized_entropy,
        balance_score=balance_score,
        entropy_bonus=entropy_bonus,
        imbalance_penalty=imbalance_penalty,
        n_regimes=n,
        max_entropy=max_entropy,
        threshold_used=threshold,
        dominant_regime=dominant_regime,
        dominant_weight=dominant_weight,
        is_balanced=is_balanced,
    )


def extract_regime_sharpes_from_features(
    features: Dict[str, object],
) -> Dict[str, float]:
    """
    frost_features.py の extract_all_features() 出力から
    regime_sharpes 辞書を抽出するヘルパー。

    期待するキー（FROST 既存 feature キー）:
      - crisis_period_sharpe
      - low_liquidity_sharpe
      - high_vol_sharpe
      - event_window_only_sharpe

    Parameters
    ----------
    features : dict
        extract_all_features() の戻り値

    Returns
    -------
    dict[str, float]
        レジーム名 → Sharpe 比
    """
    regime_key_map = {
        "crisis_period_sharpe": "crisis",
        "low_liquidity_sharpe": "low_liquidity",
        "high_vol_sharpe": "high_vol",
        "event_window_only_sharpe": "event_window",
        # 拡張: 将来的に追加されるレジームもここにマッピング
        "bull_sharpe": "bull",
        "bear_sharpe": "bear",
        "sideways_sharpe": "sideways",
        "rate_hike_sharpe": "rate_hike",
        "rate_cut_sharpe": "rate_cut",
        "risk_on_sharpe": "risk_on",
        "risk_off_sharpe": "risk_off",
    }

    result: Dict[str, float] = {}
    for feat_key, regime_name in regime_key_map.items():
        v = features.get(feat_key)
        if v is not None:
            try:
                fv = float(v)
                if not (math.isnan(fv) or math.isinf(fv)):
                    result[regime_name] = fv
            except (TypeError, ValueError):
                pass

    return result


def compute_regime_entropy_from_features(
    features: Dict[str, object],
    threshold: Optional[float] = None,
) -> RegimeEntropyResult:
    """
    FROST features dict から直接 RegimeEntropyResult を計算する便利関数。

    Parameters
    ----------
    features : dict
        frost_features.extract_all_features() の戻り値
    threshold : float, optional
        ボーナス/ペナルティ閾値

    Returns
    -------
    RegimeEntropyResult
    """
    regime_sharpes = extract_regime_sharpes_from_features(features)
    return build_regime_entropy_result(regime_sharpes, threshold=threshold)


# ---------------------------------------------------------------------------
# FROST v2 スコア統合用ヘルパー
# ---------------------------------------------------------------------------

def regime_entropy_to_score_components(
    result: RegimeEntropyResult,
) -> Dict[str, float]:
    """
    RegimeEntropyResult を FROST v2 スコア計算用コンポーネントに変換する。

    Returns
    -------
    dict with keys:
      - regime_entropy_score  : float (0〜1)  正スコア軸として使用
      - regime_entropy_bonus  : float (0〜0.2)
      - regime_imbalance_penalty : float (0〜0.3)
      - normalized_entropy    : float
      - is_balanced           : bool
    """
    return {
        "regime_entropy_score": result.balance_score,
        "regime_entropy_bonus": result.entropy_bonus,
        "regime_imbalance_penalty": result.imbalance_penalty,
        "normalized_entropy": result.normalized_entropy,
        "is_balanced": result.is_balanced,
    }


def regime_entropy_hard_gate_pass(
    result: RegimeEntropyResult,
    min_entropy: Optional[float] = None,
) -> Tuple[bool, str]:
    """
    レジームエントロピーの hard gate 判定。

    環境変数 FROST_REGIME_ENTROPY_ENABLED=0 の場合は常に pass。

    Parameters
    ----------
    result : RegimeEntropyResult
    min_entropy : float, optional
        最低エントロピー閾値。None の場合は _REGIME_ENTROPY_MIN を使用。

    Returns
    -------
    (pass_flag: bool, reason: str)
    """
    if not _REGIME_ENTROPY_ENABLED:
        return True, "REGIME_ENTROPY_ENABLED=0: スキップ"

    if min_entropy is None:
        min_entropy = _REGIME_ENTROPY_MIN

    if result.n_regimes < _MIN_REGIME_COUNT:
        # レジームデータ不足は PASS（データがないだけでペナルティにしない）
        return True, f"有効レジーム数={result.n_regimes} < {_MIN_REGIME_COUNT}: エントロピー評価スキップ"

    if result.normalized_entropy < min_entropy:
        return (
            False,
            f"normalized_regime_entropy={result.normalized_entropy:.4f} < min={min_entropy:.4f} "
            f"(dominant={result.dominant_regime}, weight={result.dominant_weight:.3f})",
        )

    return True, f"regime_entropy OK: {result.normalized_entropy:.4f} >= {min_entropy:.4f}"


# ---------------------------------------------------------------------------
# バッチ集計
# ---------------------------------------------------------------------------

def summarize_regime_entropy_batch(
    results: List[RegimeEntropyResult],
) -> Dict[str, object]:
    """
    複数候補の RegimeEntropyResult をバッチ集計する。

    Parameters
    ----------
    results : list[RegimeEntropyResult]

    Returns
    -------
    dict
        mean_normalized_entropy, balanced_count, unbalanced_count, etc.
    """
    if not results:
        return {
            "count": 0,
            "mean_normalized_entropy": 0.0,
            "median_normalized_entropy": 0.0,
            "balanced_count": 0,
            "unbalanced_count": 0,
            "balanced_ratio": 0.0,
            "mean_balance_score": 0.0,
        }

    entropies = [r.normalized_entropy for r in results]
    balance_scores = [r.balance_score for r in results]
    balanced = [r for r in results if r.is_balanced]

    n = len(results)
    sorted_e = sorted(entropies)
    mid = n // 2
    median_e = sorted_e[mid] if n % 2 == 1 else (sorted_e[mid - 1] + sorted_e[mid]) / 2

    return {
        "count": n,
        "mean_normalized_entropy": sum(entropies) / n,
        "median_normalized_entropy": median_e,
        "balanced_count": len(balanced),
        "unbalanced_count": n - len(balanced),
        "balanced_ratio": len(balanced) / n,
        "mean_balance_score": sum(balance_scores) / n,
    }
