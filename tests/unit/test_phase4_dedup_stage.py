"""
test_phase4_dedup_stage.py
--------------------------
Phase 4: DedupStage 統合テスト (D4 負債解消の検証)

テスト構成
----------
TestDedupStageInit           (5)  : コンストラクタ / from_config / 環境変数デフォルト
TestHashSimilarity           (6)  : _hash_similarity ヘルパー
TestFormulaSimilarity        (7)  : _formula_similarity Jaccard ヘルパー
TestPearsonCorrelation       (6)  : _pearson / _safe ヘルパー
TestComputeCorrelationMatrix (5)  : DedupStage.compute_correlation_matrix()
TestFindCorrDuplicates       (6)  : DedupStage.find_corr_duplicates()
TestDetectStructural         (9)  : DedupStage.detect_structural()
TestSelectPreferred          (7)  : DedupStage._select_preferred()
TestApplySignal              (8)  : DedupStage.apply_signal()
TestDedupRunResult           (5)  : DedupRunResult ユーティリティ
TestDedupStageRun            (6)  : DedupStage.run() 統合
TestBackwardCompatSignalDedup(8)  : frost_signal_dedup.py 後方互換 API
TestBackwardCompatRanker     (7)  : frost_ranker.detect_near_duplicates() 後方互換 API
"""
from __future__ import annotations

import os
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import pytest

from analytics.python.frost.dedup_stage import (
    DedupStage,
    DedupRunResult,
    SignalDedupResult,
    StructuralDedupResult,
    _hash_similarity,
    _formula_similarity,
    _pearson,
    _safe,
    _EvalProxy,
)


# ===========================================================================
# テスト用フィクスチャ
# ===========================================================================

@dataclass
class _MockCandidate:
    candidate_id: str
    candidate_hash: str = ""
    formula_text: Optional[str] = None
    source_candidate_id: Optional[str] = None
    run_id: str = "run-001"
    trace_id: str = "trace-001"


@dataclass
class _MockEvaluation:
    candidate_id: str
    frost_score: float = 0.0
    hard_gate_passed: bool = True
    complexity_penalty: float = 0.0
    crowding_penalty: float = 0.0
    regime_entropy_score: float = 0.0
    suppressed_by_signal_dedup: Optional[str] = None


def _make_candidates(*defs):
    """(id, hash, formula) タプルのリストから _MockCandidate を生成する。"""
    out = []
    for d in defs:
        cid = d[0]
        chash = d[1] if len(d) > 1 else ""
        formula = d[2] if len(d) > 2 else None
        out.append(_MockCandidate(candidate_id=cid, candidate_hash=chash, formula_text=formula))
    return out


def _make_signal_matrix(**kwargs) -> Dict[str, List[float]]:
    """キーワード引数でシグナル行列を生成する。"""
    return dict(kwargs)


# ===========================================================================
# 1. TestDedupStageInit
# ===========================================================================

class TestDedupStageInit:
    def test_default_thresholds(self):
        stage = DedupStage()
        assert stage.structural_threshold == 0.95
        assert stage.signal_threshold == float(os.environ.get("FROST_SIGNAL_CORR_MAX", "0.90"))

    def test_custom_thresholds(self):
        stage = DedupStage(structural_threshold=0.80, signal_threshold=0.85)
        assert stage.structural_threshold == 0.80
        assert stage.signal_threshold == 0.85

    def test_from_config_dataclass(self):
        @dataclass
        class Cfg:
            near_duplicate_threshold: float = 0.88
        stage = DedupStage.from_config(Cfg())
        assert stage.structural_threshold == 0.88

    def test_from_config_dict(self):
        stage = DedupStage.from_config({"near_duplicate_threshold": 0.77})
        assert stage.structural_threshold == 0.77

    def test_from_config_signal_threshold_dict(self):
        stage = DedupStage.from_config({"signal_corr_max": 0.88})
        assert stage.signal_threshold == 0.88


# ===========================================================================
# 2. TestHashSimilarity
# ===========================================================================

class TestHashSimilarity:
    def test_identical_hash(self):
        assert _hash_similarity("abcdef", "abcdef") == 1.0

    def test_empty_hash(self):
        assert _hash_similarity("", "abc") == 0.0
        assert _hash_similarity("abc", "") == 0.0

    def test_no_common_prefix(self):
        result = _hash_similarity("abcdef", "xyz123")
        assert result == 0.0

    def test_partial_common_prefix(self):
        result = _hash_similarity("abcdef", "abcxyz")
        # 共通プレフィックス 3 / max(6,6) = 0.5
        assert abs(result - 0.5) < 1e-9

    def test_different_length(self):
        result = _hash_similarity("abc", "abcdef")
        # 共通プレフィックス 3 / max(3,6) = 0.5
        assert abs(result - 0.5) < 1e-9

    def test_both_empty(self):
        # 両方空文字列は hash1 == hash2 → 1.0 を返す
        assert _hash_similarity("", "") == 1.0


# ===========================================================================
# 3. TestFormulaSimilarity
# ===========================================================================

class TestFormulaSimilarity:
    def test_identical_formula(self):
        assert _formula_similarity("a + b", "a + b") == 1.0

    def test_none_formula(self):
        assert _formula_similarity(None, "a + b") == 0.0
        assert _formula_similarity("a + b", None) == 0.0
        assert _formula_similarity(None, None) == 0.0

    def test_empty_formula(self):
        assert _formula_similarity("", "a + b") == 0.0
        assert _formula_similarity("a + b", "") == 0.0

    def test_no_overlap(self):
        result = _formula_similarity("a b c", "x y z")
        assert result == 0.0

    def test_partial_overlap(self):
        # tokens1 = {a, b, c}, tokens2 = {a, b, d}
        # intersection = {a, b}, union = {a, b, c, d}
        result = _formula_similarity("a b c", "a b d")
        assert abs(result - 2 / 4) < 1e-9

    def test_parentheses_stripped(self):
        # "(a + b)" と "(a + b + c)" のトークンは括弧を除いて比較
        result = _formula_similarity("(a + b)", "(a + b + c)")
        # tokens1 = {a, +, b}, tokens2 = {a, +, b, +, c}
        # intersection = {a, +, b}, union = {a, +, b, c}
        assert 0 < result < 1.0

    def test_full_overlap_shuffled_order(self):
        result = _formula_similarity("a b c", "c b a")
        assert result == 1.0


# ===========================================================================
# 4. TestPearsonCorrelation
# ===========================================================================

class TestPearsonCorrelation:
    def test_perfect_positive(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert abs(_pearson(xs, xs) - 1.0) < 1e-9

    def test_perfect_negative(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [-1.0, -2.0, -3.0, -4.0, -5.0]
        assert abs(_pearson(xs, ys) - (-1.0)) < 1e-9

    def test_uncorrelated(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [2.0, 2.0, 2.0, 2.0, 2.0]  # 定数列
        assert _pearson(xs, ys) == 0.0

    def test_too_short(self):
        assert _pearson([1.0, 2.0], [1.0, 2.0]) == 0.0

    def test_safe_handles_nan(self):
        xs = [1.0, float("nan"), 3.0, 4.0, 5.0]
        ys = [1.0, 2.0, 3.0, 4.0, 5.0]
        # NaN → 0.0 に変換されるので計算自体は成功する
        result = _pearson(xs, ys)
        assert -1.0 <= result <= 1.0

    def test_safe_converts_inf(self):
        assert _safe(float("inf")) == 0.0
        assert _safe(float("-inf")) == 0.0
        assert _safe(float("nan")) == 0.0
        assert _safe(1.5) == 1.5


# ===========================================================================
# 5. TestComputeCorrelationMatrix
# ===========================================================================

class TestComputeCorrelationMatrix:
    def test_two_candidates(self):
        sm = {"A": [1.0, 2.0, 3.0, 4.0, 5.0], "B": [1.0, 2.0, 3.0, 4.0, 5.0]}
        matrix = DedupStage.compute_correlation_matrix(sm)
        assert ("A", "B") in matrix
        assert abs(matrix[("A", "B")] - 1.0) < 1e-9

    def test_upper_triangle_only(self):
        sm = {"A": [1.0]*5, "B": [2.0]*5, "C": [3.0]*5}
        matrix = DedupStage.compute_correlation_matrix(sm)
        # 上三角のみ (id_a < id_b)
        assert ("A", "B") in matrix
        assert ("A", "C") in matrix
        assert ("B", "C") in matrix
        assert ("B", "A") not in matrix

    def test_sorted_key_order(self):
        sm = {"C": [1.0]*5, "A": [1.0]*5, "B": [1.0]*5}
        matrix = DedupStage.compute_correlation_matrix(sm)
        for (a, b) in matrix.keys():
            assert a < b, f"キー順序エラー: ({a}, {b})"

    def test_single_candidate_returns_empty(self):
        sm = {"A": [1.0, 2.0, 3.0]}
        matrix = DedupStage.compute_correlation_matrix(sm)
        assert len(matrix) == 0

    def test_three_candidates_count(self):
        sm = {
            "A": [1.0, 2.0, 3.0, 4.0, 5.0],
            "B": [5.0, 4.0, 3.0, 2.0, 1.0],
            "C": [1.0, 3.0, 5.0, 3.0, 1.0],
        }
        matrix = DedupStage.compute_correlation_matrix(sm)
        assert len(matrix) == 3  # C(3,2) = 3 ペア


# ===========================================================================
# 6. TestFindCorrDuplicates
# ===========================================================================

class TestFindCorrDuplicates:
    def _matrix(self):
        return {
            ("A", "B"): 0.95,  # threshold 超過
            ("A", "C"): 0.80,  # threshold 未満
            ("B", "C"): 0.92,  # threshold 超過
        }

    def test_finds_high_corr_pairs(self):
        pairs = DedupStage.find_corr_duplicates(self._matrix(), threshold=0.90)
        ids = {(a, b) for a, b, _ in pairs}
        assert ("A", "B") in ids
        assert ("B", "C") in ids

    def test_excludes_low_corr_pairs(self):
        pairs = DedupStage.find_corr_duplicates(self._matrix(), threshold=0.90)
        ids = {(a, b) for a, b, _ in pairs}
        assert ("A", "C") not in ids

    def test_sorted_by_abs_corr_desc(self):
        pairs = DedupStage.find_corr_duplicates(self._matrix(), threshold=0.90)
        corrs = [abs(c) for _, _, c in pairs]
        assert corrs == sorted(corrs, reverse=True)

    def test_empty_matrix(self):
        pairs = DedupStage.find_corr_duplicates({}, threshold=0.90)
        assert pairs == []

    def test_negative_corr_detected(self):
        matrix = {("A", "B"): -0.95}
        pairs = DedupStage.find_corr_duplicates(matrix, threshold=0.90)
        assert len(pairs) == 1
        assert pairs[0][2] == -0.95

    def test_exact_threshold_excluded(self):
        # |ρ| = threshold は除外 (> threshold のみ)
        matrix = {("A", "B"): 0.90}
        pairs = DedupStage.find_corr_duplicates(matrix, threshold=0.90)
        assert len(pairs) == 0


# ===========================================================================
# 7. TestDetectStructural
# ===========================================================================

class TestDetectStructural:
    def test_identical_hash_suppressed(self):
        candidates = _make_candidates(("A", "aabbcc"), ("B", "aabbcc"))
        result = DedupStage().detect_structural(candidates)
        assert "B" in result.suppressed
        assert result.suppressed["B"] == "A"

    def test_different_hash_not_suppressed(self):
        candidates = _make_candidates(("A", "aaaaaa"), ("B", "bbbbbb"))
        result = DedupStage().detect_structural(candidates)
        assert len(result.suppressed) == 0

    def test_formula_jaccard_suppressed(self):
        # hash は違うが formula が完全一致 (Jaccard = 1.0)
        candidates = _make_candidates(
            ("A", "hash1", "momentum rank lag"),
            ("B", "hash2", "momentum rank lag"),
        )
        result = DedupStage().detect_structural(candidates)
        assert "B" in result.suppressed

    def test_empty_candidates(self):
        result = DedupStage().detect_structural([])
        assert result.dedup_count == 0

    def test_single_candidate_not_suppressed(self):
        candidates = _make_candidates(("A", "abc"))
        result = DedupStage().detect_structural(candidates)
        assert len(result.suppressed) == 0

    def test_threshold_respected(self):
        # 共通プレフィックス 3/6 = 0.5, threshold=0.6 なら抑制されない
        candidates = _make_candidates(("A", "abcdef"), ("B", "abcxyz"))
        result = DedupStage(structural_threshold=0.6).detect_structural(candidates)
        assert len(result.suppressed) == 0

    def test_chain_suppression(self):
        # A と B が重複 → B 抑制。B と C の比較は B が抑制済みなのでスキップ
        candidates = _make_candidates(("A", "xxxxxx"), ("B", "xxxxxx"), ("C", "yyyyyy"))
        result = DedupStage().detect_structural(candidates)
        assert "B" in result.suppressed
        assert "C" not in result.suppressed

    def test_result_type(self):
        candidates = _make_candidates(("A", "abc"), ("B", "xyz"))
        result = DedupStage().detect_structural(candidates)
        assert isinstance(result, StructuralDedupResult)

    def test_dedup_count(self):
        candidates = _make_candidates(
            ("A", "xxxxxx"), ("B", "xxxxxx"), ("C", "xxxxxx")
        )
        result = DedupStage().detect_structural(candidates)
        assert result.dedup_count == 2  # B と C が抑制される


# ===========================================================================
# 8. TestSelectPreferred
# ===========================================================================

class TestSelectPreferred:
    def _stage(self):
        return DedupStage()

    def _eval_map(self, **kwargs) -> Dict[str, _EvalProxy]:
        """{id: (complexity, crowding, entropy)} から eval_map を生成。"""
        return {
            cid: _EvalProxy(
                candidate_id=cid,
                complexity_penalty=vals[0],
                crowding_penalty=vals[1],
                regime_entropy_score=vals[2],
            )
            for cid, vals in kwargs.items()
        }

    def test_lower_complexity_wins(self):
        dup_pairs = [("A", "B", 0.95)]
        em = self._eval_map(A=(0.1, 0.0, 0.0), B=(0.5, 0.0, 0.0))
        result = self._stage()._select_preferred(dup_pairs, em, 0.90)
        assert result.suppressed["B"] is True
        assert result.suppressed_by["B"] == "A"

    def test_lower_crowding_wins_tiebreak(self):
        dup_pairs = [("A", "B", 0.95)]
        em = self._eval_map(A=(0.1, 0.2, 0.0), B=(0.1, 0.5, 0.0))
        result = self._stage()._select_preferred(dup_pairs, em, 0.90)
        assert result.suppressed["B"] is True

    def test_higher_entropy_wins_tiebreak(self):
        dup_pairs = [("A", "B", 0.95)]
        em = self._eval_map(A=(0.1, 0.1, 0.8), B=(0.1, 0.1, 0.2))
        result = self._stage()._select_preferred(dup_pairs, em, 0.90)
        assert result.suppressed["B"] is True

    def test_result_type(self):
        result = self._stage()._select_preferred([], {}, 0.90)
        assert isinstance(result, SignalDedupResult)

    def test_dedup_count(self):
        dup_pairs = [("A", "B", 0.95), ("A", "C", 0.92)]
        em = self._eval_map(A=(0.1, 0.0, 0.0), B=(0.9, 0.0, 0.0), C=(0.8, 0.0, 0.0))
        result = self._stage()._select_preferred(dup_pairs, em, 0.90)
        assert result.dedup_count == 2

    def test_already_suppressed_skipped(self):
        # B が先に抑制されたら B-C ペアは処理されない (C は抑制されない)
        dup_pairs = [("A", "B", 0.99), ("B", "C", 0.95)]
        em = self._eval_map(
            A=(0.1, 0.0, 0.0), B=(0.9, 0.0, 0.0), C=(0.5, 0.0, 0.0)
        )
        result = self._stage()._select_preferred(dup_pairs, em, 0.90)
        assert result.suppressed.get("B") is True
        assert result.suppressed.get("C") is False  # B が既に抑制済みでスキップ

    def test_threshold_recorded(self):
        result = self._stage()._select_preferred([], {}, 0.88)
        assert result.threshold_used == 0.88


# ===========================================================================
# 9. TestApplySignal
# ===========================================================================

class TestApplySignal:
    def _make_evals(self, *ids):
        return [_MockEvaluation(candidate_id=cid) for cid in ids]

    def test_disabled_returns_original(self):
        stage = DedupStage(signal_dedup_enabled=False)
        evals = self._make_evals("A", "B")
        sm = {"A": [1.0]*5, "B": [1.0]*5}
        updated, result = stage.apply_signal(evals, sm)
        assert updated is evals
        assert result.dedup_count == 0

    def test_single_signal_no_dedup(self):
        stage = DedupStage()
        evals = self._make_evals("A")
        sm = {"A": [1.0]*5}
        updated, result = stage.apply_signal(evals, sm)
        assert result.dedup_count == 0

    def test_high_corr_suppressed(self):
        stage = DedupStage(signal_threshold=0.90)
        evals = [
            _MockEvaluation(candidate_id="A", complexity_penalty=0.1),
            _MockEvaluation(candidate_id="B", complexity_penalty=0.9),
        ]
        sm = {
            "A": [1.0, 2.0, 3.0, 4.0, 5.0],
            "B": [1.0, 2.0, 3.0, 4.0, 5.0],  # A と完全相関
        }
        updated, result = stage.apply_signal(evals, sm)
        assert result.dedup_count == 1
        suppressed_id = [cid for cid, v in result.suppressed.items() if v]
        assert "B" in suppressed_id

    def test_suppressed_flag_set_on_eval(self):
        stage = DedupStage(signal_threshold=0.90)
        evals = [
            _MockEvaluation(candidate_id="A", complexity_penalty=0.1),
            _MockEvaluation(candidate_id="B", complexity_penalty=0.9),
        ]
        sm = {
            "A": [1.0, 2.0, 3.0, 4.0, 5.0],
            "B": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
        updated, result = stage.apply_signal(evals, sm)
        ev_b = next(e for e in updated if e.candidate_id == "B")
        assert ev_b.suppressed_by_signal_dedup == "A"

    def test_low_corr_not_suppressed(self):
        stage = DedupStage(signal_threshold=0.90)
        evals = self._make_evals("A", "B")
        sm = {
            "A": [1.0, 2.0, 3.0, 4.0, 5.0],
            "B": [5.0, 4.0, 3.0, 2.0, 1.0],  # 完全負の相関 (|ρ|=1.0) → これは除外される
        }
        # 負の相関も |ρ| > threshold なので抑制される
        updated, result = stage.apply_signal(evals, sm)
        assert result.dedup_count == 1

    def test_returns_signal_dedup_result(self):
        stage = DedupStage()
        evals = self._make_evals("A", "B")
        sm = {"A": [1.0]*5, "B": [2.0]*5}
        _, result = stage.apply_signal(evals, sm)
        assert isinstance(result, SignalDedupResult)

    def test_custom_threshold(self):
        stage = DedupStage(signal_threshold=0.90)
        evals = self._make_evals("A", "B")
        sm = {
            "A": [1.0, 2.0, 3.0, 4.0, 5.0],
            "B": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
        _, result = stage.apply_signal(evals, sm, threshold=0.99)
        # threshold=0.99 → 完全相関 1.0 > 0.99 なので抑制される
        assert result.dedup_count == 1

    def test_empty_signal_values_excluded(self):
        stage = DedupStage()
        evals = self._make_evals("A", "B")
        sm = {"A": [], "B": [1.0]*5}  # A は空 → active_signals < 2
        _, result = stage.apply_signal(evals, sm)
        assert result.dedup_count == 0


# ===========================================================================
# 10. TestDedupRunResult
# ===========================================================================

class TestDedupRunResult:
    def _make_result(self, structural_suppressed=None, signal_suppressed=None):
        structural = StructuralDedupResult(
            candidate_ids=["A", "B", "C"],
            suppressed=structural_suppressed or {},
            threshold_used=0.95,
            dedup_count=len(structural_suppressed or {}),
        )
        signal = SignalDedupResult(
            candidate_ids=["A", "B"],
            suppressed=signal_suppressed or {},
            suppressed_by={},
            corr_pairs=[],
            threshold_used=0.90,
            dedup_count=sum(1 for v in (signal_suppressed or {}).values() if v),
        )
        return DedupRunResult(structural=structural, signal=signal)

    def test_all_suppressed_union(self):
        result = self._make_result(
            structural_suppressed={"B": "A"},
            signal_suppressed={"C": True},
        )
        assert "B" in result.all_suppressed_ids
        assert "C" in result.all_suppressed_ids

    def test_is_suppressed_structural(self):
        result = self._make_result(structural_suppressed={"B": "A"})
        assert result.is_suppressed("B") is True
        assert result.is_suppressed("A") is False

    def test_is_suppressed_signal(self):
        result = self._make_result(signal_suppressed={"C": True})
        assert result.is_suppressed("C") is True

    def test_total_suppressed(self):
        result = self._make_result(
            structural_suppressed={"B": "A"},
            signal_suppressed={"C": True},
        )
        assert result.total_suppressed == 2

    def test_empty_result(self):
        result = self._make_result()
        assert result.total_suppressed == 0
        assert result.is_suppressed("A") is False


# ===========================================================================
# 11. TestDedupStageRun
# ===========================================================================

class TestDedupStageRun:
    def test_run_returns_dedup_run_result(self):
        stage = DedupStage()
        candidates = _make_candidates(("A", "hash1"), ("B", "hash2"))
        evals = [_MockEvaluation("A"), _MockEvaluation("B")]
        result = stage.run(candidates, evals)
        assert isinstance(result, DedupRunResult)

    def test_run_structural_dedup_fires(self):
        stage = DedupStage()
        candidates = _make_candidates(("A", "xxxxxx"), ("B", "xxxxxx"))
        evals = [_MockEvaluation("A"), _MockEvaluation("B")]
        result = stage.run(candidates, evals)
        assert "B" in result.structural.suppressed

    def test_run_signal_dedup_fires(self):
        stage = DedupStage(signal_threshold=0.90)
        candidates = _make_candidates(("A", "h1"), ("B", "h2"))
        evals = [
            _MockEvaluation("A", complexity_penalty=0.1),
            _MockEvaluation("B", complexity_penalty=0.9),
        ]
        sm = {
            "A": [1.0, 2.0, 3.0, 4.0, 5.0],
            "B": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
        result = stage.run(candidates, evals, signal_matrix=sm)
        assert result.signal.dedup_count == 1

    def test_run_no_signal_matrix(self):
        stage = DedupStage()
        candidates = _make_candidates(("A", "h1"), ("B", "h2"))
        evals = [_MockEvaluation("A"), _MockEvaluation("B")]
        result = stage.run(candidates, evals, signal_matrix=None)
        assert result.signal.dedup_count == 0

    def test_run_combined_suppressed_ids(self):
        stage = DedupStage(signal_threshold=0.90)
        candidates = _make_candidates(
            ("A", "xxxxxx"),  # 構造重複 → B を抑制
            ("B", "xxxxxx"),
            ("C", "hhhhhh"),  # シグナル重複 → D を抑制
            ("D", "iiiiii"),
        )
        evals = [
            _MockEvaluation("A"), _MockEvaluation("B"),
            _MockEvaluation("C", complexity_penalty=0.1),
            _MockEvaluation("D", complexity_penalty=0.9),
        ]
        sm = {
            "C": [1.0, 2.0, 3.0, 4.0, 5.0],
            "D": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
        result = stage.run(candidates, evals, signal_matrix=sm)
        assert "B" in result.all_suppressed_ids
        assert "D" in result.all_suppressed_ids

    def test_run_empty_candidates(self):
        stage = DedupStage()
        result = stage.run([], [])
        assert result.total_suppressed == 0


# ===========================================================================
# 12. TestBackwardCompatSignalDedup
# ===========================================================================

class TestBackwardCompatSignalDedup:
    """frost_signal_dedup.py の後方互換 API が DedupStage に委譲されることを検証"""

    def test_compute_signal_correlation_matrix(self):
        from analytics.python.frost.frost_signal_dedup import compute_signal_correlation_matrix
        sm = {"A": [1.0, 2.0, 3.0, 4.0, 5.0], "B": [1.0, 2.0, 3.0, 4.0, 5.0]}
        matrix = compute_signal_correlation_matrix(sm)
        assert ("A", "B") in matrix
        assert abs(matrix[("A", "B")] - 1.0) < 1e-9

    def test_find_oos_duplicates(self):
        from analytics.python.frost.frost_signal_dedup import find_oos_duplicates
        matrix = {("A", "B"): 0.95, ("A", "C"): 0.80}
        pairs = find_oos_duplicates(matrix, threshold=0.90)
        assert len(pairs) == 1
        assert pairs[0][0] == "A" and pairs[0][1] == "B"

    def test_select_preferred_from_duplicates(self):
        from analytics.python.frost.frost_signal_dedup import (
            select_preferred_from_duplicates,
            _EvalProxy,
            SignalDedupResult,
        )
        dup_pairs = [("A", "B", 0.95)]
        eval_map = {
            "A": _EvalProxy(candidate_id="A", complexity_penalty=0.1),
            "B": _EvalProxy(candidate_id="B", complexity_penalty=0.9),
        }
        result = select_preferred_from_duplicates(dup_pairs, eval_map)
        assert isinstance(result, SignalDedupResult)
        assert result.suppressed["B"] is True

    def test_apply_signal_dedup_high_corr(self):
        from analytics.python.frost.frost_signal_dedup import apply_signal_dedup
        evals = [
            _MockEvaluation("A", complexity_penalty=0.1),
            _MockEvaluation("B", complexity_penalty=0.9),
        ]
        sm = {
            "A": [1.0, 2.0, 3.0, 4.0, 5.0],
            "B": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
        updated, result = apply_signal_dedup(evals, sm, threshold=0.90)
        assert result.dedup_count == 1

    def test_apply_signal_dedup_single_signal(self):
        from analytics.python.frost.frost_signal_dedup import apply_signal_dedup
        evals = [_MockEvaluation("A")]
        sm = {"A": [1.0, 2.0, 3.0]}
        updated, result = apply_signal_dedup(evals, sm)
        assert result.dedup_count == 0

    def test_signal_dedup_result_imported(self):
        from analytics.python.frost.frost_signal_dedup import SignalDedupResult as SDR
        from analytics.python.frost.dedup_stage import SignalDedupResult as SDR2
        assert SDR is SDR2  # 再エクスポートを確認

    def test_eval_proxy_attributes(self):
        from analytics.python.frost.frost_signal_dedup import _EvalProxy
        proxy = _EvalProxy(candidate_id="X", frost_score=1.5)
        assert proxy.frost_score == 1.5
        assert proxy.complexity_penalty == 0.0

    def test_module_level_corr_max(self):
        import analytics.python.frost.frost_signal_dedup as fsd
        assert hasattr(fsd, "_CORR_MAX")
        assert fsd._CORR_MAX == float(os.environ.get("FROST_SIGNAL_CORR_MAX", "0.90"))


# ===========================================================================
# 13. TestBackwardCompatRanker
# ===========================================================================

class TestBackwardCompatRanker:
    """frost_ranker.detect_near_duplicates() が DedupStage 委譲になっていることを検証"""

    def _mock_candidate(self, cid, chash="", formula=None):
        from analytics.python.frost.frost_contracts import FrostCandidate
        return FrostCandidate(
            run_id="run-001",
            candidate_id=cid,
            trace_id="trace-001",
            candidate_hash=chash,
            formula_text=formula,
        )

    def test_detect_near_duplicates_identical_hash(self):
        from analytics.python.frost.frost_ranker import detect_near_duplicates
        c1 = self._mock_candidate("A", "abcdef")
        c2 = self._mock_candidate("B", "abcdef")
        result = detect_near_duplicates([c1, c2])
        assert "B" in result
        assert result["B"] == "A"

    def test_detect_near_duplicates_no_dup(self):
        from analytics.python.frost.frost_ranker import detect_near_duplicates
        c1 = self._mock_candidate("A", "aaaaaa")
        c2 = self._mock_candidate("B", "bbbbbb")
        result = detect_near_duplicates([c1, c2])
        assert len(result) == 0

    def test_detect_near_duplicates_formula_match(self):
        from analytics.python.frost.frost_ranker import detect_near_duplicates
        c1 = self._mock_candidate("A", "h1", "momentum rank lag")
        c2 = self._mock_candidate("B", "h2", "momentum rank lag")
        result = detect_near_duplicates([c1, c2])
        assert "B" in result

    def test_detect_near_duplicates_threshold(self):
        from analytics.python.frost.frost_ranker import detect_near_duplicates
        c1 = self._mock_candidate("A", "abcdef")
        c2 = self._mock_candidate("B", "abcxyz")
        # 共通プレフィックス 3/6 = 0.5 < threshold=0.95
        result = detect_near_duplicates([c1, c2], threshold=0.95)
        assert len(result) == 0

    def test_detect_returns_dict(self):
        from analytics.python.frost.frost_ranker import detect_near_duplicates
        c1 = self._mock_candidate("A", "abc")
        result = detect_near_duplicates([c1])
        assert isinstance(result, dict)

    def test_rank_evaluations_unchanged(self):
        """rank_evaluations は DedupStage と無関係なので引き続き動作する"""
        from analytics.python.frost.frost_ranker import rank_evaluations
        from analytics.python.frost.frost_contracts import FrostEvaluation

        ev1 = FrostEvaluation(
            run_id="r", candidate_id="A", trace_id="t",
            frost_score=0.8, hard_gate_passed=True,
        )
        ev2 = FrostEvaluation(
            run_id="r", candidate_id="B", trace_id="t",
            frost_score=0.6, hard_gate_passed=True,
        )
        ranked = rank_evaluations([ev2, ev1])
        assert ranked[0][1].candidate_id == "A"  # 高スコアが先頭

    def test_check_family_limit_unchanged(self):
        """check_family_limit は DedupStage と無関係なので引き続き動作する"""
        from analytics.python.frost.frost_ranker import check_family_limit
        from analytics.python.frost.frost_contracts import FrostCandidate

        c = FrostCandidate(
            run_id="r", candidate_id="A", trace_id="t",
            source_candidate_id="run-0001-xyz",
        )
        # source_candidate_id="run-0001-xyz" の先頭 8 文字 = "run-0001"
        counts = {"run-0001": 3}
        assert check_family_limit(c, counts, max_same_family=3) is True
        assert check_family_limit(c, {}, max_same_family=3) is False
