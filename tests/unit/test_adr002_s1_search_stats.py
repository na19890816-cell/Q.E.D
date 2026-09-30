"""
test_adr002_s1_search_stats.py
------------------------------
ADR-002 S1: 探索側の試行数記録 (eml_search.SearchStats / eml_master_formula / eml_lineage)

最重要の不変条件:
  stats_out を渡しても渡さなくても、探索結果 (式・スコア・順序) は完全に同一
  → golden の決定は変わらない
"""
from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from analytics.python.alpha.eml.eml_fitness import simple_rank_ic_fitness
from analytics.python.alpha.eml.eml_lineage import (
    eml_family_key,
    trial_batches_from_eml_output,
)
from analytics.python.alpha.eml.eml_master_formula import (
    EMLDiscoveryConfig,
    EMLDiscoveryOutput,
    run_eml_discovery,
)
from analytics.python.alpha.eml.eml_search import (
    SearchStats,
    exhaustive_search,
    gradient_search,
)
from analytics.python.alpha.eml.eml_tree import enumerate_trees
from analytics.python.frost.frost_dsr import DsrGate, N_TRIALS_SOURCE_PROVIDED
from analytics.python.frost.frost_lineage import TrialLedger, make_batch_id, make_family_key
from analytics.python.pg_io.postgres_lineage_bridge import ledger_tables_exist

pytestmark = pytest.mark.adr002_lineage

TERMINALS = ["r1", "r5", "r20"]


@pytest.fixture(scope="module")
def df():
    rng = np.random.default_rng(42)
    return pd.DataFrame({t: rng.normal(0, 1, 200) for t in TERMINALS})


@pytest.fixture(scope="module")
def target(df):
    return df["r1"] + np.random.default_rng(99).normal(0, 0.5, len(df))


def _sig(cands):
    # NaN fitness は repr で比較 (NaN != NaN のため)
    return [(c.compiled_expr, repr(round(c.fitness_score, 12))) for c in cands]


# ===========================================================================
# exhaustive_search
# ===========================================================================

class TestExhaustiveStats:
    def _run(self, df, target, top_k=5, stats=None):
        return exhaustive_search(TERMINALS, 2, "run", "tr", df, target,
                                 simple_rank_ic_fitness, top_k=top_k, stats_out=stats)

    def test_results_unchanged(self, df, target):
        """golden 非影響: stats_out の有無で結果が完全一致"""
        a = self._run(df, target)
        stats = []
        b = self._run(df, target, stats=stats)
        assert _sig(a) == _sig(b)

    def test_default_no_stats(self, df, target):
        assert isinstance(self._run(df, target), list)

    def test_counts_before_top_k(self, df, target):
        """L1: n_evaluated は top_k ではなく列挙した全木数"""
        stats = []
        res = self._run(df, target, top_k=3, stats=stats)
        (st,) = stats
        n_trees = len(enumerate_trees(2, TERMINALS))
        assert st.stage == "exhaustive"
        assert st.n_generated == n_trees
        assert st.n_evaluated + st.n_invalid == n_trees
        assert st.n_returned == len(res) == 3
        assert st.n_evaluated > st.n_returned
        assert 1 <= st.n_distinct_expr <= st.n_evaluated

    def test_fitness_moments(self, df, target):
        stats = []
        exhaustive_search(TERMINALS, 2, "run", "tr", df, target,
                          simple_rank_ic_fitness, top_k=10_000, stats_out=stats)
        st = stats[0]
        assert st.fitness_count <= st.n_evaluated
        assert st.fitness_count == st.n_evaluated - st.n_fitness_failed - st.n_fitness_nonfinite
        assert st.fitness_m2 >= 0.0 and math.isfinite(st.fitness_mean)

    def test_failures_counted(self, df, target):
        def boom(node, f, t):
            raise RuntimeError("x")
        stats = []
        res = exhaustive_search(["r1", "r5"], 2, "run", "tr", df, target, boom, top_k=2, stats_out=stats)
        st = stats[0]
        assert st.n_fitness_failed == st.n_evaluated > 0
        assert st.fitness_count == 0
        assert all(c.fitness_score == -999.0 for c in res)

    def test_appends(self, df, target):
        stats = [SearchStats("gradient", 1, 1, 0, 0, 1)]
        self._run(df, target, stats=stats)
        assert len(stats) == 2 and stats[1].stage == "exhaustive"


# ===========================================================================
# gradient_search
# ===========================================================================

class TestGradientStats:
    def _run(self, df, target, stats=None, seed=42):
        return gradient_search(TERMINALS, 2, "run", "tr", df, target, simple_rank_ic_fitness,
                               n_init=3, adam_steps=3, top_k=2, rng_seed=seed, stats_out=stats)

    def test_results_unchanged(self, df, target):
        a = self._run(df, target)
        b = self._run(df, target, stats=[])
        assert _sig(a) == _sig(b)

    def test_counts(self, df, target):
        stats = []
        res = self._run(df, target, stats=stats)
        (st,) = stats
        assert st.stage == "gradient"
        assert st.n_generated == 3
        assert st.n_evaluated + st.n_invalid == 3
        assert st.n_returned == len(res) <= 2

    def test_fitness_evals_includes_finite_difference(self, df, target):
        stats = []
        self._run(df, target, stats=stats)
        st = stats[0]
        assert st.fitness_evals >= st.n_evaluated

    def test_to_dict(self):
        d = SearchStats("gradient", 3, 3, 0, 0, 2, fitness_evals=10).to_dict()
        assert d["stage"] == "gradient" and d["fitness_evals"] == 10 and "fitness_kind" in d
        assert d["n_trials"] == 3  # n_distinct_expr 未設定 → n_evaluated

    def test_n_trials_prefers_distinct(self):
        assert SearchStats("exhaustive", 875, 875, 0, 0, 20, n_distinct_expr=5).n_trials == 5

    def test_exhaustive_degeneracy_documented(self, df, target):
        """
        EML セレクタの初期重み (raw_weight=0 → 左選択) により exhaustive の木は
        「端子 or 定数」に縮退する。N = 木の数 だと数百倍の過大計上になる。
        """
        stats = []
        exhaustive_search(TERMINALS, 2, "run", "tr", df, target,
                          simple_rank_ic_fitness, top_k=5, stats_out=stats)
        st = stats[0]
        assert st.n_distinct_expr <= len(TERMINALS) + 1
        assert st.n_evaluated >= 10 * st.n_distinct_expr


# ===========================================================================
# run_eml_discovery
# ===========================================================================

@pytest.fixture(scope="module")
def discovery(df, target):
    cfg = EMLDiscoveryConfig(
        run_id="run-s1", trace_id="trace-s1", terminal_set=TERMINALS,
        max_depth=2, gradient_n_init=2, gradient_steps=2,
        top_k_exhaustive=4, top_k_gradient=2, rng_seed=7,
        universe="U", target_name="T", target_horizon="5d",
    )
    return run_eml_discovery(cfg, df, target)


class TestDiscoveryOutput:
    def test_search_stats_present(self, discovery):
        assert [s.stage for s in discovery.search_stats] == ["exhaustive", "gradient"]

    def test_n_trials_exceeds_total_searched(self, discovery):
        """total_searched (top_k 合計) は試行数ではない — ADR-002 L1 の実証"""
        assert discovery.total_searched == sum(s.n_returned for s in discovery.search_stats)
        assert discovery.n_trials_evaluated > discovery.total_searched

    def test_n_trials_is_distinct(self, discovery):
        assert discovery.n_trials == sum(s.n_distinct_expr for s in discovery.search_stats)
        assert discovery.n_trials <= discovery.n_trials_evaluated

    def test_family_fields(self, discovery):
        assert (discovery.universe, discovery.target_name, discovery.target_horizon) == ("U", "T", "5d")

    def test_backward_compatible_construction(self):
        """既存コードの位置引数/キーワード構築 (新フィールドなし) が壊れない"""
        o = EMLDiscoveryOutput("r", "t", "b", [], [], [], [], 0, "h")
        assert o.search_stats == [] and o.n_trials_evaluated == 0

    def test_config_env_defaults(self, monkeypatch):
        monkeypatch.setenv("EML_UNIVERSE", "TOPIX500")
        monkeypatch.setenv("EML_TARGET_NAME", "fwd_ret")
        c = EMLDiscoveryConfig()
        assert c.universe == "TOPIX500" and c.target_name == "fwd_ret"


# ===========================================================================
# eml_lineage
# ===========================================================================

class TestEmlLineage:
    def test_family_key(self, discovery):
        assert eml_family_key(discovery) == make_family_key("5d", "U", "T", discovery.terminal_set_hash)

    def test_batches(self, discovery):
        bs = trial_batches_from_eml_output(discovery)
        assert [b.stage for b in bs] == ["exhaustive", "gradient"]
        assert sum(b.n_trials for b in bs) == discovery.n_trials
        assert bs[0].batch_id == make_batch_id("run-s1", "exhaustive", 0)
        assert all(b.run_id == "run-s1" and b.trace_id == "trace-s1" for b in bs)

    def test_sr_stats_intentionally_empty(self, discovery):
        """生存者 Sharpe は選択バイアスで分散過小 → V[SR] に使わない"""
        for b in trial_batches_from_eml_output(discovery):
            assert b.sr_stats.count == 0
            assert b.metadata["survivor_sharpe_daily"]["note"]

    def test_metadata(self, discovery):
        b = trial_batches_from_eml_output(discovery)[0]
        assert b.metadata["search_stats"]["n_trials"] == b.n_trials
        assert b.metadata["search_stats"]["n_evaluated"] >= b.n_trials
        assert b.family_spec["universe"] == "U"

    def test_deterministic_ids(self, discovery):
        a = [b.batch_id for b in trial_batches_from_eml_output(discovery)]
        b = [b.batch_id for b in trial_batches_from_eml_output(discovery)]
        assert a == b

    def test_multiple_same_stage_seq(self):
        out = SimpleNamespace(
            run_id="r", trace_id="t", batch_label="b", candidates=[], eval_results=[],
            terminal_set_hash="h", target_horizon="5d", universe="U", target_name="T",
            search_stats=[SearchStats("gradient", 2, 2, 0, 0, 1), SearchStats("gradient", 3, 3, 0, 0, 1)],
        )
        bs = trial_batches_from_eml_output(out)
        assert len({b.batch_id for b in bs}) == 2
        assert bs[1].batch_id == make_batch_id("r", "gradient", 1)

    def test_survivor_sharpe_daily(self):
        c = SimpleNamespace(candidate_id="c1", metadata={"search_mode": "exhaustive"})
        ev = SimpleNamespace(candidate_id="c1", sharpe=math.sqrt(252) * 0.1)
        out = SimpleNamespace(
            run_id="r", trace_id="t", batch_label="b", candidates=[c], eval_results=[ev],
            terminal_set_hash="h", target_horizon="5d", universe="U", target_name="T",
            search_stats=[SearchStats("exhaustive", 5, 5, 0, 0, 1)],
        )
        m = trial_batches_from_eml_output(out)[0].metadata["survivor_sharpe_daily"]
        assert m["count"] == 1 and m["values"][0] == pytest.approx(0.1)

    def test_end_to_end_dsr(self, discovery):
        """探索 → 台帳 → snapshot → DsrGate が provided の N で動く"""
        led = TrialLedger(batches=trial_batches_from_eml_output(discovery))
        snap = led.snapshot(eml_family_key(discovery))
        assert snap.n_trials == discovery.n_trials
        assert "sr_variance" not in snap.to_dsr_kwargs()
        rets = list(np.random.default_rng(1).normal(0.002, 0.01, 300))
        r = DsrGate().check(rets, **snap.to_dsr_kwargs())
        assert r.n_trials == discovery.n_trials
        assert r.n_trials_source == N_TRIALS_SOURCE_PROVIDED
        assert r.sr_variance_source == "estimator"


# ===========================================================================
# ledger_tables_exist / pipeline helper
# ===========================================================================

def _conn(row):
    cur = MagicMock()
    cur.fetchone.return_value = row
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=cur)
    ctx.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = ctx
    return conn, cur


class TestLedgerTablesExist:
    def test_true(self):
        assert ledger_tables_exist(_conn((True,))[0]) is True

    def test_false(self):
        assert ledger_tables_exist(_conn((False,))[0]) is False
        assert ledger_tables_exist(_conn(None)[0]) is False


class TestPipelineRecordTrialLedger:
    @pytest.fixture
    def mod(self):
        import importlib
        return importlib.import_module("scripts.postgres.run_eml_pipeline")

    def test_disabled(self, mod, monkeypatch, discovery):
        monkeypatch.setenv("EML_LINEAGE_ENABLED", "0")
        conn, cur = _conn((True,))
        assert mod._record_trial_ledger(conn, discovery) == {"status": "disabled"}
        cur.execute.assert_not_called()

    def test_skip_no_table(self, mod, monkeypatch, discovery):
        monkeypatch.delenv("EML_LINEAGE_ENABLED", raising=False)
        conn, cur = _conn((False,))
        assert mod._record_trial_ledger(conn, discovery)["status"] == "skipped_no_table"
        assert cur.execute.call_count == 1

    def test_records(self, mod, monkeypatch, discovery):
        monkeypatch.delenv("EML_LINEAGE_ENABLED", raising=False)
        conn, cur = _conn((True,))
        cur.rowcount = 1
        info = mod._record_trial_ledger(conn, discovery)
        assert info["status"] == "recorded"
        assert info["batches"] == 2 and info["inserted"] == 2
        assert info["n_trials"] == discovery.n_trials
        conn.commit.assert_called_once()
        sqls = [c[0][0] for c in cur.execute.call_args_list]
        assert sum("INSERT INTO qed_trial_batches" in q for q in sqls) == 2
