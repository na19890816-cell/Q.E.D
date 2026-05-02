"""
tests/unit/test_eml_core.py
---------------------------
EML コアモジュールのユニットテスト。
- eml_core: ノード生成・深さ計算・バリデーション
- eml_tree: 木生成・snap・prune
- eml_compiler: compile_to_expr・validate_safe_expr
- eml_runtime_lower: lower_expr
- eml_fitness: calc_rank_ic / compute_fitness
- eml_search: exhaustive_search / gradient_search
"""
from __future__ import annotations

import sys
import os
import math
import random
import pytest
import numpy as np
import pandas as pd

# プロジェクトルート追加
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from analytics.python.alpha.eml.eml_core import (
    EML_DEPTH_MAX, EML_DEPTH_MIN,
    EMLNode, NODE_CONST, NODE_EML, NODE_TERMINAL,
    validate_depth,
)
from analytics.python.alpha.eml.eml_tree import (
    build_const, build_eml_node, build_leaf,
    enumerate_trees, random_tree, snap_weights, prune_snapped,
)
from analytics.python.alpha.eml.eml_compiler import (
    compile_to_expr, validate_safe_expr, assert_safe_expr,
)
from analytics.python.alpha.eml.eml_runtime_lower import (
    lower_expr, lower_and_rank_normalize,
)
from analytics.python.alpha.eml.eml_fitness import (
    calc_rank_ic, calc_sharpe, compute_fitness, simple_rank_ic_fitness,
)
from analytics.python.alpha.eml.eml_search import (
    EMLCandidate, exhaustive_search, gradient_search,
)


# ─────────────────────────────────────────────
# フィクスチャ
# ─────────────────────────────────────────────

TERMINALS = ["r1", "r5", "r20", "gap", "vol"]

@pytest.fixture
def simple_df():
    """5 terminal × 200 rows の合成 DataFrame。"""
    rng = np.random.default_rng(42)
    n = 200
    return pd.DataFrame({
        t: rng.normal(0, 1, n)
        for t in TERMINALS
    })

@pytest.fixture
def simple_target(simple_df):
    """simple_df["r1"] をターゲットとする。"""
    rng = np.random.default_rng(99)
    return simple_df["r1"] + rng.normal(0, 0.5, len(simple_df))


# ─────────────────────────────────────────────
# eml_core
# ─────────────────────────────────────────────

class TestEMLCore:
    def test_leaf_depth_is_zero(self):
        node = build_leaf("r1")
        assert node.depth() == 0

    def test_eml_depth_1(self):
        node = build_eml_node(build_leaf("r1"), build_leaf("r5"))
        assert node.depth() == 1

    def test_eml_depth_2(self):
        inner = build_eml_node(build_leaf("r1"), build_leaf("r5"))
        outer = build_eml_node(inner, build_leaf("r20"))
        assert outer.depth() == 2

    def test_validate_depth_passes_for_depth_2(self):
        inner = build_eml_node(build_leaf("r1"), build_leaf("r5"))
        outer = build_eml_node(inner, build_leaf("r20"))
        validate_depth(outer)  # no exception

    def test_validate_depth_fails_for_depth_5(self):
        # depth=5 の木を手動生成
        node = build_leaf("r1")
        for _ in range(5):
            node = build_eml_node(node, build_leaf("r5"))
        with pytest.raises(ValueError, match="depth=5"):
            validate_depth(node)

    def test_node_count(self):
        inner = build_eml_node(build_leaf("r1"), build_leaf("r5"))
        outer = build_eml_node(inner, build_leaf("r20"))
        # outer(1) + inner(1) + 3 leaves = 5
        assert outer.node_count() == 5

    def test_terminals_used(self):
        inner = build_eml_node(build_leaf("r1"), build_leaf("r5"))
        outer = build_eml_node(inner, build_leaf("r20"))
        assert outer.terminals_used() == {"r1", "r5", "r20"}

    def test_to_json_from_json_roundtrip(self):
        inner = build_eml_node(build_leaf("r1"), build_leaf("r5"), raw_weight=1.2)
        outer = build_eml_node(inner, build_leaf("r20"), raw_weight=-0.5)
        s = outer.to_json()
        restored = EMLNode.from_json(s)
        assert restored.kind == NODE_EML
        assert restored.raw_weight == -0.5
        assert restored.left.kind == NODE_EML
        assert restored.left.raw_weight == 1.2


# ─────────────────────────────────────────────
# eml_tree
# ─────────────────────────────────────────────

class TestEMLTree:
    def test_random_tree_depth_in_range(self):
        rng = random.Random(42)
        for _ in range(20):
            t = random_tree(3, TERMINALS, rng=rng)
            d = t.depth()
            assert EML_DEPTH_MIN <= d <= EML_DEPTH_MAX, f"depth={d} out of range"

    def test_enumerate_trees_returns_nonempty(self):
        trees = enumerate_trees(2, TERMINALS)
        assert len(trees) > 0

    def test_enumerate_trees_all_valid_depth(self):
        # depth=2, 少ないterminalで高速化
        trees = enumerate_trees(2, ["r1", "r5"])
        for t in trees:
            d = t.depth()
            assert EML_DEPTH_MIN <= d <= EML_DEPTH_MAX

    def test_snap_weights_sets_0_or_1(self):
        node = random_tree(3, TERMINALS)
        snapped = snap_weights(node)
        for n in _collect_eml_nodes(snapped):
            assert n.snapped_weight in (0.0, 1.0)

    def test_prune_snapped_reduces_to_leaf_or_terminal(self):
        node = build_eml_node(build_leaf("r1"), build_leaf("r5"), raw_weight=2.0)
        snapped = snap_weights(node)
        pruned = prune_snapped(snapped)
        # raw_weight=2.0 → sigmoid > 0.5 → left (r1)
        assert pruned.kind == NODE_TERMINAL
        assert pruned.terminal_name == "r1"

    def test_depth_guard_on_enumerate(self):
        """EML_DEPTH_MAX を超える深さの木は含まれない。"""
        # 少ない terminal で高速化
        trees = enumerate_trees(EML_DEPTH_MAX + 1, ["r1", "r5"])
        for t in trees:
            assert t.depth() <= EML_DEPTH_MAX


# ─────────────────────────────────────────────
# eml_compiler
# ─────────────────────────────────────────────

class TestEMLCompiler:
    def test_compile_terminal_node(self):
        node = build_leaf("r1")
        expr = compile_to_expr(node)
        assert expr == "r1"

    def test_compile_const_node(self):
        node = build_const()
        expr = compile_to_expr(node)
        assert expr == "1.0"

    def test_compile_eml_node_left_selected(self):
        # raw_weight=5.0 → sigmoid ≫ 0.5 → left
        node = build_eml_node(build_leaf("r1"), build_leaf("r5"), raw_weight=5.0)
        expr = compile_to_expr(node)
        assert expr == "r1"

    def test_compile_eml_node_right_selected(self):
        # raw_weight=-5.0 → sigmoid ≪ 0.5 → right
        node = build_eml_node(build_leaf("r1"), build_leaf("r5"), raw_weight=-5.0)
        expr = compile_to_expr(node)
        assert expr == "r5"

    def test_validate_safe_expr_valid_terminal(self):
        assert validate_safe_expr("r1", {"r1", "r5"}) is True

    def test_validate_safe_expr_valid_const(self):
        assert validate_safe_expr("1.0", {"r1"}) is True

    def test_validate_safe_expr_invalid_expr(self):
        assert validate_safe_expr("os.system('rm -rf /')", {"r1"}) is False

    def test_assert_safe_expr_raises_on_invalid(self):
        with pytest.raises(ValueError, match="Unsafe"):
            assert_safe_expr("__import__('os')", {"r1"}, label="test")

    def test_assert_safe_expr_passes_on_valid(self):
        assert_safe_expr("r1", {"r1", "r5"})  # no exception


# ─────────────────────────────────────────────
# eml_runtime_lower
# ─────────────────────────────────────────────

class TestEMLRuntimeLower:
    def test_lower_terminal_returns_series(self, simple_df):
        result = lower_expr("r1", simple_df)
        assert isinstance(result, pd.Series)
        assert len(result) == len(simple_df)

    def test_lower_const_returns_ones(self, simple_df):
        result = lower_expr("1.0", simple_df)
        assert (result == 1.0).all()

    def test_lower_unknown_terminal_raises(self, simple_df):
        with pytest.raises((ValueError, KeyError)):
            lower_expr("unknown_terminal", simple_df)

    def test_lower_and_rank_normalize_range(self, simple_df):
        result = lower_and_rank_normalize("r1", simple_df)
        assert result.min() >= -0.5 - 1e-9
        assert result.max() <= 0.5 + 1e-9


# ─────────────────────────────────────────────
# eml_fitness
# ─────────────────────────────────────────────

class TestEMLFitness:
    def test_rank_ic_range(self, simple_df, simple_target):
        ic = calc_rank_ic(simple_df["r1"], simple_target)
        assert -1.0 <= ic <= 1.0

    def test_rank_ic_perfect_correlation(self):
        s = pd.Series(range(100), dtype=float)
        ic = calc_rank_ic(s, s)
        assert abs(ic - 1.0) < 1e-6

    def test_sharpe_positive_for_constant_positive_return(self):
        r = pd.Series([0.01] * 100)
        s = calc_sharpe(r)
        assert s > 0

    def test_compute_fitness_returns_float(self, simple_df, simple_target):
        node = snap_weights(random_tree(2, TERMINALS, rng=random.Random(1)))
        score = compute_fitness(node, simple_df, simple_target)
        assert isinstance(score, float)
        assert not math.isnan(score)

    def test_simple_rank_ic_fitness_returns_float(self, simple_df, simple_target):
        node = snap_weights(random_tree(2, TERMINALS, rng=random.Random(1)))
        score = simple_rank_ic_fitness(node, simple_df, simple_target)
        assert isinstance(score, float)


# ─────────────────────────────────────────────
# eml_search
# ─────────────────────────────────────────────

class TestEMLSearch:
    RUN_ID   = "test-run-001"
    TRACE_ID = "test-trace-001"

    def test_exhaustive_search_returns_candidates(self, simple_df, simple_target):
        cands = exhaustive_search(
            terminals=["r1", "r5"],
            max_depth=2,
            run_id=self.RUN_ID,
            trace_id=self.TRACE_ID,
            feature_df=simple_df,
            target=simple_target,
            fitness_fn=simple_rank_ic_fitness,
            top_k=5,
        )
        assert len(cands) <= 5
        assert all(isinstance(c, EMLCandidate) for c in cands)

    def test_exhaustive_search_depth_guard(self, simple_df, simple_target):
        """exhaustive で depth > EML_DEPTH_MAX の木は含まれない。"""
        # terminal を絞って高速化
        cands = exhaustive_search(
            terminals=["r1", "r5"],
            max_depth=EML_DEPTH_MAX + 2,  # クランプされるはず
            run_id=self.RUN_ID,
            trace_id=self.TRACE_ID,
            feature_df=simple_df,
            target=simple_target,
            fitness_fn=simple_rank_ic_fitness,
            top_k=20,
        )
        for c in cands:
            assert c.node.depth() <= EML_DEPTH_MAX

    def test_gradient_search_returns_candidates(self, simple_df, simple_target):
        cands = gradient_search(
            terminals=TERMINALS,
            max_depth=2,
            run_id=self.RUN_ID,
            trace_id=self.TRACE_ID,
            feature_df=simple_df,
            target=simple_target,
            fitness_fn=simple_rank_ic_fitness,
            n_init=3,
            adam_steps=10,
            top_k=3,
            rng_seed=42,
        )
        assert len(cands) <= 3
        assert all(isinstance(c, EMLCandidate) for c in cands)

    def test_candidates_have_valid_compiled_expr(self, simple_df, simple_target):
        cands = exhaustive_search(
            terminals=["r1", "r5"],
            max_depth=2,
            run_id=self.RUN_ID,
            trace_id=self.TRACE_ID,
            feature_df=simple_df,
            target=simple_target,
            fitness_fn=simple_rank_ic_fitness,
            top_k=10,
        )
        terminal_set = {"r1", "r5"}
        for c in cands:
            assert validate_safe_expr(c.compiled_expr, terminal_set) or \
                   c.compiled_expr == "1.0", \
                   f"Unsafe expr: {c.compiled_expr!r}"

    def test_trace_id_propagated(self, simple_df, simple_target):
        cands = exhaustive_search(
            terminals=["r1", "r5"],
            max_depth=2,
            run_id=self.RUN_ID,
            trace_id=self.TRACE_ID,
            feature_df=simple_df,
            target=simple_target,
            fitness_fn=simple_rank_ic_fitness,
            top_k=5,
        )
        for c in cands:
            assert c.trace_id == self.TRACE_ID, "trace_id が伝播されていない"

    def test_sorted_by_fitness_descending(self, simple_df, simple_target):
        cands = exhaustive_search(
            terminals=["r1", "r5"],
            max_depth=2,
            run_id=self.RUN_ID,
            trace_id=self.TRACE_ID,
            feature_df=simple_df,
            target=simple_target,
            fitness_fn=simple_rank_ic_fitness,
            top_k=10,
        )
        scores = [c.fitness_score for c in cands]
        assert scores == sorted(scores, reverse=True), "フィットネスが降順でない"


# ─────────────────────────────────────────────
# ヘルパー
# ─────────────────────────────────────────────

def _collect_eml_nodes(node: EMLNode):
    if node.kind != NODE_EML:
        return []
    result = [node]
    if node.left:
        result += _collect_eml_nodes(node.left)
    if node.right:
        result += _collect_eml_nodes(node.right)
    return result
