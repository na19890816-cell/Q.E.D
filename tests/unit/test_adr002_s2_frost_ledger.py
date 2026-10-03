"""ADR-002 S2: FROST 評価 → 試行台帳 (DB レス)。"""
from __future__ import annotations

import dataclasses
import math
from unittest.mock import MagicMock, patch

import pytest

from analytics.python.frost import frost_run_lineage as L
from analytics.python.frost.frost_config import FrostConfig
from analytics.python.frost.frost_contracts import FrostCandidate, FrostEvaluation, FrostRunOutput
from analytics.python.frost.frost_lineage import TrialLedger, make_family_key
from tests.fixtures.frost_synthetic import make_synthetic_candidates

pytestmark = pytest.mark.adr002_lineage


def _cand(cid, st="technical", h="h", horizon="5d", spec=None):
    return FrostCandidate(candidate_id=cid, run_id="r", trace_id="t", source_type=st,
                          candidate_hash=h, horizon=horizon, feature_spec_json=spec or {})


def _ev(cid, sharpe):
    return FrostEvaluation(candidate_id=cid, run_id="r", oos_sharpe=sharpe)


def _out(cands, evs, run_id="run-1"):
    return FrostRunOutput(run_id=run_id, trace_id="tr", candidates=cands, evaluations=evs,
                          batch_label="b", policy_hash="sha256:x")


class TestCounting:
    def test_eml_excluded(self):
        cs = [_cand("a", "eml", "h1"), _cand("b", "technical", "h2")]
        b = L.trial_batches_from_frost_output(_out(cs, [_ev("a", 1.0), _ev("b", 1.0)]))
        assert len(b) == 1 and b[0].n_trials == 1
        assert b[0].metadata["excluded_eml_candidates"] == 1

    def test_all_eml_gives_no_batch(self):
        cs = [_cand("a", "eml", "h1")]
        assert L.trial_batches_from_frost_output(_out(cs, [_ev("a", 1.0)])) == []

    def test_distinct_hash_dedup(self):
        cs = [_cand("a", h="same"), _cand("b", h="same"), _cand("c", h="other")]
        b = L.trial_batches_from_frost_output(_out(cs, [_ev("a", 1.0), _ev("b", 2.0), _ev("c", 0.5)]))[0]
        assert b.n_trials == 2 and b.metadata["n_candidates_evaluated"] == 3
        assert b.sr_stats.count == 2  # 同一式の Sharpe は最初の 1 件のみ

    def test_empty_hash_counts_each_candidate(self):
        cs = [_cand("a", h=""), _cand("b", h="")]
        assert L.trial_batches_from_frost_output(_out(cs, [_ev("a", 1), _ev("b", 1)]))[0].n_trials == 2

    def test_unevaluated_not_counted(self):
        cs = [_cand("a", h="1"), _cand("b", h="2")]
        assert L.trial_batches_from_frost_output(_out(cs, [_ev("a", 1.0)]))[0].n_trials == 1

    def test_stage_and_source(self):
        b = L.trial_batches_from_frost_output(_out([_cand("a")], [_ev("a", 1.0)]))[0]
        assert b.stage == "frost_eval" and b.source_type == "frost"

    def test_requires_run_id(self):
        with pytest.raises(ValueError):
            L.trial_batches_from_frost_output(_out([_cand("a")], [_ev("a", 1.0)], run_id=""))


class TestSharpe:
    def test_daily_conversion(self):
        cs = [_cand("a", h="1"), _cand("b", h="2")]
        b = L.trial_batches_from_frost_output(_out(cs, [_ev("a", math.sqrt(252)), _ev("b", 0.0)]))[0]
        assert b.sr_stats.mean == pytest.approx(0.5) and b.sr_periodicity == "daily"

    def test_nonfinite_and_none_skipped(self):
        cs = [_cand("a", h="1"), _cand("b", h="2"), _cand("c", h="3")]
        b = L.trial_batches_from_frost_output(_out(cs, [_ev("a", None), _ev("b", float("nan")), _ev("c", 1.0)]))[0]
        assert b.n_trials == 3 and b.sr_stats.count == 1 and b.metadata["n_oos_sharpe"] == 1

    def test_annualization_zero_disables_sr(self):
        b = L.trial_batches_from_frost_output(_out([_cand("a")], [_ev("a", 1.0)]), sharpe_annualization=0)[0]
        assert b.sr_stats.count == 0 and b.n_trials == 1


class TestFamily:
    def test_default_family_matches_eml_default(self):
        fam, _, used = L.candidate_family(_cand("a"))
        assert used is True
        assert fam == make_family_key("5d", "event_study_panel", "abnormal_return", "")

    def test_family_from_spec(self):
        c = _cand("a", spec={"universe": "jp_top500", "target_name": "ret_5d", "terminal_set_hash": "abc"})
        fam, spec, used = L.candidate_family(c)
        assert used is False and fam == make_family_key("5d", "jp_top500", "ret_5d", "abc")
        assert spec["universe"] == "jp_top500"

    def test_grouped_by_family(self):
        cs = [_cand("a", h="1", horizon="5d"), _cand("b", h="2", horizon="20d"), _cand("c", h="3", horizon="5d")]
        bs = L.trial_batches_from_frost_output(_out(cs, [_ev(x, 1.0) for x in "abc"]))
        assert sorted(b.n_trials for b in bs) == [1, 2]
        assert len({b.batch_id for b in bs}) == 2

    def test_default_fill_recorded(self):
        cs = [_cand("a", h="1"), _cand("b", h="2", spec={"universe": "event_study_panel",
                                                         "target_name": "abnormal_return"})]
        b = L.trial_batches_from_frost_output(_out(cs, [_ev("a", 1), _ev("b", 1)]))[0]
        assert b.metadata["family_source"] == {"default_filled": 1, "from_candidate": 1}

    def test_env_defaults(self, monkeypatch):
        monkeypatch.setenv("FROST_LEDGER_UNIVERSE", "u2")
        monkeypatch.setenv("FROST_LEDGER_SR_ANNUALIZATION", "bad")
        d = L.ledger_defaults_from_env()
        assert d["default_universe"] == "u2" and d["sharpe_annualization"] == 252


class TestDeterminismAndLedger:
    def test_batch_id_deterministic(self):
        o = _out([_cand("a")], [_ev("a", 1.0)])
        assert L.trial_batches_from_frost_output(o)[0].batch_id == L.trial_batches_from_frost_output(o)[0].batch_id

    def test_batch_id_distinct_from_s1_stages(self):
        from analytics.python.frost.frost_lineage import make_batch_id
        b = L.trial_batches_from_frost_output(_out([_cand("a")], [_ev("a", 1.0)]))[0]
        assert b.batch_id not in {make_batch_id("run-1", s, 0) for s in ("exhaustive", "gradient", "frost_eval")}

    def test_snapshot_accumulates_with_s1(self):
        """S1 (EML 探索) と S2 (FROST) が同一 family に合算される。"""
        from analytics.python.frost.frost_lineage import TrialBatch
        fam = make_family_key("5d", "event_study_panel", "abnormal_return", "")
        s1 = TrialBatch.create(fam, "run-1", "exhaustive", 17)
        s2 = L.trial_batches_from_frost_output(_out([_cand("a", h="1"), _cand("b", h="2")],
                                                   [_ev("a", 1.0), _ev("b", 2.0)]))
        snap = TrialLedger([s1, *s2], []).snapshot(fam)
        assert snap.n_trials == 19


class TestRunnerWiring:
    def _run(self, cands, conn=None, env=None, monkeypatch=None):
        from analytics.python.frost import frost_runner as R
        if monkeypatch and env:
            for k, v in env.items():
                monkeypatch.setenv(k, v)
        with patch.object(R, "_write_output"), patch.object(R, "_upsert_policy"):
            return R.run_frost_pipeline(cands, FrostConfig(dry_run=True), conn=conn, run_id="s2-test")

    def _conn(self, tables=True):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (tables,)
        cur.rowcount = 1
        return conn, cur

    def _techs(self, n=6):
        return [dataclasses.replace(c, source_type="technical") for c in make_synthetic_candidates(n=n)]

    def test_records_non_eml(self):
        conn, cur = self._conn()
        out = self._run(self._techs(), conn)
        assert out.lineage["status"] == "recorded" and out.lineage["n_trials"] == 6
        assert any("INSERT INTO qed_trial_batches" in c.args[0] for c in cur.execute.call_args_list)
        conn.commit.assert_called()

    def test_eml_only_nothing(self):
        conn, _ = self._conn()
        out = self._run(make_synthetic_candidates(n=4), conn)  # source_type=eml
        assert out.lineage["status"] == "nothing_to_record"

    def test_no_table_skips(self):
        conn, cur = self._conn(tables=False)
        out = self._run(self._techs(), conn)
        assert out.lineage["status"] == "skipped_no_table"
        assert not any("INSERT INTO qed_trial_batches" in c.args[0] for c in cur.execute.call_args_list)

    def test_disabled_by_env(self, monkeypatch):
        conn, _ = self._conn()
        out = self._run(self._techs(), conn, env={"FROST_LINEAGE_ENABLED": "0"}, monkeypatch=monkeypatch)
        assert out.lineage == {"status": "disabled"}

    def test_write_error_does_not_fail_run(self):
        conn, cur = self._conn()
        cur.execute.side_effect = [None, RuntimeError("boom")]
        out = self._run(self._techs(), conn)
        assert out.lineage["status"] == "error" and "lineage write error" in (out.error_message or "")
        assert out.status == "dry_run"

    def test_rollback_aborted_transaction_before_write(self):
        """先行書き込みの失敗で INERROR の接続は rollback してから台帳に書く (過少計上防止)。"""
        import psycopg
        conn, cur = self._conn()
        conn.info.transaction_status = psycopg.pq.TransactionStatus.INERROR
        out = self._run(self._techs(), conn)
        conn.rollback.assert_called()
        assert out.lineage["status"] == "recorded"

    def test_no_dsn_skips(self, monkeypatch):
        monkeypatch.delenv("QED_PG_DSN", raising=False)
        monkeypatch.delenv("FROST_PG_DSN", raising=False)
        out = self._run(self._techs(), conn=None)
        assert out.lineage["status"] in ("skipped_no_connection", "error")
        assert out.status == "dry_run"

    def test_decisions_unchanged_by_ledger(self, monkeypatch):
        """台帳記録の有無は決定に影響しない (golden 非影響)。"""
        cs = self._techs(20)
        conn, _ = self._conn()
        a = self._run([dataclasses.replace(c) for c in cs], conn)
        monkeypatch.setenv("FROST_LINEAGE_ENABLED", "0")
        b = self._run([dataclasses.replace(c) for c in cs], conn)
        assert a.lineage["status"] == "recorded" and b.lineage["status"] == "disabled"
        assert [(d.candidate_id, d.decision) for d in a.decisions] == [(d.candidate_id, d.decision) for d in b.decisions]
