"""
test_adr002_lineage.py
----------------------
ADR-002 系譜ログ (B 設計) — frost_lineage.py / postgres_lineage_bridge.py の DB レス単体テスト
"""
from __future__ import annotations

import inspect
import json
import math
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from analytics.python.frost import frost_lineage
from analytics.python.frost.frost_dsr import DsrGate, N_TRIALS_SOURCE_ASSUMED, N_TRIALS_SOURCE_PROVIDED
from analytics.python.frost.frost_lineage import (
    FAMILY_PREFIX,
    LEDGER_NAMESPACE,
    SNAPSHOT_PREFIX,
    VALID_RELATIONS,
    VALID_STAGES,
    LineageEdge,
    SharpeStats,
    TrialBatch,
    TrialLedger,
    TrialSnapshot,
    family_spec_dict,
    formula_hash,
    make_batch_id,
    make_edge_id,
    make_family_key,
    merge_all,
    normalize_formula,
)
from analytics.python.pg_io.postgres_lineage_bridge import (
    fetch_trial_snapshot,
    insert_lineage_edge,
    insert_trial_batch,
    insert_trial_batches,
    load_ledger_for_family,
)

pytestmark = pytest.mark.adr002_lineage

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
FAM_A = make_family_key("5d", "TOPIX500", "fwd_ret_5d", "ts1")
FAM_B = make_family_key("5d", "TOPIX500", "fwd_ret_5d", "ts2")
FAM_C = make_family_key("20d", "TOPIX500", "fwd_ret_20d", "ts1")


def _batch(fam=FAM_A, run="r1", stage="exhaustive", n=100, srs=None, seq=0, at=T0, **kw):
    return TrialBatch.create(fam, run, stage, n, sharpes=srs, seq=seq, recorded_at=at, **kw)


# ===========================================================================
# 識別子
# ===========================================================================

class TestFormulaHash:
    def test_prefix_and_length(self):
        h = formula_hash("rank(close)")
        assert h.startswith("sha256:") and len(h) == 7 + 64

    def test_whitespace_insensitive(self):
        assert formula_hash("a + b") == formula_hash("a+b") == formula_hash("  a +\tb\n")

    def test_different_formulas(self):
        assert formula_hash("a+b") != formula_hash("a-b")

    def test_empty_raises(self):
        for v in ("", "   ", None):
            with pytest.raises(ValueError):
                formula_hash(v)

    def test_normalize(self):
        assert normalize_formula(" x *  y ") == "x*y"
        assert normalize_formula(None) == ""

    def test_process_independent(self):
        """組み込み hash() と異なり PYTHONHASHSEED に依存しない (ADR-002 L3)"""
        code = ("from analytics.python.frost.frost_lineage import formula_hash;"
                "print(formula_hash('rank(close/delay(close,5))'))")
        outs = set()
        for seed in ("1", "2", "3"):
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                               env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
                               cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]))
            assert r.returncode == 0, r.stderr
            outs.add(r.stdout.strip())
        assert len(outs) == 1


class TestFamilyKey:
    def test_prefix_and_length(self):
        assert FAM_A.startswith(FAMILY_PREFIX) and len(FAM_A) == len(FAMILY_PREFIX) + 32

    def test_deterministic(self):
        assert make_family_key("5d", "TOPIX500", "fwd_ret_5d", "ts1") == FAM_A

    def test_strips(self):
        assert make_family_key(" 5d ", "TOPIX500 ", "fwd_ret_5d", " ts1") == FAM_A

    def test_components_matter(self):
        assert len({FAM_A, FAM_B, FAM_C}) == 3

    def test_extra_changes_key(self):
        assert make_family_key("5d", "TOPIX500", "fwd_ret_5d", "ts1", cost_model="v2") != FAM_A

    def test_extra_order_independent(self):
        a = make_family_key("5d", "U", "T", x=1, y=2)
        b = make_family_key("5d", "U", "T", y=2, x=1)
        assert a == b

    @pytest.mark.parametrize("args", [("", "U", "T"), ("5d", " ", "T"), ("5d", "U", None)])
    def test_required(self, args):
        with pytest.raises(ValueError):
            make_family_key(*args)

    def test_reserved_extra(self):
        with pytest.raises(TypeError):
            make_family_key("5d", "U", "T", **{"horizon": "x"})

    def test_spec_dict(self):
        d = family_spec_dict("5d", "U", "T", "ts", note="n")
        assert d == {"horizon": "5d", "universe": "U", "target": "T",
                     "terminal_set_hash": "ts", "note": "n"}


class TestBatchEdgeIds:
    def test_batch_id_deterministic(self):
        assert make_batch_id("r1", "exhaustive", 0) == make_batch_id("r1", "exhaustive", 0)

    def test_batch_id_varies(self):
        ids = {make_batch_id("r1", "exhaustive", 0), make_batch_id("r1", "exhaustive", 1),
               make_batch_id("r1", "gradient", 0), make_batch_id("r2", "exhaustive", 0)}
        assert len(ids) == 4

    def test_batch_id_requires_run(self):
        with pytest.raises(ValueError):
            make_batch_id("", "manual")

    def test_namespace_frozen(self):
        """namespace を変えると既存 batch_id の冪等性が壊れる → 値を固定"""
        assert str(LEDGER_NAMESPACE) == str(
            __import__("uuid").uuid5(__import__("uuid").NAMESPACE_DNS, "qed.adr002.trial_ledger"))

    def test_edge_id_deterministic_and_varies(self):
        a = make_edge_id(FAM_A, FAM_B, "mutation")
        assert a == make_edge_id(FAM_A, FAM_B, "mutation")
        assert a != make_edge_id(FAM_A, FAM_B, "retrain")
        assert a != make_edge_id(FAM_B, FAM_A, "mutation")
        assert a != make_edge_id(FAM_A, FAM_B, "mutation", "sha256:x")


# ===========================================================================
# SharpeStats
# ===========================================================================

class TestSharpeStats:
    def test_empty(self):
        s = SharpeStats()
        assert s.count == 0 and s.variance is None

    def test_single(self):
        s = SharpeStats.from_values([0.3])
        assert s.count == 1 and s.mean == 0.3 and s.variance is None

    def test_known(self):
        s = SharpeStats.from_values([1.0, 2.0, 3.0, 4.0])
        assert s.mean == pytest.approx(2.5)
        assert s.variance == pytest.approx(5.0 / 3.0)

    def test_filters(self):
        s = SharpeStats.from_values([1.0, None, float("nan"), "x", float("inf"), 3.0])
        assert s.count == 2 and s.variance == pytest.approx(2.0)

    def test_none_input(self):
        assert SharpeStats.from_values(None).count == 0

    @pytest.mark.parametrize("kw", [{"count": -1}, {"count": 2, "m2": -1.0},
                                    {"count": 0, "mean": 1.0}])
    def test_invalid(self, kw):
        with pytest.raises(ValueError):
            SharpeStats(**kw)

    def test_merge_equals_pooled(self):
        rng = random.Random(1)
        xs = [rng.gauss(0.05, 0.3) for _ in range(200)]
        parts = [xs[:13], xs[13:90], xs[90:91], xs[91:]]
        merged = merge_all(SharpeStats.from_values(p) for p in parts)
        pooled = SharpeStats.from_values(xs)
        assert merged.count == pooled.count
        assert merged.mean == pytest.approx(pooled.mean, abs=1e-14)
        assert merged.variance == pytest.approx(pooled.variance, rel=1e-12)

    def test_merge_commutative(self):
        a = SharpeStats.from_values([0.1, 0.4, -0.2])
        b = SharpeStats.from_values([0.7, 0.2])
        ab, ba = a.merge(b), b.merge(a)
        assert ab.count == ba.count
        assert ab.mean == pytest.approx(ba.mean) and ab.m2 == pytest.approx(ba.m2)

    def test_merge_identity(self):
        a = SharpeStats.from_values([0.1, 0.4])
        assert a.merge(SharpeStats()) == a and SharpeStats().merge(a) == a

    def test_against_numpy(self):
        np = pytest.importorskip("numpy")
        xs = [random.Random(5).uniform(-1, 1) for _ in range(50)]
        assert SharpeStats.from_values(xs).variance == pytest.approx(float(np.var(xs, ddof=1)))

    def test_to_dict(self):
        assert SharpeStats.from_values([1, 3]).to_dict() == {"count": 2, "mean": 2.0, "m2": 2.0}


# ===========================================================================
# TrialBatch / LineageEdge
# ===========================================================================

class TestTrialBatch:
    def test_create(self):
        b = _batch(srs=[0.1, 0.2])
        assert b.batch_id == make_batch_id("r1", "exhaustive", 0)
        assert b.sr_stats.count == 2 and b.n_trials == 100

    def test_frozen(self):
        with pytest.raises(Exception):
            _batch().n_trials = 5  # type: ignore[misc]

    @pytest.mark.parametrize("stage", VALID_STAGES)
    def test_valid_stages(self, stage):
        _batch(stage=stage)

    def test_invalid_stage(self):
        with pytest.raises(ValueError):
            _batch(stage="bogus")

    def test_negative_n(self):
        with pytest.raises(ValueError):
            _batch(n=-1)

    def test_sr_count_exceeds_n(self):
        with pytest.raises(ValueError):
            _batch(n=1, srs=[0.1, 0.2])

    def test_bad_family_key(self):
        with pytest.raises(ValueError):
            _batch(fam="nope")

    def test_naive_datetime_to_utc(self):
        b = _batch(at=datetime(2026, 1, 1))
        assert b.recorded_at.tzinfo is not None

    def test_to_row(self):
        r = _batch(srs=[0.1, 0.3], metadata={"k": 1}).to_row()
        assert r["sr_count"] == 2 and r["sr_mean"] == pytest.approx(0.2)
        assert r["metadata"] == {"k": 1} and r["stage"] == "exhaustive"

    def test_zero_trials_allowed(self):
        assert _batch(n=0).n_trials == 0


class TestLineageEdge:
    def test_create(self):
        e = LineageEdge.create(FAM_A, FAM_B, "mutation", recorded_at=T0)
        assert e.edge_id == make_edge_id(FAM_A, FAM_B, "mutation")

    @pytest.mark.parametrize("rel", VALID_RELATIONS)
    def test_valid_relations(self, rel):
        LineageEdge.create(FAM_A, FAM_B, rel)

    def test_invalid_relation(self):
        with pytest.raises(ValueError):
            LineageEdge.create(FAM_A, FAM_B, "cousin")

    def test_bad_family(self):
        with pytest.raises(ValueError):
            LineageEdge.create("x", FAM_B, "mutation")

    def test_to_row(self):
        r = LineageEdge.create(FAM_A, FAM_B, "retrain", child_formula_hash="sha256:c").to_row()
        assert r["relation"] == "retrain" and r["child_formula_hash"] == "sha256:c"


# ===========================================================================
# TrialLedger
# ===========================================================================

class TestLedgerBasics:
    def test_dedup_on_init(self):
        b = _batch()
        assert len(TrialLedger(batches=[b, b]).batches) == 1

    def test_append_idempotent(self):
        led = TrialLedger()
        assert led.append_batch(_batch()) is True
        assert led.append_batch(_batch()) is False
        assert len(led.batches) == 1

    def test_append_edge_idempotent(self):
        led = TrialLedger()
        e = LineageEdge.create(FAM_A, FAM_B, "mutation")
        assert led.append_edge(e) and not led.append_edge(e)

    def test_empty_snapshot(self):
        s = TrialLedger().snapshot(FAM_A)
        assert s.is_empty and s.n_trials == 0 and s.to_dsr_kwargs() == {}


class TestLedgerCounting:
    def test_sums_across_runs(self):
        """L2: 10 run × 100 試行 は N=1000 (N=100 ではない)"""
        led = TrialLedger(batches=[_batch(run=f"r{i}", n=100) for i in range(10)])
        assert led.snapshot(FAM_A).n_trials == 1000

    def test_other_family_excluded(self):
        led = TrialLedger(batches=[_batch(n=50), _batch(fam=FAM_C, n=999)])
        assert led.snapshot(FAM_A).n_trials == 50

    def test_counts_unsaved_trials(self):
        """L1: SR を持たない (保存しなかった) 試行も件数に含まれる"""
        led = TrialLedger(batches=[_batch(n=4212, srs=[0.1, 0.2, 0.3])])
        s = led.snapshot(FAM_A)
        assert s.n_trials == 4212 and s.sr_stats.count == 3

    def test_sr_variance_merged(self):
        led = TrialLedger(batches=[
            _batch(run="r1", n=10, srs=[0.1, 0.2, 0.3]),
            _batch(run="r2", n=10, srs=[0.5, -0.1]),
        ])
        s = led.snapshot(FAM_A)
        assert s.sr_variance == pytest.approx(
            SharpeStats.from_values([0.1, 0.2, 0.3, 0.5, -0.1]).variance)

    def test_periodicity_filter(self):
        led = TrialLedger(batches=[
            _batch(run="r1", n=10, srs=[0.1, 0.2], sr_periodicity="daily"),
            _batch(run="r2", n=20, srs=[1.0, 2.0], sr_periodicity="weekly"),
        ])
        s = led.snapshot(FAM_A, sr_periodicity="daily")
        assert s.n_trials == 30              # 件数は頻度に関わらず全計上 (過少計上防止)
        assert s.sr_stats.count == 2         # 統計は daily のみ
        assert s.excluded_periodicity_batches == 1

    def test_no_periodicity_filter_merges_all(self):
        led = TrialLedger(batches=[
            _batch(run="r1", n=10, srs=[0.1, 0.2], sr_periodicity="daily"),
            _batch(run="r2", n=20, srs=[1.0, 2.0], sr_periodicity="weekly"),
        ])
        assert led.snapshot(FAM_A).sr_stats.count == 4


class TestLedgerAsOf:
    def test_future_batches_excluded(self):
        """R3: 判定時点より後の試行で過去の N が変わらない"""
        led = TrialLedger(batches=[
            _batch(run="r1", n=100, at=T0),
            _batch(run="r2", n=900, at=T0 + timedelta(days=10)),
        ])
        assert led.snapshot(FAM_A, as_of=T0 + timedelta(days=1)).n_trials == 100
        assert led.snapshot(FAM_A, as_of=T0 + timedelta(days=10)).n_trials == 1000  # <= 境界含む
        assert led.snapshot(FAM_A).n_trials == 1000

    def test_unknown_time_excluded_under_as_of(self):
        led = TrialLedger(batches=[_batch(at=None, n=5), _batch(run="r2", n=7)])
        assert led.snapshot(FAM_A, as_of=T0).n_trials == 7
        assert led.snapshot(FAM_A).n_trials == 12

    def test_naive_as_of(self):
        led = TrialLedger(batches=[_batch(n=5)])
        assert led.snapshot(FAM_A, as_of=datetime(2026, 1, 2)).n_trials == 5

    def test_append_after_snapshot_does_not_change_it(self):
        led = TrialLedger(batches=[_batch(n=5)])
        s1 = led.snapshot(FAM_A, as_of=T0)
        led.append_batch(_batch(run="late", n=50, at=T0 + timedelta(hours=1)))
        s2 = led.snapshot(FAM_A, as_of=T0)
        assert s1.n_trials == s2.n_trials and s1.snapshot_hash == s2.snapshot_hash


class TestLedgerLineage:
    def _ledger(self):
        return TrialLedger(
            batches=[_batch(fam=FAM_A, run="a", n=100), _batch(fam=FAM_B, run="b", n=10),
                     _batch(fam=FAM_C, run="c", n=1)],
            edges=[LineageEdge.create(FAM_A, FAM_B, "mutation", recorded_at=T0),
                   LineageEdge.create(FAM_B, FAM_C, "retrain", recorded_at=T0)],
        )

    def test_inherits_ancestors(self):
        """派生元の探索コストを継承する: C = C + B + A"""
        assert self._ledger().snapshot(FAM_C).n_trials == 111

    def test_middle(self):
        assert self._ledger().snapshot(FAM_B).n_trials == 110

    def test_descendants_not_counted(self):
        """子孫方向は辿らない: A の N に B/C を含めない"""
        assert self._ledger().snapshot(FAM_A).n_trials == 100

    def test_include_ancestors_false(self):
        assert self._ledger().snapshot(FAM_C, include_ancestors=False).n_trials == 1

    def test_families_reported(self):
        assert set(self._ledger().snapshot(FAM_C).families) == {FAM_A, FAM_B, FAM_C}

    def test_cycle_safe(self):
        led = TrialLedger(
            batches=[_batch(fam=FAM_A, n=1), _batch(fam=FAM_B, run="b", n=2)],
            edges=[LineageEdge.create(FAM_A, FAM_B, "mutation"),
                   LineageEdge.create(FAM_B, FAM_A, "mutation")],
        )
        assert led.snapshot(FAM_A).n_trials == 3
        assert led.snapshot(FAM_B).n_trials == 3

    def test_future_edge_ignored(self):
        led = TrialLedger(
            batches=[_batch(fam=FAM_A, n=100), _batch(fam=FAM_B, run="b", n=10)],
            edges=[LineageEdge.create(FAM_A, FAM_B, "mutation", recorded_at=T0 + timedelta(days=5))],
        )
        assert led.snapshot(FAM_B, as_of=T0 + timedelta(days=1)).n_trials == 10
        assert led.snapshot(FAM_B, as_of=T0 + timedelta(days=5)).n_trials == 110

    def test_diamond_counted_once(self):
        """A→B, A→C, B→D, C→D でも A は 1 回だけ計上"""
        fam_d = make_family_key("5d", "U", "T", "d")
        led = TrialLedger(
            batches=[_batch(fam=f, run=f, n=n) for f, n in
                     ((FAM_A, 1000), (FAM_B, 10), (FAM_C, 10), (fam_d, 1))],
            edges=[LineageEdge.create(FAM_A, FAM_B, "mutation"),
                   LineageEdge.create(FAM_A, FAM_C, "mutation"),
                   LineageEdge.create(FAM_B, fam_d, "ensemble_member"),
                   LineageEdge.create(FAM_C, fam_d, "ensemble_member")],
        )
        assert led.snapshot(fam_d).n_trials == 1021


# ===========================================================================
# TrialSnapshot
# ===========================================================================

class TestSnapshot:
    def test_hash_prefix(self):
        s = TrialLedger(batches=[_batch()]).snapshot(FAM_A, as_of=T0)
        assert s.snapshot_hash.startswith(SNAPSHOT_PREFIX)

    def test_hash_deterministic_order_independent(self):
        b1, b2 = _batch(run="r1"), _batch(run="r2")
        h1 = TrialLedger(batches=[b1, b2]).snapshot(FAM_A, as_of=T0).snapshot_hash
        h2 = TrialLedger(batches=[b2, b1]).snapshot(FAM_A, as_of=T0).snapshot_hash
        assert h1 == h2

    def test_hash_changes_with_batches(self):
        a = TrialLedger(batches=[_batch(run="r1")]).snapshot(FAM_A, as_of=T0)
        b = TrialLedger(batches=[_batch(run="r1"), _batch(run="r2")]).snapshot(FAM_A, as_of=T0)
        assert a.snapshot_hash != b.snapshot_hash

    def test_hash_changes_with_as_of(self):
        led = TrialLedger(batches=[_batch()])
        assert led.snapshot(FAM_A, as_of=T0).snapshot_hash != \
            led.snapshot(FAM_A, as_of=T0 + timedelta(days=1)).snapshot_hash

    def test_dsr_kwargs(self):
        s = TrialLedger(batches=[_batch(n=40, srs=[0.1, 0.2, 0.4])]).snapshot(FAM_A)
        kw = s.to_dsr_kwargs()
        assert kw["n_trials"] == 40
        assert kw["sr_variance"] == pytest.approx(SharpeStats.from_values([0.1, 0.2, 0.4]).variance)

    def test_dsr_kwargs_without_variance(self):
        s = TrialLedger(batches=[_batch(n=40, srs=[0.1])]).snapshot(FAM_A)
        assert s.to_dsr_kwargs() == {"n_trials": 40}

    def test_to_dict_json(self):
        d = TrialLedger(batches=[_batch(srs=[0.1, 0.2])]).snapshot(FAM_A, as_of=T0).to_dict()
        json.dumps(d)
        assert d["n_trials"] == 100 and d["batch_count"] == 1 and d["snapshot_hash"]


# ===========================================================================
# DSR 連携 (ADR-002 → NOTE-001)
# ===========================================================================

class TestDsrIntegration:
    def _returns(self):
        rng = random.Random(3)
        return [rng.gauss(0.0015, 0.01) for _ in range(500)]

    def test_empty_ledger_falls_back_to_assumed(self):
        snap = TrialLedger().snapshot(FAM_A)
        r = DsrGate().check(self._returns(), **snap.to_dsr_kwargs())
        assert r.n_trials_source == N_TRIALS_SOURCE_ASSUMED and r.review_required

    def test_ledger_provides_n(self):
        snap = TrialLedger(batches=[_batch(n=3, srs=[0.01, 0.02, 0.03])]).snapshot(FAM_A)
        r = DsrGate().check(self._returns(), **snap.to_dsr_kwargs())
        assert r.n_trials == 3 and r.n_trials_source == N_TRIALS_SOURCE_PROVIDED
        assert r.sr_variance_source == "provided"

    def test_hidden_trials_flip_decision(self):
        """
        同じリターンでも、台帳に探索の実コストが記録されると DSR が不合格に転じる
        (top_k だけを数えていた L1 の危険性の実証)
        """
        rets = self._returns()
        top_k_only = TrialLedger(batches=[_batch(n=1)]).snapshot(FAM_A)
        full = TrialLedger(batches=[_batch(n=10000)]).snapshot(FAM_A)
        assert DsrGate().check(rets, **top_k_only.to_dsr_kwargs()).passed
        assert not DsrGate().check(rets, **full.to_dsr_kwargs()).passed


# ===========================================================================
# postgres_lineage_bridge (MagicMock)
# ===========================================================================

def _conn(fetchall_seq=None, rowcount=1):
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.fetchall.side_effect = list(fetchall_seq or [])
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=cur)
    ctx.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = ctx
    return conn, cur


def _batch_row(b: TrialBatch):
    r = b.to_row()
    return (r["batch_id"], r["family_key"], r["run_id"], r["trace_id"], r["source_type"],
            r["stage"], r["n_trials"], r["sr_count"], r["sr_mean"], r["sr_m2"],
            r["sr_periodicity"], json.dumps(r["family_spec"]), r["metadata"], r["recorded_at"])


def _edge_row(e: LineageEdge):
    r = e.to_row()
    return (r["edge_id"], r["parent_family_key"], r["child_family_key"],
            r["parent_formula_hash"], r["child_formula_hash"], r["relation"],
            r["run_id"], r["metadata"], r["recorded_at"])


class TestBridgeWrite:
    def test_dry_run(self):
        conn, cur = _conn()
        assert insert_trial_batch(conn, _batch(), dry_run=True) is False
        conn.cursor.assert_not_called()

    def test_insert_batch_sql(self):
        conn, cur = _conn()
        assert insert_trial_batch(conn, _batch(srs=[0.1, 0.2])) is True
        sql, params = cur.execute.call_args[0]
        assert "INSERT INTO qed_trial_batches" in sql
        assert "ON CONFLICT (batch_id) DO NOTHING" in sql
        assert "UPDATE" not in sql.upper().replace("ON CONFLICT", "")
        assert params[0] == make_batch_id("r1", "exhaustive", 0)
        assert params[6] == 100 and params[7] == 2
        json.loads(params[11]); json.loads(params[12])

    def test_insert_batch_conflict(self):
        conn, _ = _conn(rowcount=0)
        assert insert_trial_batch(conn, _batch()) is False

    def test_insert_batches_count(self):
        conn, cur = _conn(rowcount=1)
        assert insert_trial_batches(conn, [_batch(run="a"), _batch(run="b")]) == 2
        assert cur.execute.call_count == 2

    def test_insert_edge(self):
        conn, cur = _conn()
        e = LineageEdge.create(FAM_A, FAM_B, "mutation")
        assert insert_lineage_edge(conn, e) is True
        sql, params = cur.execute.call_args[0]
        assert "INSERT INTO qed_lineage_edges" in sql and "DO NOTHING" in sql
        assert params[0] == e.edge_id and params[5] == "mutation"

    def test_insert_edge_dry_run(self):
        conn, cur = _conn()
        assert insert_lineage_edge(conn, LineageEdge.create(FAM_A, FAM_B, "mutation"), dry_run=True) is False
        cur.execute.assert_not_called()


class TestBridgeRead:
    def test_load_with_ancestors(self):
        ba, bb = _batch(fam=FAM_A, run="a", n=100), _batch(fam=FAM_B, run="b", n=10)
        e = LineageEdge.create(FAM_A, FAM_B, "mutation", recorded_at=T0)
        conn, cur = _conn(fetchall_seq=[[(FAM_B,), (FAM_A,)], [_edge_row(e)],
                                        [_batch_row(ba), _batch_row(bb)]])
        led = load_ledger_for_family(conn, FAM_B)
        assert len(led.batches) == 2 and len(led.edges) == 1
        assert led.snapshot(FAM_B).n_trials == 110
        first_sql = cur.execute.call_args_list[0][0][0]
        assert "WITH RECURSIVE" in first_sql

    def test_load_without_ancestors(self):
        conn, cur = _conn(fetchall_seq=[[_batch_row(_batch(n=7))]])
        led = load_ledger_for_family(conn, FAM_A, include_ancestors=False)
        assert cur.execute.call_count == 1
        assert led.snapshot(FAM_A).n_trials == 7

    def test_row_roundtrip_preserves_stats(self):
        b = _batch(srs=[0.1, 0.25, -0.05], metadata={"k": "v"})
        conn, _ = _conn(fetchall_seq=[[_batch_row(b)]])
        got = load_ledger_for_family(conn, FAM_A, include_ancestors=False).batches[0]
        assert got.sr_stats == b.sr_stats and got.metadata == {"k": "v"}
        assert got.family_spec == {}

    def test_fetch_snapshot_as_of(self):
        b1 = _batch(run="r1", n=5, at=T0)
        b2 = _batch(run="r2", n=50, at=T0 + timedelta(days=3))
        conn, cur = _conn(fetchall_seq=[[(FAM_A,)], [], [_batch_row(b1), _batch_row(b2)]])
        snap = fetch_trial_snapshot(conn, FAM_A, as_of=T0 + timedelta(days=1))
        assert snap.n_trials == 5
        assert cur.execute.call_args_list[0][0][1][1] == T0 + timedelta(days=1)

    def test_bytes_text_columns(self):
        """SQL_ASCII DB では TEXT が bytes で返る → 防御的にデコード"""
        e = LineageEdge.create(FAM_A, FAM_B, "retrain", recorded_at=T0)
        b = _batch(fam=FAM_A, n=3)
        enc = lambda row: tuple(v.encode() if isinstance(v, str) else v for v in row)
        conn, _ = _conn(fetchall_seq=[[(FAM_B.encode(),)], [enc(_edge_row(e))], [enc(_batch_row(b))]])
        led = load_ledger_for_family(conn, FAM_B)
        assert led.edges[0].relation == "retrain"
        assert led.snapshot(FAM_B).n_trials == 3

    def test_bad_jsonb_type(self):
        row = list(_batch_row(_batch()))
        row[12] = 123
        conn, _ = _conn(fetchall_seq=[[tuple(row)]])
        with pytest.raises(ValueError):
            load_ledger_for_family(conn, FAM_A, include_ancestors=False)


# ===========================================================================
# マイグレーション / 設計原則
# ===========================================================================

class TestMigration084:
    @pytest.fixture(scope="class")
    def sql(self):
        from pathlib import Path
        p = Path(__file__).resolve().parents[2] / "qedschema/migrations/084_qed_trial_ledger.sql"
        return p.read_text(encoding="utf-8")

    def test_tables(self, sql):
        assert "CREATE TABLE qed_trial_batches" in sql
        assert "CREATE TABLE qed_lineage_edges" in sql

    def test_append_only_triggers(self, sql):
        assert "BEFORE UPDATE OR DELETE ON qed_trial_batches" in sql
        assert "BEFORE UPDATE OR DELETE ON qed_lineage_edges" in sql
        assert "RAISE EXCEPTION" in sql

    def test_stage_and_relation_checks_match_python(self, sql):
        for s in VALID_STAGES:
            assert f"'{s}'" in sql
        for r in VALID_RELATIONS:
            assert f"'{r}'" in sql

    def test_no_alter_existing(self, sql):
        assert "ALTER TABLE" not in sql.upper()

    def test_registered(self, sql):
        assert "084_qed_trial_ledger.sql" in sql


class TestDesignPrinciples:
    def test_no_numpy_statistics_builtin_hash(self):
        import ast
        src = inspect.getsource(frost_lineage)
        assert "import numpy" not in src and "import statistics" not in src
        calls = [n.func.id for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        assert "hash" not in calls, "組み込み hash() は PYTHONHASHSEED 依存のため使用禁止"
