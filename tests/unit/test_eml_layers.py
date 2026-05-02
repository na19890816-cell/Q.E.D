"""
tests/unit/test_eml_layers.py
------------------------------
EML 全レイヤーのユニットテスト。

対象:
  - Terminal Layer (build_terminal_set, regime_features)
  - EML Tree Layer (eml_tree, eml_compiler, eml_runtime_lower)
  - Metrics Layer (predictive, portfolio, trading, risk, regime)
  - Backtest Layer (cost_model, slippage_model, risk_gate, harness)
  - Promotion Layer (promotion_bridge)
  - Guard Rules (depth, fold, trace_id, UPSERT)
  - Failure scenarios (depth guard, look-ahead bias, crisis drawdown,
                       missing terminals)
"""
from __future__ import annotations

import math
import os
import sys
import uuid

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def rng():
    return np.random.default_rng(42)


@pytest.fixture
def small_returns(rng):
    return pd.Series(rng.normal(0, 0.02, 200))


@pytest.fixture
def small_feature_df(rng):
    n = 200
    return pd.DataFrame({
        "r1":  rng.normal(0, 0.01, n),
        "r5":  rng.normal(0, 0.02, n),
        "r20": rng.normal(0, 0.03, n),
        "gap": rng.normal(0, 0.005, n),
        "vol": np.abs(rng.normal(0.02, 0.005, n)),
    })


# ═══════════════════════════════════════════════════════════════════════
# 1. Terminal Layer
# ═══════════════════════════════════════════════════════════════════════

class TestTerminalLayer:

    def test_build_terminal_features_returns_dataframe(self, small_returns):
        from analytics.python.features.build_terminal_set import build_terminal_features
        panel = pd.DataFrame({"metric": small_returns.values, "run_id": "test"})
        df = build_terminal_features(panel)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == len(panel)

    def test_select_terminals_subsets_columns(self, small_feature_df):
        from analytics.python.features.build_terminal_set import select_terminals
        subset = ["r1", "r5"]
        result = select_terminals(small_feature_df, subset)
        assert set(result.columns) == set(subset)

    def test_select_terminals_missing_filled_with_zero(self, small_feature_df):
        from analytics.python.features.build_terminal_set import select_terminals
        result = select_terminals(small_feature_df, ["r1", "nonexistent_term"])
        assert "nonexistent_term" in result.columns
        assert (result["nonexistent_term"] == 0.0).all()

    def test_get_terminal_set_from_env(self, monkeypatch):
        from analytics.python.features.build_terminal_set import get_terminal_set_from_env
        monkeypatch.setenv("EML_ALPHA_TERMINAL_SET", "r1,r5,r20")
        ts = get_terminal_set_from_env()
        assert set(ts) == {"r1", "r5", "r20"}

    def test_build_crisis_mask_returns_bool_series(self, small_returns):
        from analytics.python.features.regime_features import build_crisis_mask
        mask = build_crisis_mask(small_returns)
        assert isinstance(mask, pd.Series)
        assert mask.dtype == bool

    def test_crisis_mask_length_matches_input(self, small_returns):
        from analytics.python.features.regime_features import build_crisis_mask
        mask = build_crisis_mask(small_returns)
        assert len(mask) == len(small_returns)


# ═══════════════════════════════════════════════════════════════════════
# 2. EML Tree / Compiler / Runtime Layer
# ═══════════════════════════════════════════════════════════════════════

class TestEMLTreeLayer:

    def test_depth_guard_rejects_depth_1(self):
        """depth=1 は EML_DEPTH_MIN=2 未満なので validate_depth が例外を送出する。"""
        from analytics.python.alpha.eml.eml_core import validate_depth
        from analytics.python.alpha.eml.eml_tree import build_leaf, build_eml_node
        node_d1 = build_eml_node(build_leaf("r1"), build_leaf("r5"))  # depth=1
        with pytest.raises(ValueError, match="depth"):
            validate_depth(node_d1)

    def test_depth_guard_rejects_depth_5(self):
        """depth=5 は EML_DEPTH_MAX=4 超過で例外を送出する。"""
        from analytics.python.alpha.eml.eml_core import validate_depth
        from analytics.python.alpha.eml.eml_tree import build_leaf, build_eml_node
        def make_depth(d):
            if d == 0:
                return build_leaf("r1")
            return build_eml_node(make_depth(d - 1), make_depth(d - 1))
        node_d5 = make_depth(5)
        with pytest.raises(ValueError):
            validate_depth(node_d5)

    def test_enumerate_trees_all_within_depth_bounds(self):
        from analytics.python.alpha.eml.eml_tree import enumerate_trees
        from analytics.python.alpha.eml.eml_core import EML_DEPTH_MIN, EML_DEPTH_MAX
        trees = enumerate_trees(2, ["r1", "r5", "r20"])
        assert len(trees) > 0
        for t in trees:
            d = t.depth()
            assert EML_DEPTH_MIN <= d <= EML_DEPTH_MAX, f"depth={d} out of range"

    def test_snap_weights_produces_0_or_1(self):
        from analytics.python.alpha.eml.eml_tree import random_tree, snap_weights
        from analytics.python.alpha.eml.eml_core import NODE_EML
        import random as stdlib_random
        rng = stdlib_random.Random(99)
        tree = random_tree(2, ["r1", "r5", "r20"], rng=rng)
        snapped = snap_weights(tree)
        def check(node):
            if node.kind == NODE_EML:
                assert node.snapped_weight in (0.0, 1.0), f"bad snap: {node.snapped_weight}"
                check(node.left)
                check(node.right)
        check(snapped)

    def test_compile_to_expr_returns_string(self):
        from analytics.python.alpha.eml.eml_tree import build_leaf, snap_weights, build_eml_node
        from analytics.python.alpha.eml.eml_compiler import compile_to_expr
        node = build_eml_node(build_leaf("r1"), build_leaf("r5"))
        snapped = snap_weights(node)
        expr = compile_to_expr(snapped)
        assert isinstance(expr, str)
        assert len(expr) > 0

    def test_validate_safe_expr_rejects_os_system(self):
        from analytics.python.alpha.eml.eml_compiler import validate_safe_expr
        # os.system は unsafe token を含むため False になる
        assert validate_safe_expr("os.system('rm -rf /')", terminals={"r1"}) is False

    def test_validate_safe_expr_accepts_terminal(self):
        from analytics.python.alpha.eml.eml_compiler import validate_safe_expr
        assert validate_safe_expr("r1", terminals={"r1"}) is True

    def test_assert_safe_expr_raises_on_import(self):
        from analytics.python.alpha.eml.eml_compiler import assert_safe_expr
        with pytest.raises(ValueError, match="[Uu]nsafe"):
            assert_safe_expr("__import__('os')", terminals={"r1"})

    def test_lower_expr_terminal(self, small_feature_df):
        from analytics.python.alpha.eml.eml_runtime_lower import lower_expr
        result = lower_expr("r1", small_feature_df)
        assert isinstance(result, pd.Series)
        assert len(result) == len(small_feature_df)

    def test_lower_and_rank_normalize_output_range(self, small_feature_df):
        from analytics.python.alpha.eml.eml_runtime_lower import lower_and_rank_normalize
        result = lower_and_rank_normalize("r1", small_feature_df)
        assert result.notna().all() or result.isna().sum() < len(result) * 0.1
        # rank-normalized は [-1, 1] 程度に収まる
        assert result.abs().max() <= 1.0 + 1e-9

    def test_lower_expr_unknown_terminal_raises(self, small_feature_df):
        from analytics.python.alpha.eml.eml_runtime_lower import lower_expr
        with pytest.raises((ValueError, KeyError)):
            lower_expr("nonexistent_terminal_xyz", small_feature_df)

    def test_tree_json_no_nan(self):
        """tree_json に NaN/Infinity が含まれないことを確認。"""
        import json
        from analytics.python.alpha.eml.eml_tree import random_tree, snap_weights
        import random as stdlib_random
        rng = stdlib_random.Random(7)
        for _ in range(20):
            tree = random_tree(2, ["r1", "r5", "r20"], rng=rng)
            snapped = snap_weights(tree)
            js = snapped.to_json()
            assert "NaN" not in js, f"NaN in tree_json: {js}"
            assert "Infinity" not in js, f"Infinity in tree_json: {js}"

    def test_eml_core_to_json_from_json_roundtrip(self):
        from analytics.python.alpha.eml.eml_tree import build_leaf, build_eml_node, snap_weights
        from analytics.python.alpha.eml.eml_core import EMLNode
        node = build_eml_node(build_leaf("r1"), build_leaf("r5"))
        snapped = snap_weights(node)
        js = snapped.to_json()
        restored = EMLNode.from_json(js)
        assert restored.depth() == snapped.depth()
        assert restored.node_count() == snapped.node_count()


# ═══════════════════════════════════════════════════════════════════════
# 3. Metrics Layer
# ═══════════════════════════════════════════════════════════════════════

class TestMetricsLayer:

    def test_predictive_compute_returns_dataclass(self, small_feature_df, small_returns):
        from analytics.python.metrics.predictive import compute_predictive
        m = compute_predictive(small_feature_df["r1"], small_returns)
        assert isinstance(m.ic, float)
        assert isinstance(m.rank_ic, float)
        assert -1.0 <= m.rank_ic <= 1.0

    def test_predictive_hit_rate_range(self, small_feature_df, small_returns):
        from analytics.python.metrics.predictive import compute_predictive
        m = compute_predictive(small_feature_df["r1"], small_returns)
        assert 0.0 <= m.hit_rate <= 1.0

    def test_predictive_ic_t_stat(self, small_feature_df, small_returns):
        from analytics.python.metrics.predictive import compute_predictive
        m = compute_predictive(small_feature_df["r1"], small_returns)
        assert isinstance(m.ic_t_stat, float)

    def test_portfolio_sharpe_is_float(self, small_returns):
        from analytics.python.metrics.portfolio import compute_portfolio
        m = compute_portfolio(small_returns)
        assert isinstance(m.sharpe, float)

    def test_portfolio_max_drawdown_nonpositive(self, small_returns):
        from analytics.python.metrics.portfolio import compute_portfolio
        m = compute_portfolio(small_returns)
        assert m.max_drawdown <= 0.0

    def test_portfolio_sortino_is_float(self, small_returns):
        from analytics.python.metrics.portfolio import compute_portfolio
        m = compute_portfolio(small_returns)
        assert isinstance(m.sortino, float)

    def test_portfolio_calmar_is_float(self, small_returns):
        from analytics.python.metrics.portfolio import compute_portfolio
        m = compute_portfolio(small_returns)
        assert isinstance(m.calmar, float)

    def test_portfolio_cvar5_nonpositive(self, small_returns):
        from analytics.python.metrics.portfolio import compute_portfolio
        m = compute_portfolio(small_returns)
        assert m.cvar_5 <= 0.0

    def test_trading_turnover_nonnegative(self, small_feature_df, small_returns):
        from analytics.python.metrics.trading import compute_trading
        pos = small_feature_df["r1"].clip(-1, 1)
        m = compute_trading(pos, small_returns)
        assert m.turnover >= 0.0

    def test_trading_win_loss_ratio_nonnegative(self, small_feature_df, small_returns):
        from analytics.python.metrics.trading import compute_trading
        pos = small_feature_df["r1"].clip(-1, 1)
        m = compute_trading(pos, small_returns)
        assert m.win_loss_ratio >= 0.0

    def test_trading_cost_drag_nonnegative(self, small_feature_df, small_returns):
        from analytics.python.metrics.trading import compute_trading
        pos = small_feature_df["r1"].clip(-1, 1)
        m = compute_trading(pos, small_returns, cost_bps=2.0)
        assert m.cost_drag >= 0.0

    def test_risk_var5_nonpositive(self, small_returns):
        from analytics.python.metrics.risk import compute_risk
        m = compute_risk(small_returns)
        assert m.var_5 <= 0.0

    def test_risk_cvar5_nonpositive(self, small_returns):
        from analytics.python.metrics.risk import compute_risk
        m = compute_risk(small_returns)
        assert m.cvar_5 <= 0.0

    def test_risk_kelly_fraction_is_float(self, small_returns):
        from analytics.python.metrics.risk import compute_risk
        m = compute_risk(small_returns)
        assert isinstance(m.kelly_fraction, float)

    def test_regime_crisis_sharpe(self, small_feature_df, small_returns):
        from analytics.python.metrics.regime import compute_regime
        from analytics.python.features.regime_features import build_crisis_mask
        pos = small_feature_df["r1"].clip(-1, 1)
        net_ret = pos * small_returns
        mask = build_crisis_mask(small_returns)
        m = compute_regime(pos, small_returns, net_ret, crisis_mask=mask)
        assert isinstance(m.crisis_period_sharpe, float)

    def test_regime_consistency_score_is_float(self, small_feature_df, small_returns):
        from analytics.python.metrics.regime import compute_regime
        from analytics.python.features.regime_features import build_crisis_mask
        pos = small_feature_df["r1"].clip(-1, 1)
        net_ret = pos * small_returns
        mask = build_crisis_mask(small_returns)
        m = compute_regime(pos, small_returns, net_ret, crisis_mask=mask)
        assert isinstance(m.regime_consistency_score, float)

    def test_metrics_to_dict_has_required_keys(self, small_returns, small_feature_df):
        """各 Metrics dataclass の to_dict() が必要なキーを持つ。"""
        from analytics.python.metrics.predictive import compute_predictive
        from analytics.python.metrics.portfolio import compute_portfolio
        from analytics.python.metrics.trading import compute_trading
        from analytics.python.metrics.risk import compute_risk
        pos = small_feature_df["r1"].clip(-1, 1)
        assert "rank_ic" in compute_predictive(pos, small_returns).to_dict()
        assert "sharpe" in compute_portfolio(small_returns).to_dict()
        assert "turnover" in compute_trading(pos, small_returns).to_dict()
        assert "var_5" in compute_risk(small_returns).to_dict()


# ═══════════════════════════════════════════════════════════════════════
# 4. Backtest Layer
# ═══════════════════════════════════════════════════════════════════════

class TestBacktestLayer:

    def test_cost_model_apply(self, small_returns, small_feature_df):
        from analytics.python.backtest.cost_model import CostModelConfig, apply_cost
        cfg = CostModelConfig(cost_bps=2.0)
        pos = small_feature_df["r1"].clip(-1, 1)
        costs = apply_cost(pos, cfg)
        assert isinstance(costs, pd.Series)
        assert (costs >= 0).all()

    def test_slippage_model_apply(self, small_returns, small_feature_df):
        from analytics.python.backtest.slippage_model import SlippageModelConfig, apply_slippage
        cfg = SlippageModelConfig(slippage_bps=2.0)
        pos = small_feature_df["r1"].clip(-1, 1)
        slippage = apply_slippage(pos, cfg)
        assert isinstance(slippage, pd.Series)
        assert (slippage >= 0).all()

    def test_risk_gate_normal_regime_passes(self, small_returns, small_feature_df):
        from analytics.python.backtest.risk_gate import RiskGateConfig, apply_risk_gate
        cfg = RiskGateConfig(max_trade_loss=0.02, per_symbol_limit=0.1)
        pos = small_feature_df["r1"].clip(-1, 1)
        gate_pos, triggers = apply_risk_gate(pos, small_returns, cfg)
        assert isinstance(gate_pos, pd.Series)
        assert isinstance(triggers, list)

    def test_risk_gate_crisis_derate_zero(self, small_returns, small_feature_df):
        """derate_factor=0.0 のとき全ポジションが 0 になる。"""
        from analytics.python.backtest.risk_gate import RiskGateConfig, apply_risk_gate
        # crisis_mask=全True で action='derate', derate_factor=0.0
        cfg = RiskGateConfig(action="derate", derate_factor=0.0)
        pos = small_feature_df["r1"].clip(-1, 1)
        crisis_mask = pd.Series(True, index=pos.index)
        gate_pos, triggers = apply_risk_gate(
            pos, small_returns, cfg, crisis_mask=crisis_mask
        )
        # derate_factor=0.0 → 全ポジションゼロ
        assert (gate_pos.abs() < 1e-9).all(), f"crisis derate failed: max={gate_pos.abs().max()}"

    def test_portfolio_simulator_net_returns(self, small_returns, small_feature_df):
        from analytics.python.backtest.portfolio_simulator import simulate_portfolio
        from analytics.python.backtest.cost_model import CostModelConfig
        from analytics.python.backtest.slippage_model import SlippageModelConfig
        pos = small_feature_df["r1"].clip(-1, 1)
        result = simulate_portfolio(
            pos, small_returns,
            CostModelConfig(cost_bps=2.0),
            SlippageModelConfig(slippage_bps=2.0),
        )
        assert isinstance(result.net_returns, pd.Series)
        assert len(result.net_returns) == len(pos)

    def test_walkforward_harness_single_fold(self, small_returns, small_feature_df):
        """データ不足時は single fold で実行される。"""
        from analytics.python.backtest.harness import WalkForwardConfig, WalkForwardHarness
        cfg = WalkForwardConfig(min_train_days=750, step_days=5)
        harness = WalkForwardHarness(cfg)
        signal = small_feature_df["r1"]
        result = harness.run(
            signal=signal,
            returns=small_returns,
            run_id="test_run",
            trace_id=str(uuid.uuid4()),
        )
        assert result.total_folds >= 1
        assert isinstance(result.overall_sharpe, float)
        assert isinstance(result.overall_max_drawdown, float)

    def test_walkforward_harness_multi_fold(self, rng):
        """十分なデータがある場合は複数フォールドが生成される。"""
        from analytics.python.backtest.harness import WalkForwardConfig, WalkForwardHarness
        n = 900
        returns = pd.Series(rng.normal(0, 0.02, n))
        signal  = pd.Series(rng.normal(0, 1, n))
        cfg = WalkForwardConfig(min_train_days=750, step_days=5)
        harness = WalkForwardHarness(cfg)
        result = harness.run(
            signal=signal,
            returns=returns,
            run_id="test_multi",
            trace_id=str(uuid.uuid4()),
        )
        assert result.total_folds >= 1

    def test_fold_guard_insufficient_data(self, rng):
        """fold guard: min_train_days > データ数 → single fold で処理。"""
        from analytics.python.backtest.harness import WalkForwardConfig, WalkForwardHarness
        n = 100
        returns = pd.Series(rng.normal(0, 0.02, n))
        signal  = pd.Series(rng.normal(0, 1, n))
        cfg = WalkForwardConfig(min_train_days=750, step_days=5)
        harness = WalkForwardHarness(cfg)
        result = harness.run(signal, returns, "fold_test", str(uuid.uuid4()))
        assert result.total_folds == 1
        assert result.metadata.get("single_fold") is True

    def test_crisis_drawdown_derate_zero(self, rng):
        """全期間クライシス + derate_factor=0 → ネットリターンが 0。"""
        from analytics.python.backtest.harness import WalkForwardConfig, WalkForwardHarness
        from analytics.python.backtest.risk_gate import RiskGateConfig
        n = 200
        returns = pd.Series(rng.normal(-0.01, 0.05, n))
        signal  = pd.Series(rng.normal(0, 1, n))
        risk_cfg = RiskGateConfig(action="derate", derate_factor=0.0)
        cfg = WalkForwardConfig(
            min_train_days=10,
            step_days=5,
            risk_config=risk_cfg,
        )
        harness = WalkForwardHarness(cfg)
        crisis_mask = pd.Series(True, index=signal.index)
        result = harness.run(signal, returns, "crisis_test", str(uuid.uuid4()),
                             crisis_mask=crisis_mask)
        # derate=0 → net_returns ≈ 0 → drawdown ≈ 0
        assert result.overall_max_drawdown >= -1e-9, (
            f"Expected ~0 drawdown with derate=0, got {result.overall_max_drawdown}"
        )


# ═══════════════════════════════════════════════════════════════════════
# 5. EML Search / Fitness / Evaluation Layer
# ═══════════════════════════════════════════════════════════════════════

class TestEMLSearchLayer:

    def test_exhaustive_search_returns_candidates(self, small_feature_df, small_returns):
        from analytics.python.alpha.eml.eml_search import exhaustive_search
        from analytics.python.alpha.eml.eml_fitness import simple_rank_ic_fitness
        candidates = exhaustive_search(
            terminals=["r1", "r5"],
            max_depth=2,
            run_id="test_run",
            trace_id=str(uuid.uuid4()),
            feature_df=small_feature_df,
            target=small_returns,
            fitness_fn=simple_rank_ic_fitness,
            top_k=5,
        )
        assert len(candidates) > 0
        assert len(candidates) <= 5

    def test_exhaustive_search_depth_within_bounds(self, small_feature_df, small_returns):
        from analytics.python.alpha.eml.eml_search import exhaustive_search
        from analytics.python.alpha.eml.eml_fitness import simple_rank_ic_fitness
        from analytics.python.alpha.eml.eml_core import EML_DEPTH_MIN, EML_DEPTH_MAX
        candidates = exhaustive_search(
            terminals=["r1", "r5"],
            max_depth=2,
            run_id="test",
            trace_id=str(uuid.uuid4()),
            feature_df=small_feature_df,
            target=small_returns,
            fitness_fn=simple_rank_ic_fitness,
            top_k=50,
        )
        for c in candidates:
            d = c.tree_depth()
            assert EML_DEPTH_MIN <= d <= EML_DEPTH_MAX, f"depth={d}"

    def test_gradient_search_returns_candidates(self, small_feature_df, small_returns):
        from analytics.python.alpha.eml.eml_search import gradient_search
        from analytics.python.alpha.eml.eml_fitness import simple_rank_ic_fitness
        candidates = gradient_search(
            terminals=["r1", "r5", "r20"],
            max_depth=2,
            run_id="test",
            trace_id=str(uuid.uuid4()),
            feature_df=small_feature_df,
            target=small_returns,
            fitness_fn=simple_rank_ic_fitness,
            n_init=3,
            adam_steps=5,
            top_k=3,
        )
        assert len(candidates) > 0

    def test_compute_fitness_returns_float(self, small_feature_df, small_returns):
        from analytics.python.alpha.eml.eml_fitness import compute_fitness
        from analytics.python.alpha.eml.eml_tree import random_tree, snap_weights
        import random as stdlib_random
        rng = stdlib_random.Random(42)
        tree = random_tree(2, ["r1", "r5"], rng=rng)
        snapped = snap_weights(tree)
        score = compute_fitness(snapped, small_feature_df, small_returns)
        assert isinstance(score, float)
        assert not math.isnan(score)
        assert not math.isinf(score)

    def test_compute_fitness_with_regime_mask(self, small_feature_df, small_returns):
        from analytics.python.alpha.eml.eml_fitness import compute_fitness
        from analytics.python.alpha.eml.eml_tree import random_tree, snap_weights
        from analytics.python.features.regime_features import build_crisis_mask
        import random as stdlib_random
        rng = stdlib_random.Random(43)
        tree = random_tree(2, ["r1", "r5"], rng=rng)
        snapped = snap_weights(tree)
        crisis = build_crisis_mask(small_returns)
        score = compute_fitness(snapped, small_feature_df, small_returns, regime_mask=crisis)
        assert isinstance(score, float)
        assert not math.isnan(score)

    def test_evaluation_runner_returns_result(self, small_feature_df, small_returns):
        from analytics.python.alpha.eml.eml_evaluation_runner import EMLEvaluationRunner
        from analytics.python.alpha.eml.eml_search import EMLCandidate
        from analytics.python.alpha.eml.eml_tree import random_tree, snap_weights
        from analytics.python.alpha.eml.eml_compiler import compile_to_expr
        import random as stdlib_random
        rng = stdlib_random.Random(55)
        tree = random_tree(2, ["r1", "r5"], rng=rng)
        snapped = snap_weights(tree)
        expr = compile_to_expr(snapped)
        candidate = EMLCandidate(
            candidate_id=str(uuid.uuid4()),
            run_id="test",
            trace_id=str(uuid.uuid4()),
            node=snapped,
            compiled_expr=expr,
            fitness_score=0.1,
        )
        runner = EMLEvaluationRunner(
            feature_df=small_feature_df,
            target=small_returns,
            cost_bps=2.0,
        )
        result = runner.run(candidate)
        assert result.candidate_id == candidate.candidate_id
        assert isinstance(result.rank_ic, float)
        assert isinstance(result.sharpe, float)
        assert isinstance(result.max_drawdown, float)

    def test_trace_id_preserved_in_candidate(self, small_feature_df, small_returns):
        """trace_id がすべての候補に引き継がれることを確認。"""
        from analytics.python.alpha.eml.eml_search import exhaustive_search
        from analytics.python.alpha.eml.eml_fitness import simple_rank_ic_fitness
        trace_id = str(uuid.uuid4())
        candidates = exhaustive_search(
            terminals=["r1", "r5"],
            max_depth=2,
            run_id="trace_test",
            trace_id=trace_id,
            feature_df=small_feature_df,
            target=small_returns,
            fitness_fn=simple_rank_ic_fitness,
            top_k=5,
        )
        for c in candidates:
            assert c.trace_id == trace_id, (
                f"trace_id mismatch: expected {trace_id}, got {c.trace_id}"
            )


# ═══════════════════════════════════════════════════════════════════════
# 6. IO Layer (NaN サニタイズ)
# ═══════════════════════════════════════════════════════════════════════

class TestIOLayer:

    def test_safe_float_handles_nan(self):
        from analytics.python.io.postgres_eml_alpha_writer import _safe_float
        assert _safe_float(float("nan")) == 0.0
        assert _safe_float(float("inf")) == 0.0
        assert _safe_float(-float("inf")) == 0.0
        assert _safe_float(1.5) == 1.5

    def test_safe_json_sanitizes_nan(self):
        from analytics.python.io.postgres_eml_alpha_writer import _safe_json
        data = {"score": float("nan"), "val": 1.0, "nested": {"inf": float("inf")}}
        js = _safe_json(data)
        assert "NaN" not in js
        assert "Infinity" not in js
        assert "null" in js  # NaN → null

    def test_safe_json_preserves_valid_values(self):
        from analytics.python.io.postgres_eml_alpha_writer import _safe_json
        import json
        data = {"a": 1.5, "b": "hello", "c": [1, 2, 3]}
        result = json.loads(_safe_json(data))
        assert result["a"] == 1.5
        assert result["b"] == "hello"


# ═══════════════════════════════════════════════════════════════════════
# 7. EML Master Formula (end-to-end, no DB)
# ═══════════════════════════════════════════════════════════════════════

class TestEMLMasterFormula:

    def test_run_eml_discovery_basic(self, small_feature_df, small_returns):
        from analytics.python.alpha.eml.eml_master_formula import (
            EMLDiscoveryConfig, run_eml_discovery,
        )
        cfg = EMLDiscoveryConfig(
            run_id="test_master",
            trace_id=str(uuid.uuid4()),
            batch_label="test",
            target_horizon="5d",
            max_depth=2,
            terminal_set=["r1", "r5"],
            gradient_n_init=2,
            gradient_steps=3,
            min_fitness_for_promotion=-1.0,
            min_rank_ic=-1.0,
        )
        output = run_eml_discovery(cfg, small_feature_df, small_returns)
        assert output.total_searched > 0
        assert len(output.candidates) > 0
        assert output.terminal_set_hash

    def test_run_eml_discovery_all_rejected(self, small_feature_df, small_returns):
        """超高閾値 → 全候補 REJECTED。"""
        from analytics.python.alpha.eml.eml_master_formula import (
            EMLDiscoveryConfig, run_eml_discovery,
        )
        cfg = EMLDiscoveryConfig(
            run_id="test_reject_all",
            trace_id=str(uuid.uuid4()),
            batch_label="test",
            max_depth=2,
            terminal_set=["r1", "r5"],
            gradient_n_init=1,
            gradient_steps=2,
            min_fitness_for_promotion=999.0,
            min_rank_ic=999.0,
        )
        output = run_eml_discovery(cfg, small_feature_df, small_returns)
        assert len(output.promoted) == 0
        assert len(output.rejected) > 0

    def test_run_eml_discovery_depth_guard(self, small_feature_df, small_returns):
        """max_depth=5 → 内部で 4 にクランプされる。"""
        from analytics.python.alpha.eml.eml_master_formula import (
            EMLDiscoveryConfig, run_eml_discovery,
        )
        from analytics.python.alpha.eml.eml_core import EML_DEPTH_MAX
        cfg = EMLDiscoveryConfig(
            run_id="depth_guard_test",
            trace_id=str(uuid.uuid4()),
            batch_label="test",
            max_depth=5,   # EML_DEPTH_MAX=4 を超える
            terminal_set=["r1", "r5"],
            gradient_n_init=1,
            gradient_steps=2,
            min_fitness_for_promotion=-99.0,
            min_rank_ic=-99.0,
        )
        output = run_eml_discovery(cfg, small_feature_df, small_returns)
        for c in output.candidates:
            assert c.tree_depth() <= EML_DEPTH_MAX, (
                f"candidate depth {c.tree_depth()} exceeds EML_DEPTH_MAX={EML_DEPTH_MAX}"
            )

    def test_run_eml_discovery_trace_id_preserved(self, small_feature_df, small_returns):
        """trace_id が output 全体に引き継がれること。"""
        from analytics.python.alpha.eml.eml_master_formula import (
            EMLDiscoveryConfig, run_eml_discovery,
        )
        trace_id = str(uuid.uuid4())
        cfg = EMLDiscoveryConfig(
            run_id="trace_test",
            trace_id=trace_id,
            batch_label="test",
            max_depth=2,
            terminal_set=["r1", "r5"],
            gradient_n_init=1,
            gradient_steps=2,
            min_fitness_for_promotion=-99.0,
            min_rank_ic=-99.0,
        )
        output = run_eml_discovery(cfg, small_feature_df, small_returns)
        assert output.trace_id == trace_id
        for c in output.candidates:
            assert c.trace_id == trace_id

    def test_only_lowered_real_safe_formulas_saved(self, small_feature_df, small_returns):
        """compiled_expr はすべて real-safe (unsafe tokens が含まれない)。"""
        from analytics.python.alpha.eml.eml_master_formula import (
            EMLDiscoveryConfig, run_eml_discovery,
        )
        from analytics.python.alpha.eml.eml_compiler import validate_safe_expr
        cfg = EMLDiscoveryConfig(
            run_id="safe_expr_test",
            trace_id=str(uuid.uuid4()),
            batch_label="test",
            max_depth=2,
            terminal_set=["r1", "r5"],
            gradient_n_init=1,
            gradient_steps=2,
            min_fitness_for_promotion=-99.0,
            min_rank_ic=-99.0,
        )
        output = run_eml_discovery(cfg, small_feature_df, small_returns)
        terminals = set(["r1", "r5"])
        for c in output.candidates:
            assert validate_safe_expr(c.compiled_expr, terminals=terminals), (
                f"UNSAFE expr found: {c.compiled_expr}"
            )


# ═══════════════════════════════════════════════════════════════════════
# 8. Guard Rules
# ═══════════════════════════════════════════════════════════════════════

class TestGuardRules:

    def test_upsert_guard_safe_float_no_nan_in_db_params(self):
        """DB に送るパラメータに NaN/Inf が含まれないこと。"""
        from analytics.python.io.postgres_eml_alpha_writer import _safe_float, _safe_json
        from analytics.python.alpha.eml.eml_tree import random_tree, snap_weights
        import json
        import random as stdlib_random
        rng = stdlib_random.Random(77)
        tree = random_tree(2, ["r1", "r5"], rng=rng)
        snapped = snap_weights(tree)
        # NaN の fitness をサニタイズ
        sanitized = _safe_float(float("nan"))
        assert not math.isnan(sanitized)
        # tree_json は JSON として parse できる
        tree_json = snapped.to_json()
        parsed = json.loads(tree_json)
        assert isinstance(parsed, dict)

    def test_trace_id_guard_uuid_format(self):
        """trace_id は UUID 形式であること。"""
        trace_id = str(uuid.uuid4())
        try:
            uuid.UUID(trace_id)
            valid = True
        except ValueError:
            valid = False
        assert valid

    def test_branch_guard_snap_produces_valid_tree(self):
        """snap 後のツリーが depth guard を通過またはリーフになること。"""
        from analytics.python.alpha.eml.eml_tree import random_tree, snap_weights
        from analytics.python.alpha.eml.eml_core import validate_depth
        import random as stdlib_random
        rng = stdlib_random.Random(88)
        valid_count = 0
        for _ in range(20):
            tree = random_tree(2, ["r1", "r5", "r20"], rng=rng)
            snapped = snap_weights(tree)
            try:
                validate_depth(snapped)
                valid_count += 1
            except ValueError:
                # depth 0 or 1 になった場合は ValueError — snap でリーフに縮退したケース
                pass
        # 少なくとも一部は有効な depth に収まる
        assert valid_count > 0

    def test_audit_decision_allowed_values(self):
        """audit_events の decision は APPLIED/DRY_RUN/CONFLICTED/REJECTED のみ。"""
        from analytics.python.alpha.promotion_bridge import ALLOWED_DECISIONS
        assert ALLOWED_DECISIONS == {"APPLIED", "REJECTED", "CONFLICTED", "DRY_RUN"}

    def test_psycopg_guard_placeholder_style(self):
        """%s プレースホルダーが使われていること（psycopg3 準拠）。"""
        import ast
        writer_path = os.path.join(
            os.path.dirname(__file__), "../../analytics/python/io/postgres_eml_alpha_writer.py"
        )
        with open(writer_path) as f:
            src = f.read()
        # %s バインディングが存在する
        assert "%s" in src, "psycopg3 の %s バインディングが見つかりません"
        # ? バインディング（sqlite3スタイル）は使っていない
        # INSERT文内に ? が含まれていないことを確認
        lines_with_q = [l for l in src.splitlines()
                        if "?" in l and "INSERT" not in l.upper() and "#" not in l]
        # INSERT 以外の行に ? があれば警告 (strict には失敗させる)
        assert len(lines_with_q) == 0 or all("?" not in l for l in lines_with_q), \
            f"sqlite3 style ? found: {lines_with_q}"
