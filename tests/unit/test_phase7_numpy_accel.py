"""
test_phase7_numpy_accel.py
--------------------------
Phase 7 numpy 化の数値同等性テスト (ADR-001)。

検証条件:
  - 各 numpy 版関数の戻り値が pure Python 版と相対誤差 1e-8 以内であること
  - 公開 API シグネチャが変化していないこと
  - 決定反転ゼロ: 閾値比較の結果が変わらないこと

実行:
  pytest tests/unit/test_phase7_numpy_accel.py -v
  pytest -m "not numpy_accel" で全スキップ可能
"""
from __future__ import annotations

import math
import statistics
import sys
import os
import pytest

# プロジェクトルートを sys.path に追加
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# ============================================================
# Pure Python リファレンス実装（numpy 化前の実装をインライン再現）
# ============================================================

def _pearson_pure(xs, ys):
    """Phase 7 前の純 Python 実装（比較用）。"""
    n = len(xs)
    if n < 3:
        return 0.0
    def _safe(v):
        try:
            f = float(v)
            return 0.0 if (math.isnan(f) or math.isinf(f)) else f
        except (TypeError, ValueError):
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
    except Exception:
        return 0.0


def _ols_simple_pure(y, x):
    """Phase 7 前の純 Python OLS 実装（比較用）。"""
    n = len(y)
    if n < 3:
        return 0.0, 0.0, 0.0, 0.0
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    ss_xx = sum((xi - mean_x) ** 2 for xi in x)
    ss_xy = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    ss_yy = sum((yi - mean_y) ** 2 for yi in y)
    if ss_xx < 1e-15:
        return mean_y, 0.0, 0.0, 0.0
    beta = ss_xy / ss_xx
    alpha = mean_y - beta * mean_x
    ss_res = sum((yi - (alpha + beta * xi)) ** 2 for xi, yi in zip(x, y))
    r_squared = 1.0 - ss_res / ss_yy if ss_yy > 1e-15 else 0.0
    r_squared = max(0.0, min(1.0, r_squared))
    residual_std = math.sqrt(ss_res / max(1, n - 2))
    return alpha, beta, r_squared, residual_std


def _rel_err(a: float, b: float) -> float:
    """相対誤差。両方ゼロなら 0.0。"""
    denom = max(abs(a), abs(b), 1e-15)
    return abs(a - b) / denom


REL_TOL = 1e-8  # ADR-001 数値同等性上限


# ============================================================
# 乱数シード固定ヘルパー
# ============================================================

def _make_signal(seed: int, n: int = 60):
    """再現可能な疑似シグナル系列を生成する。"""
    import random
    rng = random.Random(seed)
    return [rng.gauss(0.0, 1.0) for _ in range(n)]


# ============================================================
# TestDedupStagePearson
# ============================================================

@pytest.mark.numpy_accel
class TestDedupStagePearson:
    """dedup_stage._pearson の numpy 化数値同等性テスト。"""

    def _pearson_numpy(self, xs, ys):
        from analytics.python.frost.dedup_stage import _pearson
        return _pearson(xs, ys)

    def test_typical_positive_correlation(self):
        xs = _make_signal(1, 50)
        ys = [x + _make_signal(2, 50)[i] * 0.1 for i, x in enumerate(xs)]
        ref = _pearson_pure(xs, ys)
        got = self._pearson_numpy(xs, ys)
        assert _rel_err(got, ref) < REL_TOL, f"ref={ref}, got={got}"

    def test_typical_negative_correlation(self):
        xs = _make_signal(3, 50)
        ys = [-x + _make_signal(4, 50)[i] * 0.05 for i, x in enumerate(xs)]
        ref = _pearson_pure(xs, ys)
        got = self._pearson_numpy(xs, ys)
        assert _rel_err(got, ref) < REL_TOL

    def test_zero_correlation_noise(self):
        xs = _make_signal(5, 100)
        ys = _make_signal(6, 100)
        ref = _pearson_pure(xs, ys)
        got = self._pearson_numpy(xs, ys)
        assert _rel_err(got, ref) < REL_TOL

    def test_short_series_returns_zero(self):
        assert self._pearson_numpy([1.0, 2.0], [2.0, 3.0]) == 0.0

    def test_constant_series_returns_zero(self):
        xs = [1.0] * 20
        ys = _make_signal(7, 20)
        assert self._pearson_numpy(xs, ys) == 0.0

    def test_nan_inf_sanitized(self):
        xs = [float("nan"), float("inf"), 1.0, 2.0, 3.0]
        ys = [1.0, 2.0, 3.0, 4.0, 5.0]
        got = self._pearson_numpy(xs, ys)
        assert math.isfinite(got)

    def test_range_clamped(self):
        xs = _make_signal(8, 30)
        ys = xs[:]
        got = self._pearson_numpy(xs, ys)
        assert -1.0 <= got <= 1.0

    def test_sign_matches_pure_python(self):
        """符号（方向性）が純 Python 版と一致することを確認。"""
        for seed in range(20):
            xs = _make_signal(seed * 2, 40)
            ys = _make_signal(seed * 2 + 1, 40)
            ref = _pearson_pure(xs, ys)
            got = self._pearson_numpy(xs, ys)
            if abs(ref) > 0.05:  # 十分に非ゼロなペアのみ
                assert (ref > 0) == (got > 0), f"sign mismatch seed={seed}"


# ============================================================
# TestDedupStageCorrelationMatrix
# ============================================================

@pytest.mark.numpy_accel
class TestDedupStageCorrelationMatrix:
    """DedupStage.compute_correlation_matrix の numpy 化テスト。"""

    def _compute(self, signal_matrix):
        from analytics.python.frost.dedup_stage import DedupStage
        return DedupStage.compute_correlation_matrix(signal_matrix)

    def test_keys_match_pure_python(self):
        signals = {f"c{i}": _make_signal(i, 50) for i in range(5)}
        result = self._compute(signals)
        ids = sorted(signals.keys())
        expected_keys = {(ids[i], ids[j]) for i in range(5) for j in range(i+1, 5)}
        assert set(result.keys()) == expected_keys

    def test_values_close_to_pure_python(self):
        from analytics.python.frost.dedup_stage import _pearson
        signals = {f"c{i}": _make_signal(i + 10, 60) for i in range(4)}
        result = self._compute(signals)
        ids = sorted(signals.keys())
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                ref = _pearson_pure(signals[a], signals[b])
                got = result[(a, b)]
                assert _rel_err(got, ref) < REL_TOL, f"pair ({a},{b}): ref={ref}, got={got}"

    def test_all_values_in_range(self):
        signals = {f"c{i}": _make_signal(i, 80) for i in range(6)}
        result = self._compute(signals)
        for key, val in result.items():
            assert -1.0 <= val <= 1.0, f"{key}: {val}"

    def test_empty_returns_empty(self):
        assert self._compute({}) == {}

    def test_single_candidate_returns_empty(self):
        assert self._compute({"c0": [1.0, 2.0, 3.0]}) == {}


# ============================================================
# TestFrostPboMedian
# ============================================================

@pytest.mark.numpy_accel
class TestFrostPboMedian:
    """frost_pbo.estimate_pbo_from_folds の np.median 化テスト。"""

    def test_pbo_range(self):
        from analytics.python.frost.frost_pbo import estimate_pbo_from_folds
        sharpes = _make_signal(42, 8)
        pbo = estimate_pbo_from_folds(sharpes, min_folds=4)
        assert 0.0 <= pbo <= 1.0

    def test_pbo_same_value_with_numpy_median(self):
        """np.median と statistics.median は同値（偶数長は平均）を確認。"""
        import numpy as np
        for seed in range(10):
            vals = _make_signal(seed, 6)
            np_med = float(np.median(vals))
            py_med = statistics.median(vals)
            assert abs(np_med - py_med) < 1e-12

    def test_insufficient_folds_returns_half(self):
        from analytics.python.frost.frost_pbo import estimate_pbo_from_folds
        assert estimate_pbo_from_folds([1.0, 2.0], min_folds=4) == 0.5

    def test_compute_pbo_all_keys(self):
        from analytics.python.frost.frost_pbo import compute_pbo_all
        folds = [{"sharpe": s} for s in _make_signal(99, 6)]
        result = compute_pbo_all(folds)
        assert "pbo_score" in result
        assert "selection_fragility" in result
        assert 0.0 <= result["pbo_score"] <= 1.0


# ============================================================
# TestFrostCrowdingOls
# ============================================================

@pytest.mark.numpy_accel
class TestFrostCrowdingOls:
    """frost_crowding._ols_simple の np.linalg.lstsq 化数値同等性テスト。"""

    def _ols(self, y, x):
        from analytics.python.frost.frost_crowding import _ols_simple
        return _ols_simple(y, x)

    def test_alpha_beta_close_to_pure_python(self):
        xs = _make_signal(10, 60)
        ys = [2.5 * x + 0.3 + _make_signal(11, 60)[i] * 0.2 for i, x in enumerate(xs)]
        ref = _ols_simple_pure(ys, xs)
        got = self._ols(ys, xs)
        for r, g in zip(ref, got):
            assert _rel_err(r, g) < REL_TOL, f"ref={ref}, got={got}"

    def test_r_squared_range(self):
        xs = _make_signal(12, 50)
        ys = _make_signal(13, 50)
        _, _, r2, _ = self._ols(ys, xs)
        assert 0.0 <= r2 <= 1.0

    def test_perfect_fit_r_squared_near_one(self):
        xs = list(range(1, 31))
        ys = [3.0 * x + 1.0 for x in xs]
        _, _, r2, res_std = self._ols(ys, xs)
        assert abs(r2 - 1.0) < 1e-6
        assert res_std < 1e-8

    def test_short_series_returns_zeros(self):
        assert self._ols([1.0, 2.0], [1.0, 2.0]) == (0.0, 0.0, 0.0, 0.0)

    def test_signature_unchanged(self):
        """戻り値が (alpha, beta, r_squared, residual_std) の 4-tuple であること。"""
        xs = _make_signal(14, 30)
        ys = _make_signal(15, 30)
        result = self._ols(ys, xs)
        assert len(result) == 4
        for v in result:
            assert isinstance(v, float)


# ============================================================
# TestAlphaGenomeCluster
# ============================================================

@pytest.mark.numpy_accel
class TestAlphaGenomeCluster:
    """alpha_genome_cluster の numpy 化テスト。"""

    def _make_genomes(self, n: int, seed: int = 0):
        from analytics.python.genome.alpha_genome_encoder import encode_formula
        formulas = [
            "momentum(close, 5)", "value(pe_ratio)", "quality(roe)",
            "momentum(volume, 10)", "value(pb_ratio)", "quality(roa)",
            "momentum(close, 20)", "reversal(close, 5)", "volatility(close, 20)",
            "size(market_cap)",
        ]
        import random
        rng = random.Random(seed)
        genomes = []
        for i in range(n):
            f = formulas[rng.randint(0, len(formulas) - 1)]
            cid = f"cand_{i:03d}"
            g = encode_formula(cid, f)
            genomes.append(g)
        return genomes

    def test_cluster_result_structure(self):
        from analytics.python.genome.alpha_genome_cluster import cluster_genomes
        genomes = self._make_genomes(20, seed=42)
        result = cluster_genomes(genomes, k=3, random_seed=42)
        assert result.n_clusters == 3
        assert len(result.clusters) == 3
        assert len(result.assignment) == 20

    def test_all_genomes_assigned(self):
        from analytics.python.genome.alpha_genome_cluster import cluster_genomes
        genomes = self._make_genomes(15, seed=7)
        result = cluster_genomes(genomes, k=3, random_seed=7)
        assigned_ids = set(result.assignment.keys())
        original_ids = {g.candidate_id for g in genomes}
        assert assigned_ids == original_ids

    def test_inertia_nonnegative(self):
        from analytics.python.genome.alpha_genome_cluster import cluster_genomes
        genomes = self._make_genomes(10, seed=1)
        result = cluster_genomes(genomes, k=2, random_seed=1)
        assert result.inertia >= 0.0

    def test_l2_dist_returns_float(self):
        from analytics.python.genome.alpha_genome_cluster import _l2_dist
        a = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        d = _l2_dist(a, b)
        assert abs(d - math.sqrt(2)) < 1e-10

    def test_cosine_sim_identical_vectors(self):
        from analytics.python.genome.alpha_genome_cluster import _cosine_sim
        v = [0.3, 0.1, 0.2, 0.0, 0.4, 0.0, 0.0, 0.0, 0.0, 0.0]
        assert abs(_cosine_sim(v, v) - 1.0) < 1e-10

    def test_mean_vector_shape(self):
        from analytics.python.genome.alpha_genome_cluster import _mean_vector
        vecs = [[float(i)] * 10 for i in range(5)]
        mean = _mean_vector(vecs)
        assert len(mean) == 10
        assert abs(mean[0] - 2.0) < 1e-10  # mean of 0,1,2,3,4

    def test_empty_genomes(self):
        from analytics.python.genome.alpha_genome_cluster import cluster_genomes
        result = cluster_genomes([], k=3)
        assert result.n_clusters == 0


# ============================================================
# TestAlphaGenomeSimilarity
# ============================================================

@pytest.mark.numpy_accel
class TestAlphaGenomeSimilarity:
    """alpha_genome_similarity の numpy 化数値同等性テスト。"""

    def _make_genomes(self, n: int, seed: int = 0):
        from analytics.python.genome.alpha_genome_encoder import encode_formula
        import random
        rng = random.Random(seed)
        formulas = [
            "momentum(close, 5)", "value(pe_ratio)", "quality(roe)",
            "momentum(volume, 10)", "value(pb_ratio)",
        ]
        return [
            encode_formula(f"cand_{i:03d}", formulas[rng.randint(0, len(formulas) - 1)])
            for i in range(n)
        ]

    def test_edge_count_is_n_choose_2(self):
        from analytics.python.genome.alpha_genome_similarity import (
            compute_genome_similarity_matrix,
        )
        n = 6
        genomes = self._make_genomes(n)
        edges = compute_genome_similarity_matrix(genomes)
        assert len(edges) == n * (n - 1) // 2

    def test_cosine_sim_range(self):
        from analytics.python.genome.alpha_genome_similarity import (
            compute_genome_similarity_matrix,
        )
        genomes = self._make_genomes(8)
        edges = compute_genome_similarity_matrix(genomes)
        for e in edges:
            assert -1.0 <= e.cosine_similarity <= 1.0, f"out of range: {e.cosine_similarity}"

    def test_l2_dist_nonnegative(self):
        from analytics.python.genome.alpha_genome_similarity import (
            compute_genome_similarity_matrix,
        )
        genomes = self._make_genomes(5)
        edges = compute_genome_similarity_matrix(genomes)
        for e in edges:
            assert e.l2_distance >= 0.0

    def test_values_close_to_encoder_functions(self):
        """numpy 版の値がエンコーダ純 Python 版と相対誤差 1e-8 以内。"""
        from analytics.python.genome.alpha_genome_similarity import (
            compute_genome_similarity_matrix,
        )
        from analytics.python.genome.alpha_genome_encoder import (
            genome_cosine_similarity,
            genome_l2_distance,
        )
        genomes = self._make_genomes(5, seed=123)
        edges = compute_genome_similarity_matrix(genomes)
        genome_map = {g.candidate_id: g for g in genomes}
        for e in edges:
            g1 = genome_map[e.candidate_id_a]
            g2 = genome_map[e.candidate_id_b]
            ref_cos = genome_cosine_similarity(g1, g2)
            ref_l2  = genome_l2_distance(g1, g2)
            assert _rel_err(e.cosine_similarity, ref_cos) < REL_TOL, \
                f"cos mismatch: {e.candidate_id_a}-{e.candidate_id_b}"
            assert _rel_err(e.l2_distance, ref_l2) < REL_TOL, \
                f"l2 mismatch: {e.candidate_id_a}-{e.candidate_id_b}"

    def test_novelty_scores_in_range(self):
        from analytics.python.genome.alpha_genome_similarity import compute_novelty_scores
        genomes = self._make_genomes(7, seed=9)
        results = compute_novelty_scores(genomes)
        for r in results:
            assert 0.0 <= r.novelty_score <= 1.0

    def test_empty_returns_empty(self):
        from analytics.python.genome.alpha_genome_similarity import (
            compute_genome_similarity_matrix,
        )
        assert compute_genome_similarity_matrix([]) == []


# ============================================================
# TestFrostFragilitySurface
# ============================================================

@pytest.mark.numpy_accel
class TestFrostFragilitySurface:
    """frost_fragility_surface の numpy 化テスト。"""

    def _make_eval_func(self, base_sharpe=1.2, sensitivity=0.1):
        from analytics.python.frost.frost_fragility_surface import make_simple_eval_func
        return make_simple_eval_func(base_sharpe, sensitivity)

    def test_result_structure(self):
        from analytics.python.frost.frost_fragility_surface import compute_fragility_surface
        fn = self._make_eval_func()
        result = compute_fragility_surface(fn, param_names=["lookback", "threshold"])
        assert hasattr(result, "fragility_surface_index")
        assert hasattr(result, "std_sharpe")
        assert 0.0 <= result.fragility_surface_index <= 1.0

    def test_mean_sharpe_is_finite(self):
        from analytics.python.frost.frost_fragility_surface import compute_fragility_surface
        fn = self._make_eval_func(base_sharpe=0.5)
        result = compute_fragility_surface(fn, param_names=["lookback"])
        assert math.isfinite(result.mean_sharpe)

    def test_std_sharpe_nonnegative(self):
        from analytics.python.frost.frost_fragility_surface import compute_fragility_surface
        fn = self._make_eval_func()
        result = compute_fragility_surface(fn, param_names=["lookback", "threshold"])
        assert result.std_sharpe >= 0.0

    def test_local_stability_plus_fsi_near_one(self):
        from analytics.python.frost.frost_fragility_surface import compute_fragility_surface
        fn = self._make_eval_func()
        result = compute_fragility_surface(fn, param_names=["lookback"])
        assert abs(result.local_stability_score + result.fragility_surface_index - 1.0) < 1e-10


# ============================================================
# TestNumpyAccelSignatureGuard
# ============================================================

@pytest.mark.numpy_accel
class TestNumpyAccelSignatureGuard:
    """公開 API シグネチャが変化していないことを確認する後方互換テスト。"""

    def test_dedup_stage_pearson_returns_float(self):
        from analytics.python.frost.dedup_stage import _pearson
        result = _pearson([1.0, 2.0, 3.0, 4.0, 5.0], [2.0, 4.0, 6.0, 8.0, 10.0])
        assert isinstance(result, float)

    def test_frost_pbo_estimate_returns_float(self):
        from analytics.python.frost.frost_pbo import estimate_pbo_from_folds
        result = estimate_pbo_from_folds([1.0, 0.5, 1.2, 0.8, 1.5, 0.3])
        assert isinstance(result, float)
        assert 0.0 <= result <= 1.0

    def test_frost_crowding_ols_returns_4tuple(self):
        from analytics.python.frost.frost_crowding import _ols_simple
        result = _ols_simple(
            [1.0, 2.0, 3.0, 4.0, 5.0],
            [0.5, 1.0, 1.5, 2.0, 2.5],
        )
        assert len(result) == 4
        alpha, beta, r2, res_std = result
        assert abs(r2 - 1.0) < 1e-6

    def test_cluster_genomes_returns_clustering_result(self):
        from analytics.python.genome.alpha_genome_cluster import cluster_genomes, ClusteringResult
        from analytics.python.genome.alpha_genome_encoder import encode_formula
        genomes = [encode_formula(f"c{i}", "momentum(close, 5)") for i in range(5)]
        result = cluster_genomes(genomes, k=2)
        assert isinstance(result, ClusteringResult)

    def test_similarity_matrix_returns_list_of_edges(self):
        from analytics.python.genome.alpha_genome_similarity import (
            compute_genome_similarity_matrix,
            GenomeSimilarityEdge,
        )
        from analytics.python.genome.alpha_genome_encoder import encode_formula
        genomes = [encode_formula(f"c{i}", "value(pe_ratio)") for i in range(3)]
        edges = compute_genome_similarity_matrix(genomes)
        assert all(isinstance(e, GenomeSimilarityEdge) for e in edges)
