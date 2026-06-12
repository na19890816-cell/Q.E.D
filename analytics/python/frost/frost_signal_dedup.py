"""
frost_signal_dedup.py
----------------------
OOS Signal Correlation Dedup — Jaccard 類似度だけでは検出できない
「式は違うが OOS シグナルがほぼ同一」な候補を相関ベースで排除する。

主要関数
--------
compute_signal_correlation_matrix(signal_matrix)
    候補ごとの OOS シグナル列 (T × N) からピアソン相関行列 (N × N) を算出。

find_oos_duplicates(corr_matrix, candidate_ids, threshold)
    ρ > threshold な候補ペアを near-duplicate として検出。
    デフォルト閾値: FROST_SIGNAL_CORR_MAX (default=0.90)

select_preferred_from_duplicates(dup_pairs, evaluations, config)
    重複ペアから「残す候補」を選択するロジック。
    優先ルール: complexity 低 → crowding 低 → regime_entropy 高

apply_signal_dedup(evaluations, signal_matrix, config)
    FROST パイプラインへの統合エントリポイント。
    suppressed_by_signal_dedup フラグを evaluation に付与して返す。

設計原則
--------
- numpy 不使用（pure Python + statistics モジュール）
- NaN/Inf セーフ
- dry_run 対応（flag をセットするだけで DB に書かない）
- FROST_SIGNAL_DEDUP_ENABLED=0 でスキップ
- trace_id 維持
"""
from __future__ import annotations

import math
import os
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

# --------------------------------------------------------------------------- #
# 設定
# --------------------------------------------------------------------------- #

_ENABLED: bool = os.environ.get("FROST_SIGNAL_DEDUP_ENABLED", "1") == "1"
_CORR_MAX: float = float(os.environ.get("FROST_SIGNAL_CORR_MAX", "0.90"))


# --------------------------------------------------------------------------- #
# 安全演算ヘルパー
# --------------------------------------------------------------------------- #

def _safe(v: float) -> float:
    if math.isnan(v) or math.isinf(v):
        return 0.0
    return v


def _pearson(xs: List[float], ys: List[float]) -> float:
    """ピアソン相関係数 (pure Python)。計算不能なら 0.0 を返す。"""
    n = len(xs)
    if n < 3:
        return 0.0
    xs = [_safe(v) for v in xs]
    ys = [_safe(v) for v in ys]
    try:
        mx = statistics.mean(xs)
        my = statistics.mean(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        dy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if dx < 1e-10 or dy < 1e-10:
            return 0.0
        return max(-1.0, min(1.0, num / (dx * dy)))
    except (ZeroDivisionError, ValueError, statistics.StatisticsError):
        return 0.0


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #

@dataclass
class SignalDedupResult:
    """signal_dedup の結果。candidate_id ごとの重複フラグを保持。"""
    candidate_ids: List[str]
    suppressed: Dict[str, bool]          # candidate_id → True = 重複で除外
    suppressed_by: Dict[str, str]        # suppressed な候補が誰に支配されたか
    corr_pairs: List[Tuple[str, str, float]]  # (id_a, id_b, corr) で ρ > threshold なペア
    threshold_used: float
    dedup_count: int


@dataclass
class _EvalProxy:
    """
    パイプライン外からテストする際に使う最小ダックタイプ。
    FrostEvaluation の部分集合。
    """
    candidate_id: str
    frost_score: float = 0.0
    complexity_penalty: float = 0.0
    # crowding / regime_entropy は v2 で追加されるが、ここではオプション
    crowding_penalty: float = 0.0
    regime_entropy_score: float = 0.0


# --------------------------------------------------------------------------- #
# コア関数
# --------------------------------------------------------------------------- #

def compute_signal_correlation_matrix(
    signal_matrix: Dict[str, List[float]],
) -> Dict[Tuple[str, str], float]:
    """
    候補ごとの OOS シグナル列から全ペアのピアソン相関を算出する。

    Parameters
    ----------
    signal_matrix : {candidate_id: [signal_t0, signal_t1, ...]}
        各リストの長さは揃っている必要がある。

    Returns
    -------
    {(id_a, id_b): correlation}  (id_a < id_b の上三角のみ)
    """
    ids = sorted(signal_matrix.keys())
    result: Dict[Tuple[str, str], float] = {}
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            corr = _pearson(signal_matrix[a], signal_matrix[b])
            result[(a, b)] = corr
    return result


def find_oos_duplicates(
    corr_matrix: Dict[Tuple[str, str], float],
    threshold: float = _CORR_MAX,
) -> List[Tuple[str, str, float]]:
    """
    |ρ| > threshold なペアを near-duplicate として返す。

    Returns
    -------
    [(id_a, id_b, corr), ...] — ρ の絶対値が大きい順
    """
    pairs = [
        (a, b, corr)
        for (a, b), corr in corr_matrix.items()
        if abs(corr) > threshold
    ]
    pairs.sort(key=lambda x: abs(x[2]), reverse=True)
    return pairs


def _preference_score(
    candidate_id: str,
    eval_map: Dict[str, _EvalProxy],
) -> Tuple[float, float, float]:
    """
    優先ルール: complexity 低 → crowding 低 → regime_entropy 高
    タプルとして返し、min() で「より好ましい候補」を選ぶ。
    (complexity_penalty, crowding_penalty, -regime_entropy_score)
    """
    ev = eval_map.get(candidate_id)
    if ev is None:
        return (1.0, 1.0, 0.0)
    return (
        _safe(ev.complexity_penalty),
        _safe(ev.crowding_penalty),
        -_safe(ev.regime_entropy_score),
    )


def select_preferred_from_duplicates(
    dup_pairs: List[Tuple[str, str, float]],
    eval_map: Dict[str, _EvalProxy],
) -> SignalDedupResult:
    """
    重複ペアから「残す候補」を決定し、SignalDedupResult を返す。

    アルゴリズム
    ------------
    1. ρ の高いペアから順に処理
    2. ペア (a, b) で、preference_score が低い方を「残す」、高い方を suppress
    3. 既に suppressed な候補は以降のペアでスキップ
    """
    all_ids: Set[str] = set()
    for a, b, _ in dup_pairs:
        all_ids.add(a)
        all_ids.add(b)

    suppressed: Dict[str, bool] = {i: False for i in all_ids}
    suppressed_by: Dict[str, str] = {}

    for a, b, corr in dup_pairs:
        if suppressed[a] or suppressed[b]:
            continue
        score_a = _preference_score(a, eval_map)
        score_b = _preference_score(b, eval_map)
        # score が小さい方を「残す」
        if score_a <= score_b:
            suppressed[b] = True
            suppressed_by[b] = a
        else:
            suppressed[a] = True
            suppressed_by[a] = b

    return SignalDedupResult(
        candidate_ids=sorted(all_ids),
        suppressed=suppressed,
        suppressed_by=suppressed_by,
        corr_pairs=dup_pairs,
        threshold_used=_CORR_MAX,
        dedup_count=sum(1 for v in suppressed.values() if v),
    )


# --------------------------------------------------------------------------- #
# パイプライン統合エントリポイント
# --------------------------------------------------------------------------- #

def apply_signal_dedup(
    evaluations: List,
    signal_matrix: Dict[str, List[float]],
    threshold: Optional[float] = None,
) -> Tuple[List, SignalDedupResult]:
    """
    FROST パイプラインへの統合エントリポイント。

    Parameters
    ----------
    evaluations : List[FrostEvaluation]
        FrostEvaluation dataclass のリスト。
        candidate_id, complexity_penalty, crowding_penalty, regime_entropy_score 属性を参照する。
    signal_matrix : {candidate_id: [oos_signal_t0, oos_signal_t1, ...]}
        OOS 期間のシグナル列。signal が存在しない候補はデデュープ対象外。
    threshold : float | None
        None の場合は FROST_SIGNAL_CORR_MAX 環境変数を使用。

    Returns
    -------
    (evaluations_updated, SignalDedupResult)
        evaluations_updated: suppressed_by_signal_dedup 属性が追加/更新された評価リスト
        (属性がない場合は無視して元リストをそのまま返す)
    """
    if not _ENABLED:
        empty = SignalDedupResult(
            candidate_ids=[],
            suppressed={},
            suppressed_by={},
            corr_pairs=[],
            threshold_used=threshold or _CORR_MAX,
            dedup_count=0,
        )
        return evaluations, empty

    thr = threshold if threshold is not None else _CORR_MAX

    # signal が存在する候補のみ対象
    active_signals = {
        cid: sigs for cid, sigs in signal_matrix.items() if sigs
    }
    if len(active_signals) < 2:
        empty = SignalDedupResult(
            candidate_ids=list(active_signals.keys()),
            suppressed={},
            suppressed_by={},
            corr_pairs=[],
            threshold_used=thr,
            dedup_count=0,
        )
        return evaluations, empty

    # eval_map 構築
    eval_map: Dict[str, _EvalProxy] = {}
    for ev in evaluations:
        cid = ev.candidate_id
        eval_map[cid] = _EvalProxy(
            candidate_id=cid,
            frost_score=float(getattr(ev, "frost_score", 0.0) or 0.0),
            complexity_penalty=float(getattr(ev, "complexity_penalty", 0.0) or 0.0),
            crowding_penalty=float(getattr(ev, "crowding_penalty", 0.0) or 0.0),
            regime_entropy_score=float(getattr(ev, "regime_entropy_score", 0.0) or 0.0),
        )

    # 相関行列 → 重複ペア → 除外候補決定
    corr_matrix = compute_signal_correlation_matrix(active_signals)
    dup_pairs = find_oos_duplicates(corr_matrix, thr)
    result = select_preferred_from_duplicates(dup_pairs, eval_map)

    # evaluations に suppressed フラグを付与
    updated = []
    for ev in evaluations:
        cid = ev.candidate_id
        if cid in result.suppressed and result.suppressed[cid]:
            # 属性があれば上書き、なければ setattr で付与
            try:
                ev.suppressed_by_signal_dedup = result.suppressed_by.get(cid)
            except AttributeError:
                object.__setattr__(ev, "suppressed_by_signal_dedup",
                                   result.suppressed_by.get(cid))
        updated.append(ev)

    return updated, result
