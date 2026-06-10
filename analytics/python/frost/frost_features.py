"""
frost_features.py
-----------------
FROST 評価用の特徴量抽出・正規化モジュール。

FrostCandidate (上流から受け取る生データ) から、
frost_metrics.py / frost_stability.py / frost_pbo.py が使う
正規化前の raw feature dict を生成する。

設計原則:
  - 副作用なし: 計算のみ行い、DB/ファイルへの書き込みは行わない
  - NaN/Inf セーフ: _safe() を経由して返す
  - dict 形式で返す: FrostEvaluation への詰め込みは frost_metrics.py 側で行う
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from analytics.python.frost.frost_contracts import FrostCandidate


# ---------------------------------------------------------------------------
# 内部ユーティリティ
# ---------------------------------------------------------------------------

def _safe(v: Any, default: float = 0.0) -> float:
    """NaN / Inf を default に変換。"""
    try:
        f = float(v)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


def _safe_opt(v: Any) -> Optional[float]:
    """NaN / Inf を None に変換（None は None のまま）。"""
    if v is None:
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def _get(d: dict, *keys: str, default: Any = None) -> Any:
    """ネストした辞書からキーを辿って値を取得する。"""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
    return cur


# ---------------------------------------------------------------------------
# バックテスト要約から生スコアを抽出
# ---------------------------------------------------------------------------

def extract_backtest_features(backtest_summary: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """
    backtest_summary_json から raw feature を抽出する。

    Parameters
    ----------
    backtest_summary : dict
        EML / harness.py など上流が渡す backtest summary 辞書。

    Returns
    -------
    dict
        キー: oos_sharpe, oos_sortino, oos_calmar, oos_max_drawdown,
              turnover, avg_hold_days, var_5, cvar_5, downside_vol
    """
    bt = backtest_summary or {}
    return {
        "oos_sharpe":       _safe_opt(_get(bt, "oos_sharpe") or _get(bt, "sharpe")),
        "oos_sortino":      _safe_opt(_get(bt, "oos_sortino") or _get(bt, "sortino")),
        "oos_calmar":       _safe_opt(_get(bt, "oos_calmar") or _get(bt, "calmar")),
        "oos_max_drawdown": _safe_opt(_get(bt, "oos_max_drawdown") or _get(bt, "max_drawdown")),
        "turnover":         _safe_opt(_get(bt, "turnover") or _get(bt, "annual_turnover")),
        "avg_hold_days":    _safe_opt(_get(bt, "avg_hold_days") or _get(bt, "hold_period_days")),
        "var_5":            _safe_opt(_get(bt, "var_5") or _get(bt, "var_95")),
        "cvar_5":           _safe_opt(_get(bt, "cvar_5") or _get(bt, "cvar_95")),
        "downside_vol":     _safe_opt(_get(bt, "downside_vol") or _get(bt, "semi_deviation")),
    }


# ---------------------------------------------------------------------------
# metrics_json から IC / 予測力特徴量を抽出
# ---------------------------------------------------------------------------

def extract_metrics_features(metrics: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """
    metrics_json から rank_ic / ic 系の特徴量を抽出する。

    Parameters
    ----------
    metrics : dict
        EML evaluation が書き込んだ metrics 辞書。

    Returns
    -------
    dict
        キー: rank_ic, ic, ic_t_stat, hit_rate, predictive_score_raw
    """
    m = metrics or {}
    rank_ic = _safe_opt(_get(m, "rank_ic") or _get(m, "rank_information_coefficient"))
    ic      = _safe_opt(_get(m, "ic") or _get(m, "information_coefficient"))
    ic_t    = _safe_opt(_get(m, "ic_t_stat") or _get(m, "ic_t"))
    hit     = _safe_opt(_get(m, "hit_rate") or _get(m, "direction_accuracy"))

    # 単純な予測力スコア (rank_ic をメインに、ic で補完)
    if rank_ic is not None:
        pred_raw = abs(rank_ic)
    elif ic is not None:
        pred_raw = abs(ic)
    else:
        pred_raw = 0.0

    return {
        "rank_ic":              rank_ic,
        "ic":                   ic,
        "ic_t_stat":            ic_t,
        "hit_rate":             hit,
        "predictive_score_raw": pred_raw,
    }


# ---------------------------------------------------------------------------
# regime_breakdown_json からレジーム特徴量を抽出
# ---------------------------------------------------------------------------

def extract_regime_features(regime_breakdown: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """
    regime_breakdown_json からレジーム別パフォーマンス特徴量を抽出する。

    Parameters
    ----------
    regime_breakdown : dict
        レジーム別の sharpe / return 辞書。
        例: {"bull": {"sharpe": 1.2}, "bear": {"sharpe": 0.3}, "crisis": {"sharpe": -0.5}}

    Returns
    -------
    dict
        キー: crisis_sharpe, bull_sharpe, bear_sharpe,
              regime_sharpes (list), regime_pass_ratio_raw
    """
    rb = regime_breakdown or {}

    def _regime_sharpe(key: str) -> Optional[float]:
        sub = rb.get(key, {})
        if isinstance(sub, dict):
            return _safe_opt(sub.get("sharpe") or sub.get("oos_sharpe"))
        return _safe_opt(sub)

    crisis_sharpe = _regime_sharpe("crisis")
    bull_sharpe   = _regime_sharpe("bull")
    bear_sharpe   = _regime_sharpe("bear")

    # レジーム通過率 (sharpe > 0 のレジーム数 / 全レジーム数)
    all_sharpes = [s for s in [crisis_sharpe, bull_sharpe, bear_sharpe] if s is not None]
    if all_sharpes:
        pass_count = sum(1 for s in all_sharpes if s > 0.0)
        regime_pass_ratio = pass_count / len(all_sharpes)
    else:
        regime_pass_ratio = None

    return {
        "crisis_sharpe":        crisis_sharpe,
        "bull_sharpe":          bull_sharpe,
        "bear_sharpe":          bear_sharpe,
        "regime_sharpes":       all_sharpes,
        "regime_pass_ratio_raw": regime_pass_ratio,
    }


# ---------------------------------------------------------------------------
# fold 結果から安定性の原材料を抽出
# ---------------------------------------------------------------------------

def extract_fold_features(fold_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    fold_results から安定性計算に必要な特徴量を抽出する。

    Parameters
    ----------
    fold_results : list of dict
        各 fold の backtest 結果。
        {"sharpe": float, "ic": float, "rank_ic": float, ...} のリスト。

    Returns
    -------
    dict
        キー: fold_sharpes, fold_ics, fold_rank_ics, fold_returns,
              n_folds, fold_raw
    """
    folds = fold_results or []

    fold_sharpes  = [_safe(f.get("sharpe") or f.get("oos_sharpe", 0.0)) for f in folds]
    fold_ics      = [_safe(f.get("ic", 0.0)) for f in folds]
    fold_rank_ics = [_safe(f.get("rank_ic", 0.0)) for f in folds]
    fold_returns  = [_safe(f.get("total_return") or f.get("return", 0.0)) for f in folds]

    return {
        "fold_sharpes":  fold_sharpes,
        "fold_ics":      fold_ics,
        "fold_rank_ics": fold_rank_ics,
        "fold_returns":  fold_returns,
        "n_folds":       len(folds),
        "fold_raw":      folds,
    }


# ---------------------------------------------------------------------------
# キャパシティスコアを推定
# ---------------------------------------------------------------------------

def estimate_capacity_score(
    candidate: FrostCandidate,
    max_aum_estimate: float = 1e8,
) -> float:
    """
    候補のキャパシティスコア (0〜1) を推定する。

    turnover, complexity, horizon から簡易推定。
    将来は市場インパクトモデルで置換可能。

    Parameters
    ----------
    candidate : FrostCandidate
    max_aum_estimate : float
        基準運用資産額 (デフォルト 1 億円)。

    Returns
    -------
    float
        0〜1 のキャパシティスコア。1 = 制約なし。
    """
    bt = candidate.backtest_summary or {}
    turnover = _safe(bt.get("turnover") or bt.get("annual_turnover", 2.0))

    # turnover が高いほど capacity は下がる (指数的減衰)
    # turnover=1 → 1.0, turnover=4 → ~0.5, turnover=10 → ~0.2
    capacity = math.exp(-0.3 * max(0.0, turnover - 1.0))

    # 複雑度ペナルティ (0.6 を超えると capacity を下げる)
    if candidate.complexity_score > 0.5:
        capacity *= max(0.3, 1.0 - (candidate.complexity_score - 0.5))

    return max(0.0, min(1.0, capacity))


# ---------------------------------------------------------------------------
# 全特徴量を一括抽出
# ---------------------------------------------------------------------------

def extract_all_features(candidate: FrostCandidate) -> Dict[str, Any]:
    """
    FrostCandidate から全特徴量を抽出して辞書として返す。

    この辞書を frost_metrics.py のスコア計算関数へ渡す。

    Returns
    -------
    dict
        全特徴量を含む辞書:
        backtest_feat / metrics_feat / regime_feat / fold_feat / capacity_score
    """
    backtest_feat = extract_backtest_features(candidate.backtest_summary)
    metrics_feat  = extract_metrics_features(candidate.metrics)
    regime_feat   = extract_regime_features(candidate.regime_breakdown)
    fold_feat     = extract_fold_features(candidate.fold_results)
    capacity      = estimate_capacity_score(candidate)

    return {
        **backtest_feat,
        **metrics_feat,
        **regime_feat,
        **fold_feat,
        "capacity_score_raw": capacity,
        "complexity_score":   _safe(candidate.complexity_score),
        "horizon":            candidate.horizon,
        "source_type":        candidate.source_type,
    }
