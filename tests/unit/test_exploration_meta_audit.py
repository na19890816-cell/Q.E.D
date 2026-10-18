"""Synthetic fixtures prove research-event persistence, never statistical alpha."""
from __future__ import annotations

import dataclasses

import pytest

from analytics.python.audit.exploration_events import (
    AuditError, ExplorationAudit, evidence_hash, inspect_attempt, read_events,
)
from analytics.python.frost import frost_meta_sensitivity as M
from analytics.python.frost.frost_config import FrostConfig
from tests.fixtures.frost_synthetic import make_synthetic_candidates


@pytest.fixture
def prepared():
    candidates = make_synthetic_candidates(n=8, seed=7)
    config = FrostConfig(top_k=3, promotion_top_k=2, pg_dsn="postgresql://secret-placeholder")
    bundles = M.compute_bundles(candidates, config)
    return candidates, config, bundles, M._decide(candidates, bundles, config)


def _audit(tmp_path, *, historical_n_raw=None):
    return ExplorationAudit.create(
        tmp_path, search_id="meta-attempt", run_id="meta-run",
        family_key="fam:" + "a" * 32, policy_hash=M._policy_hash(FrostConfig(top_k=3, promotion_top_k=2)), code_commit="c" * 40,
        input_evidence_sha256="d" * 64, settings={"synthetic": True},
        historical_n_raw=historical_n_raw,
    )


def _call(stage, prepared, audit=None):
    candidates, config, bundles, base = prepared
    if stage == "baseline":
        return M.run_meta_validation(candidates, config, analyses=(), audit=audit)
    if stage == M.ANALYSIS_THRESHOLD:
        return M.threshold_sensitivity(candidates, config, base, deltas=(-0.2, 0.2),
                                       gates=("rank_ic",), audit=audit)
    if stage == M.ANALYSIS_ABLATION:
        return M.axis_ablation(candidates, bundles, config, base,
                               weights=("w_predictive", "w_oos_sharpe"), audit=audit)
    return M.weight_perturbation(candidates, bundles, config, base, n_runs=3, seed=11, audit=audit)


def test_complete_meta_results_unchanged_and_all_configurations_preserved(tmp_path, prepared):
    candidates, config, _, _ = prepared
    original = evidence_hash([dataclasses.asdict(c) for c in candidates])
    kwargs = {"n_runs": 3, "deltas": (0.1,), "seed": 17}
    plain = M.run_meta_validation(candidates, config, **kwargs)
    audit = _audit(tmp_path)
    observed = M.run_meta_validation(candidates, config, **kwargs, audit=audit)
    assert observed.to_dict() == plain.to_dict()
    assert evidence_hash([dataclasses.asdict(c) for c in candidates]) == original
    summary = audit.finish()
    assert summary["state"] == "COMPLETED"
    assert summary["ledger_writes"] == 0
    assert summary["historical_n_raw"] is None
    events = read_events(audit.path)
    starts = [e for e in events if e["event_type"] == "TRIAL_STARTED"]
    finishes = [e for e in events if e["event_type"] == "TRIAL_FINISHED"]
    assert len(starts) == len(finishes) == 1 + 8 + 10 + 3
    assert all(e["data"]["counting_owner"] == "RESEARCH_ONLY" for e in starts)
    assert all(e["data"]["n_raw_role"] == "research_configuration" for e in starts)
    assert all(e["data"]["formula_key"] is None for e in finishes)
    assert all(e["data"]["status"] == "SUCCEEDED" for e in finishes)
    assert all(len(e["data"]["result"]["snapshot"]["decisions"]) == len(candidates) for e in finishes)
    assert "secret-placeholder" not in repr(events)
    assert "pg_dsn" not in repr(events)
    scopes = [e["data"] for e in events if e["event_type"] == "SCOPE_STARTED"]
    assert len(scopes) == 4
    assert all(s["settings"]["dataset_hash"] == M.dataset_hash(candidates) for s in scopes)
    assert all(s["settings"]["input_evidence_sha256"] == "d" * 64 for s in scopes)
    assert all(s["settings"]["historical_n_raw"] is None for s in scopes)
    assert all("cached_bundle_sha256" in s["settings"] for s in scopes if
               s["stage"] in (M.ANALYSIS_ABLATION, M.ANALYSIS_WEIGHT))


@pytest.mark.parametrize("stage", ["baseline", M.ANALYSIS_THRESHOLD, M.ANALYSIS_ABLATION, M.ANALYSIS_WEIGHT])
def test_each_evaluation_starts_after_durable_event(tmp_path, prepared, monkeypatch, stage):
    audit = _audit(tmp_path, historical_n_raw=29)
    name = {"baseline": "compute_bundles", M.ANALYSIS_THRESHOLD: "full_replay"}.get(stage, "weight_replay")
    calculate = getattr(M, name)
    seen = []

    def observe(*args, **kwargs):
        events = read_events(audit.path)
        assert events[-1]["event_type"] == "TRIAL_STARTED"
        seen.append(events[-1]["data"]["trial_id"])
        return calculate(*args, **kwargs)

    monkeypatch.setattr(M, name, observe)
    _call(stage, prepared, audit)
    audit.finish()
    assert len(seen) == {"baseline": 1, M.ANALYSIS_WEIGHT: 3}.get(stage, 2)
    assert len(set(seen)) == len(seen)
    assert inspect_attempt(audit.path)["historical_n_raw"] == 29


@pytest.mark.parametrize("stage", ["baseline", M.ANALYSIS_THRESHOLD, M.ANALYSIS_ABLATION, M.ANALYSIS_WEIGHT])
@pytest.mark.parametrize("error", [ValueError, KeyboardInterrupt])
def test_failed_or_interrupted_configuration_retained_and_raised(tmp_path, prepared, monkeypatch, stage, error):
    audit = _audit(tmp_path)
    name = {"baseline": "compute_bundles", M.ANALYSIS_THRESHOLD: "full_replay"}.get(stage, "weight_replay")

    def fail(*args, **kwargs):
        assert read_events(audit.path)[-1]["event_type"] == "TRIAL_STARTED"
        raise error("sensitive error text must not be journaled")

    monkeypatch.setattr(M, name, fail)
    with pytest.raises(error):
        _call(stage, prepared, audit)
    events = read_events(audit.path)
    assert events[-1]["event_type"] == "TRIAL_FINISHED"
    assert events[-1]["data"]["status"] == "FAILED"
    assert events[-1]["data"]["result"] == {"error_type": error.__name__}
    assert "sensitive error text" not in repr(events)
    assert not any(e["event_type"] == "SCOPE_FINISHED" for e in events)
    assert inspect_attempt(audit.path)["state"] == "INCOMPLETE"


@pytest.mark.parametrize("stage", ["baseline", M.ANALYSIS_THRESHOLD, M.ANALYSIS_ABLATION, M.ANALYSIS_WEIGHT])
def test_start_persistence_failure_prevents_evaluation(tmp_path, prepared, monkeypatch, stage):
    audit = _audit(tmp_path)
    name = {"baseline": "compute_bundles", M.ANALYSIS_THRESHOLD: "full_replay"}.get(stage, "weight_replay")

    def not_evaluated(*args, **kwargs):
        pytest.fail("evaluation began before its audit start was durable")

    def unavailable(*args, **kwargs):
        raise AuditError("disk unavailable")

    monkeypatch.setattr(M, name, not_evaluated)
    monkeypatch.setattr(audit, "start_trial", unavailable)
    with pytest.raises(AuditError):
        _call(stage, prepared, audit)
    assert not any(e["event_type"] == "TRIAL_FINISHED" for e in read_events(audit.path))


def test_result_persistence_failure_stops_later_perturbations(tmp_path, prepared, monkeypatch):
    audit = _audit(tmp_path)
    append = audit._append
    calculate = M.weight_replay
    calls = []

    def observe(*args, **kwargs):
        calls.append(1)
        return calculate(*args, **kwargs)

    def interrupted_write(event_type, data):
        if event_type == "TRIAL_FINISHED":
            raise AuditError("result storage unavailable")
        return append(event_type, data)

    monkeypatch.setattr(M, "weight_replay", observe)
    monkeypatch.setattr(audit, "_append", interrupted_write)
    with pytest.raises(AuditError):
        _call(M.ANALYSIS_WEIGHT, prepared, audit)
    assert len(calls) == 1
    assert len(inspect_attempt(audit.path)["unfinished_trial_ids"]) == 1


def test_individual_weight_settings_results_replay_and_seed_are_preserved(tmp_path, prepared):
    candidates, config, bundles, base = prepared
    audit = _audit(tmp_path)
    observed = _call(M.ANALYSIS_WEIGHT, prepared, audit)
    assert [r.to_dict() for r in observed] == [r.to_dict() for r in _call(M.ANALYSIS_WEIGHT, prepared)]
    events = read_events(audit.path)
    starts = [e["data"] for e in events if e["event_type"] == "TRIAL_STARTED"]
    finishes = [e["data"] for e in events if e["event_type"] == "TRIAL_FINISHED"]
    for start, finish in zip(starts, finishes):
        cfg = dataclasses.replace(config, **start["evidence"]["setting"]["override"])
        snapshot = M.weight_replay(candidates, bundles, cfg)
        assert finish["result"]["snapshot"] == dataclasses.asdict(snapshot)
        assert finish["result"]["metrics"] == M.compare_snapshots(
            base, snapshot, promotion_top_k=config.promotion_top_k,
        )
    settings = next(e["data"]["settings"] for e in events if e["event_type"] == "SCOPE_STARTED")
    assert settings["seed"] == 11
    assert settings["planned_configurations"] == 3
    audit.finish()


def test_input_order_and_cached_evidence_are_bound(tmp_path, prepared):
    candidates, config, bundles, base = prepared
    audit = _audit(tmp_path)
    M.axis_ablation(candidates, bundles, config, base, weights=(), audit=audit)
    M.axis_ablation(list(reversed(candidates)), bundles, config, base, weights=(), audit=audit)
    starts = [e["data"]["settings"] for e in read_events(audit.path) if e["event_type"] == "SCOPE_STARTED"]
    assert starts[0]["dataset_hash"] == starts[1]["dataset_hash"]
    assert starts[0]["candidate_input_sha256"] != starts[1]["candidate_input_sha256"]
    assert starts[0]["candidate_order"] == list(reversed(starts[1]["candidate_order"]))
    assert starts[0]["cached_bundle_sha256"] == starts[1]["cached_bundle_sha256"]
    audit.finish()


def test_baseline_policy_mismatch_refuses_evaluation(tmp_path, prepared, monkeypatch):
    audit = _audit(tmp_path)
    candidates, config, _, _ = prepared
    changed = dataclasses.replace(config, min_oos_sharpe=config.min_oos_sharpe + 0.1)
    monkeypatch.setattr(M, "compute_bundles", lambda *args: pytest.fail("must reject before evaluation"))
    with pytest.raises(AuditError, match="baseline policy differs"):
        M.run_meta_validation(candidates, changed, analyses=(), audit=audit)
    assert len(read_events(audit.path)) == 1
