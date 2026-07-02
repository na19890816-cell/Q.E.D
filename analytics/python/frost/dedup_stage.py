"""
dedup_stage.py
--------------
DedupStage — FROST Near-Duplicate 排除の一元化エンジン (D4 負債解消)。

Phase 4 で新設。以前は以下の 2 箇所に散在していた排除ロジックを本モジュールに集約する:
  - frost_signal_dedup.py : OOS シグナル相関ベース排除  (|ρ| > threshold)
  - frost_ranker.py       : 式構造ベース排除 (hash 類似度 + Jaccard on tokens)

設計原則
--------
- 副作用なし (pure function / dataclass メソッド)
- pure Python 制約 (numpy 不使用)
- 後方互換: frost_signal_dedup / frost_ranker の公開 API シグネチャを変えない
- NaN / Inf セーフ
- FROST_SIGNAL_DEDUP_ENABLED=0 でシグナル dedup をスキップ
- FrostConfig.near_duplicate_threshold を統一閾値として参照

公開 API
--------
DedupStage
  .detect_structural(candidates, threshold) -> StructuralDedupResult
      候補の hash / formula 類似度による near-duplicate 検出
      (frost_ranker.detect_near_duplicates の移管先)

  .apply_signal(evaluations, signal_matrix, threshold) -> (evaluations, SignalDedupResult)
      OOS 相関ベース near-duplicate 排除
      (frost_signal_dedup.apply_signal_dedup の移管先)

  .run(candidates, evaluations, signal_matrix) -> DedupRunResult
      structural + signal 両軸を統合実行する統一エントリポイント

DedupRunResult
  structural_suppressed: {candidate_id: dominant_id}
  signal_result        : SignalDedupResult
  all_suppressed_ids   : Set[str]  (両軸の union)
"""
from __future__ import annotations

import math
import os
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

# --------------------------------------------------------------------------- #
# 環境変数
# --------------------------------------------------------------------------- #

_SIGNAL_DEDUP_ENABLED: bool = os.environ.get("FROST_SIGNAL_DEDUP_ENABLED", "1") == "1"
_CORR_MAX: float = float(os.environ.get("FROST_SIGNAL_CORR_MAX", "0.90"))


# --------------------------------------------------------------------------- #
# 安全演算ヘルパー
# --------------------------------------------------------------------------- #

def _safe(v: Any, default: float = 0.0) -> float:
    """NaN / Inf / 非数値を default に変換する。"""
    try:
        f = float(v)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# 相関演算ヘルパー (pure Python)
# --------------------------------------------------------------------------- #

def _pearson(xs: List[float], ys: List[float]) -> float:
    """ピアソン相関係数。計算不能なら 0.0 を返す。"""
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
# 構造類似度ヘルパー
# --------------------------------------------------------------------------- #

def _hash_similarity(hash1: str, hash2: str) -> float:
    """
    2 つの candidate_hash の類似度を返す (0〜1)。
    同一 hash → 1.0。異なる hash → 共通プレフィックス長に基づく簡易類似度。
    """
    if hash1 == hash2:
        return 1.0
    if not hash1 or not hash2:
        return 0.0
    max_len = max(len(hash1), len(hash2))
    common = 0
    for c1, c2 in zip(hash1, hash2):
        if c1 == c2:
            common += 1
        else:
            break
    return common / max_len


def _formula_similarity(formula1: Optional[str], formula2: Optional[str]) -> float:
    """2 つの formula_text の Jaccard 類似度 (トークン集合)。"""
    if not formula1 or not formula2:
        return 0.0
    if formula1 == formula2:
        return 1.0
    tokens1 = set(formula1.replace("(", " ").replace(")", " ").split())
    tokens2 = set(formula2.replace("(", " ").replace(")", " ").split())
    union = len(tokens1 | tokens2)
    if union == 0:
        return 0.0
    return len(tokens1 & tokens2) / union


# --------------------------------------------------------------------------- #
# 結果 Dataclasses
# --------------------------------------------------------------------------- #

@dataclass
class SignalDedupResult:
    """
    OOS 相関ベース dedup の結果。
    frost_signal_dedup.SignalDedupResult と完全互換。
    """
    candidate_ids: List[str]
    suppressed: Dict[str, bool]            # candidate_id → True = 重複で除外
    suppressed_by: Dict[str, str]          # suppressed な候補が誰に支配されたか
    corr_pairs: List[Tuple[str, str, float]]  # (id_a, id_b, corr)
    threshold_used: float
    dedup_count: int


@dataclass
class StructuralDedupResult:
    """
    式構造ベース (hash + Jaccard) dedup の結果。
    """
    candidate_ids: List[str]
    suppressed: Dict[str, Optional[str]]   # suppressed_id → dominant_id
    threshold_used: float
    dedup_count: int


@dataclass
class DedupRunResult:
    """
    DedupStage.run() の統合結果。
    structural + signal 両軸の dedup 情報を保持する。
    """
    structural: StructuralDedupResult
    signal: SignalDedupResult
    all_suppressed_ids: Set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        structural_ids = set(self.structural.suppressed.keys())
        signal_ids = {cid for cid, flag in self.signal.suppressed.items() if flag}
        self.all_suppressed_ids = structural_ids | signal_ids

    @property
    def total_suppressed(self) -> int:
        return len(self.all_suppressed_ids)

    def is_suppressed(self, candidate_id: str) -> bool:
        """どちらかの軸で抑制されているか。"""
        return candidate_id in self.all_suppressed_ids


# --------------------------------------------------------------------------- #
# _EvalProxy (内部用 ダックタイプ)
# --------------------------------------------------------------------------- #

@dataclass
class _EvalProxy:
    """
    パイプライン外からテストする際に使う最小ダックタイプ。
    FrostEvaluation の部分集合。
    """
    candidate_id: str
    frost_score: float = 0.0
    complexity_penalty: float = 0.0
    crowding_penalty: float = 0.0
    regime_entropy_score: float = 0.0


# --------------------------------------------------------------------------- #
# DedupStage
# --------------------------------------------------------------------------- #

@dataclass
class DedupStage:
    """
    FROST near-duplicate 排除の統一エンジン。

    Attributes
    ----------
    structural_threshold : float
        hash / Jaccard 類似度の排除閾値 (default: 0.95)
    signal_threshold : float
        OOS 相関の排除閾値 (default: FROST_SIGNAL_CORR_MAX 環境変数)
    signal_dedup_enabled : bool
        シグナル dedup の有効/無効 (default: FROST_SIGNAL_DEDUP_ENABLED 環境変数)
    """
    structural_threshold: float = 0.95
    signal_threshold: float = field(default_factory=lambda: _CORR_MAX)
    signal_dedup_enabled: bool = field(default_factory=lambda: _SIGNAL_DEDUP_ENABLED)

    # ------------------------------------------------------------------ #
    # 構造ベース dedup
    # ------------------------------------------------------------------ #

    def detect_structural(
        self,
        candidates: List[Any],
        threshold: Optional[float] = None,
    ) -> StructuralDedupResult:
        """
        候補の hash / formula 類似度による near-duplicate 検出。

        frost_ranker.detect_near_duplicates() の移管先。

        Parameters
        ----------
        candidates : List[FrostCandidate]
            candidate_id, candidate_hash, formula_text 属性を持つオブジェクト。
        threshold : float | None
            None の場合は self.structural_threshold を使用。

        Returns
        -------
        StructuralDedupResult
            suppressed: {suppressed_candidate_id: dominant_candidate_id}
        """
        thr = threshold if threshold is not None else self.structural_threshold
        n = len(candidates)
        suppressed: Dict[str, Optional[str]] = {}

        for i in range(n):
            if candidates[i].candidate_id in suppressed:
                continue
            for j in range(i + 1, n):
                if candidates[j].candidate_id in suppressed:
                    continue

                c1, c2 = candidates[i], candidates[j]
                hash_sim = _hash_similarity(
                    getattr(c1, "candidate_hash", "") or "",
                    getattr(c2, "candidate_hash", "") or "",
                )

                if hash_sim < thr:
                    formula_sim = _formula_similarity(
                        getattr(c1, "formula_text", None),
                        getattr(c2, "formula_text", None),
                    )
                    similarity = max(hash_sim, formula_sim)
                else:
                    similarity = hash_sim

                if similarity >= thr:
                    suppressed[c2.candidate_id] = c1.candidate_id

        all_ids = [c.candidate_id for c in candidates]
        return StructuralDedupResult(
            candidate_ids=all_ids,
            suppressed=suppressed,
            threshold_used=thr,
            dedup_count=len(suppressed),
        )

    # ------------------------------------------------------------------ #
    # 相関行列ユーティリティ
    # ------------------------------------------------------------------ #

    @staticmethod
    def compute_correlation_matrix(
        signal_matrix: Dict[str, List[float]],
    ) -> Dict[Tuple[str, str], float]:
        """
        候補ごとの OOS シグナル列から全ペアのピアソン相関を算出する。

        frost_signal_dedup.compute_signal_correlation_matrix() の移管先。

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

    @staticmethod
    def find_corr_duplicates(
        corr_matrix: Dict[Tuple[str, str], float],
        threshold: float,
    ) -> List[Tuple[str, str, float]]:
        """
        |ρ| > threshold なペアを near-duplicate として返す。

        frost_signal_dedup.find_oos_duplicates() の移管先。

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

    # ------------------------------------------------------------------ #
    # 優先ルール (シグナル dedup 用)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _preference_score(
        candidate_id: str,
        eval_map: Dict[str, _EvalProxy],
    ) -> Tuple[float, float, float]:
        """
        優先ルール: complexity 低 → crowding 低 → regime_entropy 高。
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

    def _select_preferred(
        self,
        dup_pairs: List[Tuple[str, str, float]],
        eval_map: Dict[str, _EvalProxy],
        threshold: float,
    ) -> SignalDedupResult:
        """
        重複ペアから「残す候補」を決定し、SignalDedupResult を返す。

        frost_signal_dedup.select_preferred_from_duplicates() の移管先。

        アルゴリズム
        ------------
        1. ρ の高いペアから順に処理
        2. ペア (a, b) で preference_score が低い方を「残す」、高い方を suppress
        3. 既に suppressed な候補は以降のペアでスキップ
        """
        all_ids: Set[str] = set()
        for a, b, _ in dup_pairs:
            all_ids.add(a)
            all_ids.add(b)

        suppressed: Dict[str, bool] = {i: False for i in all_ids}
        suppressed_by: Dict[str, str] = {}

        for a, b, _corr in dup_pairs:
            if suppressed[a] or suppressed[b]:
                continue
            score_a = self._preference_score(a, eval_map)
            score_b = self._preference_score(b, eval_map)
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
            threshold_used=threshold,
            dedup_count=sum(1 for v in suppressed.values() if v),
        )

    # ------------------------------------------------------------------ #
    # シグナル dedup
    # ------------------------------------------------------------------ #

    def apply_signal(
        self,
        evaluations: List[Any],
        signal_matrix: Dict[str, List[float]],
        threshold: Optional[float] = None,
    ) -> Tuple[List[Any], SignalDedupResult]:
        """
        OOS 相関ベース near-duplicate 排除。

        frost_signal_dedup.apply_signal_dedup() の移管先。

        Parameters
        ----------
        evaluations : List[FrostEvaluation]
            candidate_id / complexity_penalty / crowding_penalty /
            regime_entropy_score 属性を参照する。
        signal_matrix : {candidate_id: [oos_signal_t0, ...]}
            シグナルが存在しない候補はデデュープ対象外。
        threshold : float | None
            None の場合は self.signal_threshold を使用。

        Returns
        -------
        (evaluations_updated, SignalDedupResult)
        """
        thr = threshold if threshold is not None else self.signal_threshold

        _empty = SignalDedupResult(
            candidate_ids=[],
            suppressed={},
            suppressed_by={},
            corr_pairs=[],
            threshold_used=thr,
            dedup_count=0,
        )

        if not self.signal_dedup_enabled:
            return evaluations, _empty

        active_signals = {cid: sigs for cid, sigs in signal_matrix.items() if sigs}
        if len(active_signals) < 2:
            _empty.candidate_ids = list(active_signals.keys())
            return evaluations, _empty

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

        corr_matrix = self.compute_correlation_matrix(active_signals)
        dup_pairs = self.find_corr_duplicates(corr_matrix, thr)
        result = self._select_preferred(dup_pairs, eval_map, thr)

        # evaluations に suppressed フラグを付与
        updated = []
        for ev in evaluations:
            cid = ev.candidate_id
            if cid in result.suppressed and result.suppressed[cid]:
                try:
                    ev.suppressed_by_signal_dedup = result.suppressed_by.get(cid)
                except AttributeError:
                    object.__setattr__(
                        ev, "suppressed_by_signal_dedup", result.suppressed_by.get(cid)
                    )
            updated.append(ev)

        return updated, result

    # ------------------------------------------------------------------ #
    # 統合実行 API
    # ------------------------------------------------------------------ #

    def run(
        self,
        candidates: List[Any],
        evaluations: List[Any],
        signal_matrix: Optional[Dict[str, List[float]]] = None,
    ) -> DedupRunResult:
        """
        structural + signal 両軸を統合実行する統一エントリポイント。

        Parameters
        ----------
        candidates : List[FrostCandidate]
        evaluations : List[FrostEvaluation]
        signal_matrix : {candidate_id: [signal_values]} | None
            None の場合はシグナル dedup をスキップ。

        Returns
        -------
        DedupRunResult
        """
        # 1. 構造ベース dedup
        structural = self.detect_structural(candidates)

        # 2. シグナルベース dedup
        if signal_matrix is not None:
            _, signal = self.apply_signal(evaluations, signal_matrix)
        else:
            signal = SignalDedupResult(
                candidate_ids=[],
                suppressed={},
                suppressed_by={},
                corr_pairs=[],
                threshold_used=self.signal_threshold,
                dedup_count=0,
            )

        return DedupRunResult(structural=structural, signal=signal)

    # ------------------------------------------------------------------ #
    # ファクトリ
    # ------------------------------------------------------------------ #

    @classmethod
    def from_config(cls, config: Any) -> "DedupStage":
        """
        FrostConfig / dict から DedupStage を生成する。

        config は near_duplicate_threshold 属性 (または同名キー) を参照する。
        signal_threshold は FROST_SIGNAL_CORR_MAX 環境変数から取得する
        (config に signal_corr_max があればそちらを優先)。
        """
        if isinstance(config, dict):
            structural_thr = float(config.get("near_duplicate_threshold", 0.95))
            signal_thr = float(config.get("signal_corr_max", _CORR_MAX))
            enabled = bool(config.get("signal_dedup_enabled", _SIGNAL_DEDUP_ENABLED))
        else:
            structural_thr = float(getattr(config, "near_duplicate_threshold", 0.95))
            signal_thr = float(getattr(config, "signal_corr_max", _CORR_MAX))
            enabled = bool(getattr(config, "signal_dedup_enabled", _SIGNAL_DEDUP_ENABLED))

        return cls(
            structural_threshold=structural_thr,
            signal_threshold=signal_thr,
            signal_dedup_enabled=enabled,
        )
