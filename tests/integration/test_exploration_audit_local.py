"""Synthetic local POSIX integration. Never evaluates real alpha or writes DB."""
from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from analytics.python.audit.exploration_events import AuditError, ExplorationAudit, evidence_hash, inspect_attempt, read_events
from analytics.python.alpha.eml import eml_search as S
from analytics.python.alpha.eml.eml_master_formula import EMLDiscoveryConfig, run_eml_discovery
from analytics.python.alpha.eml.eml_tree import enumerate_trees
from analytics.python.frost import frost_runner as R, frost_selector as F
from analytics.python.frost.frost_config import FrostConfig
from analytics.python.frost.frost_contracts import FrostEvaluation, FrostRunOutput
from analytics.python.frost.frost_lineage import make_family_key
from analytics.python.frost.frost_run_lineage import trial_batches_from_frost_output
from analytics.python.frost.policy_spec import policy_spec_from_frost_config
from tests.fixtures.frost_synthetic import make_synthetic_candidates


def audit(tmp_path, run="run-1", family=None, policy=None, search="attempt-1"):
    return ExplorationAudit.create(tmp_path, search_id=search, run_id=run,
        family_key=family or make_family_key("5d", "U", "T", ""),
        policy_hash=policy or policy_spec_from_frost_config(FrostConfig()).policy_hash, code_commit="b" * 40,
        input_evidence_sha256="c" * 64, settings={"synthetic_fixture": True})


@pytest.fixture
def data():
    x = np.random.default_rng(7).normal(size=100)
    return pd.DataFrame({"x": x}), pd.Series(x + np.random.default_rng(8).normal(size=100))


@pytest.fixture
def tiny_trees(monkeypatch):
    trees = [tree for tree in enumerate_trees(2, ["x"]) if tree.depth() == 2][:4]
    assert len(trees) == 4
    monkeypatch.setattr(S, "enumerate_trees", lambda *args: copy.deepcopy(trees))


def signature(candidates):
    return [(c.compiled_expr, repr(c.fitness_score), c.status) for c in candidates]


@pytest.mark.parametrize("value", [1.0, -999.0, float("nan"), float("inf"), -float("inf")])
def test_exhaustive_all_before_top_k_with_exact_default_results(tmp_path, data, tiny_trees, value):
    df, target = data
    fn = lambda *args: value
    plain_stats, audited_stats = [], []
    plain = S.exhaustive_search(["x"], 2, "run-1", "tr", df, target, fn, top_k=1, stats_out=plain_stats)
    sink = audit(tmp_path)
    observed = S.exhaustive_search(["x"], 2, "run-1", "tr", df, target, fn, top_k=1,
                                  stats_out=audited_stats, audit=sink)
    assert signature(plain) == signature(observed)
    assert plain_stats == audited_stats
    report = sink.finish()
    events = read_events(sink.path)
    finished = [e for e in events if e["event_type"] == "TRIAL_FINISHED"]
    assert len(finished) == report["finished_trials"] == 4
    expected = "FAILED" if value == -999.0 else "NONFINITE" if not math.isfinite(value) else "SUCCEEDED"
    assert {e["data"]["status"] for e in finished} == {expected}
    assert sum(e["data"].get("returned_after_top_k", False) for e in events) == 1


def test_fitness_exception_is_audited_without_error_message(tmp_path, data, tiny_trees):
    sink = audit(tmp_path)
    def fail(*args):
        raise RuntimeError("postgresql://user:secret@host/private")
    result = S.exhaustive_search(["x"], 2, "run-1", "tr", *data, fail, top_k=1, audit=sink)
    assert result[0].fitness_score == -999
    sink.finish()
    finished = [e for e in read_events(sink.path) if e["event_type"] == "TRIAL_FINISHED"]
    assert all(e["data"]["result"]["error_type"] == "RuntimeError" for e in finished)
    assert not any(b"secret" in path.read_bytes() for path in sink.path.glob("*.json"))


def test_trial_start_durable_before_fitness_and_crash_visible(tmp_path, data, tiny_trees):
    sink = audit(tmp_path)
    calls = []
    def crash(*args):
        report = inspect_attempt(sink.path)
        assert report["started_trials"] == 1 and report["unfinished_trial_ids"]
        calls.append(1)
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        S.exhaustive_search(["x"], 2, "run-1", "tr", *data, crash, audit=sink)
    assert calls == [1]
    report = inspect_attempt(sink.path)
    assert report["state"] == "INCOMPLETE" and report["finished_trials"] == 0
    assert len(report["unfinished_trial_ids"]) == 1


def test_audit_failure_prevents_fitness(tmp_path, data, tiny_trees, monkeypatch):
    sink = audit(tmp_path)
    original = sink._append
    def fail_start(kind, payload):
        if kind == "TRIAL_STARTED":
            raise AuditError("synthetic write failure")
        return original(kind, payload)
    monkeypatch.setattr(sink, "_append", fail_start)
    calls = []
    with pytest.raises(AuditError):
        S.exhaustive_search(["x"], 2, "run-1", "tr", *data, lambda *args: calls.append(1), audit=sink)
    assert calls == []


def test_gradient_probes_recorded_and_never_added_to_s1_n(tmp_path, data):
    sink = audit(tmp_path)
    def fn(node, df, target):
        return 0.5
    plain_stats, observed_stats = [], []
    options = dict(n_init=2, adam_steps=1, top_k=1, rng_seed=7)
    plain = S.gradient_search(["x"], 2, "run-1", "tr", *data, fn, stats_out=plain_stats, **options)
    observed = S.gradient_search(["x"], 2, "run-1", "tr", *data, fn, stats_out=observed_stats, audit=sink, **options)
    assert signature(plain) == signature(observed) and plain_stats == observed_stats
    sink.finish()
    events = read_events(sink.path)
    probes = [e for e in events if e["event_type"] == "EVALUATION_STARTED"
              and e["data"]["kind"] == "optimizer_probe"]
    assert probes and all(not e["data"]["counts_as_independent_trial"] for e in probes)
    scope = next(e["data"] for e in events if e["event_type"] == "SCOPE_FINISHED")
    assert scope["stats"]["n_distinct_expr"] == observed_stats[0].n_trials
    assert observed_stats[0].n_trials <= 2 < len(probes)


def test_gradient_audit_failure_not_swallowed_by_safe_fitness(tmp_path, data, monkeypatch):
    sink = audit(tmp_path)
    old_note = sink.note
    def note(kind, value):
        if kind == "EVALUATION_STARTED" and value["kind"] == "optimizer_probe":
            raise AuditError("no space")
        old_note(kind, value)
    monkeypatch.setattr(sink, "note", note)
    calls = []
    with pytest.raises(AuditError):
        S.gradient_search(["x"], 2, "run-1", "tr", *data, lambda *args: calls.append(1),
                          n_init=2, adam_steps=1, rng_seed=7, audit=sink)
    assert calls == []
    assert inspect_attempt(sink.path)["finished_trials"] == 0


def test_master_discovery_default_decisions_and_s1_n_unchanged(tmp_path, data, tiny_trees):
    cfg = EMLDiscoveryConfig(run_id="run-1", trace_id="tr", terminal_set=["x"], max_depth=2,
                            gradient_n_init=2, gradient_steps=1, top_k_exhaustive=1,
                            top_k_gradient=1, rng_seed=7, universe="U", target_name="T")
    ts_hash = hashlib.md5(json.dumps(["x"]).encode()).hexdigest()[:16]
    sink = audit(tmp_path, family=make_family_key("5d", "U", "T", ts_hash))
    plain = run_eml_discovery(cfg, *data)
    observed = run_eml_discovery(cfg, *data, audit=sink)
    assert signature(plain.candidates) == signature(observed.candidates)
    assert plain.search_stats == observed.search_stats
    assert plain.n_trials == observed.n_trials
    report = sink.finish()
    assert report["state"] == "COMPLETED"
    dispositions = [e["data"] for e in read_events(sink.path) if e["event_type"] == "CANDIDATE_DISPOSITION"]
    assert any(e.get("step") == "eml_promotion_threshold" for e in dispositions)


def evaluation_signature(evaluation):
    value = asdict(evaluation)
    for key in ("evaluation_id", "created_at", "updated_at"):
        value.pop(key)
    return value


def test_frost_rejected_candidates_and_s2_n_preserved(tmp_path):
    candidates = make_synthetic_candidates(6)
    candidates[0].source_type = "technical"
    candidates[1].source_type = "technical"
    config = FrostConfig()
    sink = audit(tmp_path, run="meta", policy=policy_spec_from_frost_config(config).policy_hash)
    plain = F.evaluate_candidates_batch(copy.deepcopy(candidates), "meta", "tr", config)
    observed = F.evaluate_candidates_batch(copy.deepcopy(candidates), "meta", "tr", config, audit=sink)
    assert [evaluation_signature(e) for e in plain] == [evaluation_signature(e) for e in observed]
    report = sink.finish()
    assert report["finished_trials"] == 6
    finished = [e["data"] for e in read_events(sink.path) if e["event_type"] == "TRIAL_FINISHED"]
    assert any(not e["result"]["hard_gate_passed"] for e in finished)
    a = trial_batches_from_frost_output(FrostRunOutput(run_id="meta", candidates=candidates, evaluations=plain))
    b = trial_batches_from_frost_output(FrostRunOutput(run_id="meta", candidates=candidates, evaluations=observed))
    assert [(x.batch_id, x.n_trials, x.sr_stats) for x in a] == [(x.batch_id, x.n_trials, x.sr_stats) for x in b]
    assert all(x.n_trials <= 2 for x in a)
    starts = [e["data"] for e in read_events(sink.path) if e["event_type"] == "TRIAL_STARTED"]
    assert sum(e["counting_owner"] == "ADR002_S1" for e in starts) == 4
    assert starts[0]["evidence"]["candidate_input_sha256"] == evidence_hash(asdict(candidates[0]))


def test_frost_partial_failure_preserves_preceding_result_and_unvisited_inputs(tmp_path, monkeypatch):
    candidates = make_synthetic_candidates(3)
    sink = audit(tmp_path, run="meta")
    calls = []
    def evaluate(candidate, *args):
        calls.append(candidate.candidate_id)
        if len(calls) == 2:
            raise ValueError("secret message must not be logged")
        return FrostEvaluation(candidate_id=candidate.candidate_id, run_id="meta", hard_gate_passed=False)
    monkeypatch.setattr(F, "evaluate_candidate", evaluate)
    with pytest.raises(ValueError):
        F.evaluate_candidates_batch(candidates, "meta", "tr", FrostConfig(), audit=sink)
    report = inspect_attempt(sink.path)
    assert report["started_trials"] == 3 and report["finished_trials"] == 2
    assert len(report["unfinished_trial_ids"]) == 1
    finished = [e["data"] for e in read_events(sink.path) if e["event_type"] == "TRIAL_FINISHED"]
    assert [e["status"] for e in finished] == ["SUCCEEDED", "FAILED"]
    assert not any(b"secret message" in path.read_bytes() for path in sink.path.glob("*.json"))


def test_frost_runner_no_decision_change_and_audit_has_no_db_connector(tmp_path, monkeypatch):
    run_id = "00000000-0000-0000-0000-000000000001"
    config = FrostConfig()
    sink = audit(tmp_path, run=run_id, policy=policy_spec_from_frost_config(config).policy_hash)
    writes = []
    monkeypatch.setattr(R, "_write_output", lambda *args: writes.append("existing_writer_stub"))
    monkeypatch.setattr(R, "_upsert_policy", lambda *args: None)
    monkeypatch.setattr(R, "_record_frost_trial_ledger", lambda *args: {"status": "synthetic_no_db"})
    candidates = make_synthetic_candidates(6)
    plain = R.run_frost_pipeline(copy.deepcopy(candidates), config, run_id=run_id, trace_id="tr")
    observed = R.run_frost_pipeline(copy.deepcopy(candidates), config, run_id=run_id, trace_id="tr", audit=sink)
    fields = ("candidate_id", "decision", "decision_rank", "frost_score", "promotion_eligible", "gate_failures")
    assert [[getattr(d, k) for k in fields] for d in plain.decisions] == [[getattr(d, k) for k in fields] for d in observed.decisions]
    assert plain.status == observed.status
    assert len(writes) == 2  # Stubs prove no real DB/cron/trading access was used.
    assert sink.finish()["ledger_writes"] == 0


def test_frost_audit_failure_not_converted_to_failed_pipeline_and_written(tmp_path, monkeypatch):
    run_id = "00000000-0000-0000-0000-000000000001"
    config = FrostConfig()
    sink = audit(tmp_path, run=run_id, policy=policy_spec_from_frost_config(config).policy_hash)
    monkeypatch.setattr(R, "_write_output", lambda *args: pytest.fail("audit failure must stop existing writer"))
    monkeypatch.setattr(F, "evaluate_candidate", lambda *args: (_ for _ in ()).throw(AuditError("disk full")))
    with pytest.raises(AuditError):
        R.run_frost_pipeline(make_synthetic_candidates(2), config, run_id=run_id, audit=sink)


def test_frost_observed_family_matches_s2_environment_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("FROST_LEDGER_UNIVERSE", "explicit_U")
    monkeypatch.setenv("FROST_LEDGER_TARGET", "explicit_T")
    from analytics.python.frost.frost_run_lineage import ledger_defaults_from_env
    candidates = make_synthetic_candidates(1)
    candidates[0].source_type = "technical"
    sink = audit(tmp_path, run="meta")
    evaluations = F.evaluate_candidates_batch(candidates, "meta", "tr", FrostConfig(), audit=sink)
    start = next(e["data"] for e in read_events(sink.path) if e["event_type"] == "TRIAL_STARTED")
    batch, = trial_batches_from_frost_output(FrostRunOutput(run_id="meta", candidates=candidates,
                                                           evaluations=evaluations), **ledger_defaults_from_env())
    assert start["evidence"]["family_key"] == batch.family_key
    assert start["evidence"]["family_spec"] == batch.family_spec
    assert not start["ledger_write_verified"] and start["ledger_batch_id"] is None


@pytest.mark.parametrize("mode", ["exhaustive", "gradient"])
def test_default_auditerror_callback_keeps_legacy_failure_sentinel(data, tiny_trees, mode):
    def fail(*args):
        raise AuditError("raised by callback without audit")
    if mode == "exhaustive":
        result = S.exhaustive_search(["x"], 2, "run", "tr", *data, fail, top_k=1)
    else:
        result = S.gradient_search(["x"], 2, "run", "tr", *data, fail, n_init=1, adam_steps=1, rng_seed=7)
    assert result and all(c.fitness_score == -999.0 for c in result)


def test_master_failed_reference_and_nonfinite_do_not_claim_success(tmp_path, monkeypatch):
    from analytics.python.alpha.eml import eml_master_formula as M
    cfg = EMLDiscoveryConfig(run_id="run-1", trace_id="tr", terminal_set=["missing"],
                            max_depth=2, use_gradient=False, top_k_exhaustive=10000,
                            universe="U", target_name="T")
    ts_hash = hashlib.md5(json.dumps(["missing"]).encode()).hexdigest()[:16]
    sink = audit(tmp_path, family=make_family_key("5d", "U", "T", ts_hash))
    df, target = pd.DataFrame({"other": range(10)}), pd.Series(range(10))
    run_eml_discovery(cfg, df, target, audit=sink)
    records = [e["data"] for e in read_events(sink.path) if e["event_type"] == "TRIAL_FINISHED"
               and e["data"]["scope_id"].startswith("eml_discovery")]
    assert records
    assert any(e["status"] == "FAILED" for e in records)
    assert any(e["status"] == "NONFINITE" for e in records)
    for record in records:
        assert "error" not in record["result"]["evaluation"]["metadata"]


def test_master_records_effective_environment_weights(tmp_path, data, tiny_trees, monkeypatch):
    monkeypatch.setenv("EML_PROMO_W_RANK_IC", "0.123")
    cfg = EMLDiscoveryConfig(run_id="run-1", trace_id="tr", terminal_set=["x"],
                            max_depth=2, use_gradient=False, top_k_exhaustive=1,
                            universe="U", target_name="T")
    ts_hash = hashlib.md5(json.dumps(["x"]).encode()).hexdigest()[:16]
    sink = audit(tmp_path, family=make_family_key("5d", "U", "T", ts_hash))
    run_eml_discovery(cfg, *data, audit=sink)
    scope = next(e["data"] for e in read_events(sink.path) if e["event_type"] == "SCOPE_STARTED")
    assert scope["settings"]["effective_promotion_fitness_weights"]["rank_ic"] == 0.123


def test_master_weight_change_stops_before_reference_evaluation(tmp_path, data, tiny_trees, monkeypatch):
    from analytics.python.alpha.eml import eml_master_formula as M
    cfg = EMLDiscoveryConfig(run_id="run-1", trace_id="tr", terminal_set=["x"],
                            max_depth=2, use_gradient=False, top_k_exhaustive=1,
                            universe="U", target_name="T")
    ts_hash = hashlib.md5(json.dumps(["x"]).encode()).hexdigest()[:16]
    sink = audit(tmp_path, family=make_family_key("5d", "U", "T", ts_hash))
    original = M.exhaustive_search
    def change(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setenv("EML_PROMO_W_RANK_IC", "0.987654")
        return result
    monkeypatch.setattr(M, "exhaustive_search", change)
    with pytest.raises(AuditError, match="weights changed"):
        M.run_eml_discovery(cfg, *data, audit=sink)
    assert inspect_attempt(sink.path)["state"] == "INCOMPLETE"


def test_frost_nonfinite_original_input_has_explicit_warning(tmp_path):
    candidates = make_synthetic_candidates(1)
    candidates[0].metrics["rank_ic"] = float("nan")
    sink = audit(tmp_path, run="meta")
    F.evaluate_candidates_batch(candidates, "meta", "tr", FrostConfig(), audit=sink)
    start = next(e["data"] for e in read_events(sink.path) if e["event_type"] == "TRIAL_STARTED")
    assert start["evidence"]["input_contains_nonfinite"] is True
    assert start["evidence"]["candidate_input_sha256"] == evidence_hash(asdict(candidates[0]))


def test_process_exit_during_eml_evaluation_preserves_durable_start(tmp_path):
    script = """
import os, sys
import pandas as pd
from analytics.python.audit.exploration_events import ExplorationAudit
from analytics.python.alpha.eml.eml_search import exhaustive_search
sink = ExplorationAudit.create(sys.argv[1], search_id='crash', run_id='crash-run',
    family_key='fam:'+'a'*32, policy_hash='sha256:'+'b'*64, code_commit='c'*40,
    input_evidence_sha256='d'*64, settings={'synthetic_fixture': True})
def crash(*args):
    os._exit(17)
exhaustive_search(['x'], 2, 'crash-run', 'tr', pd.DataFrame({'x': range(10)}),
    pd.Series(range(10)), crash, audit=sink)
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, check=False)
    assert result.returncode == 17
    path = tmp_path / hashlib.sha256(b"crash").hexdigest()
    report = inspect_attempt(path)
    assert report["started_trials"] == 1 and report["finished_trials"] == 0
    assert report["state"] == "INCOMPLETE" and len(report["unfinished_trial_ids"]) == 1
    assert any(e["event_type"] == "EVALUATION_STARTED" for e in read_events(path))


def test_direct_frost_batch_policy_mismatch_stops_before_evaluation(tmp_path, monkeypatch):
    sink = audit(tmp_path, run="meta", policy="sha256:" + "e" * 64)
    monkeypatch.setattr(F, "evaluate_candidate", lambda *args: pytest.fail("must reject before evaluation"))
    with pytest.raises(AuditError, match="policy differs"):
        F.evaluate_candidates_batch(make_synthetic_candidates(1), "meta", "tr", FrostConfig(), audit=sink)
    assert len(read_events(sink.path)) == 1


def test_same_session_propagates_eml_to_frost_without_new_run_reservation(tmp_path, data, tiny_trees):
    sink = audit(tmp_path, run="shared-run")
    S.exhaustive_search(["x"], 2, "shared-run", "tr", *data, lambda *args: 0.5, top_k=1, audit=sink)
    F.evaluate_candidates_batch(make_synthetic_candidates(1), "shared-run", "tr", FrostConfig(), audit=sink)
    report = sink.finish()
    assert report["state"] == "COMPLETED"
    assert len(report["scope_stats"]) == 2
