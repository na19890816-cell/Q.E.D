"""Local synthetic software checks; no production history or alpha claims."""
from __future__ import annotations

import errno
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys

import pytest

from analytics.python.audit import exploration_events as E


def make_audit(tmp_path, **overrides):
    options = dict(search_id="attempt-1", run_id="run-1", family_key="fam:" + "a" * 32,
                   policy_hash="sha256:" + "b" * 64, code_commit="c" * 40,
                   input_evidence_sha256="d" * 64, settings={"seed": 7, "candidate_models": 3})
    options.update(overrides)
    return E.ExplorationAudit.create(tmp_path, **options)


def test_start_fixed_before_any_evaluation(tmp_path):
    audit = make_audit(tmp_path)
    event, = E.read_events(audit.path)
    assert event["event_type"] == "RUN_STARTED"
    assert event["context"]["historical_n_raw"] is None
    assert event["data"]["settings"]["candidate_models"] == 3
    assert datetime.fromisoformat(event["recorded_at"]).utcoffset().total_seconds() == 0
    assert E.inspect_attempt(audit.path)["state"] == "INCOMPLETE"


def test_complete_attempt_separately_fixes_counts(tmp_path):
    audit = make_audit(tmp_path)
    scope = audit.start_scope("exhaustive", {"top_k": 1})
    for i in range(3):
        audit.start_trial(scope, str(i), evidence={"input": i}, counting_owner="ADR002_S1", n_raw_role="distinct")
        audit.finish_trial(str(i), status="SUCCEEDED", result={"fitness": i}, formula_key="same")
    audit.finish_scope(scope, {"n_distinct_expr": 1, "n_generated": 3, "n_returned": 1})
    report = audit.finish()
    assert report["finished_trials"] == 3 and report["state"] == "COMPLETED"
    assert report["ledger_writes"] == 0
    events = E.read_events(audit.path)
    assert events[0]["data"]["settings"].get("n_returned") is None
    assert events[-2]["data"]["observed_distinct_formula_keys"] == 1
    with pytest.raises(E.AuditError):
        audit.start_scope("later", {})


@pytest.mark.parametrize("override", [
    {"search_id": ""}, {"run_id": ""}, {"family_key": "unknown"}, {"policy_hash": "unknown"},
    {"code_commit": "c92cef1"}, {"input_evidence_sha256": ""}, {"historical_n_raw": 0},
    {"historical_n_raw": True}, {"historical_n_raw": -1},
    {"settings": {"pg_dsn": "postgresql://private"}}, {"settings": {"nested": {"password": "private"}}},
])
def test_missing_provenance_or_credentials_rejected(tmp_path, override):
    with pytest.raises(E.AuditError):
        make_audit(tmp_path, **override)
    assert list(tmp_path.glob("*/000000000001.json")) == []


def test_immutable_same_attempt_conflict(tmp_path):
    first = make_audit(tmp_path)
    original = (first.path / "000000000001.json").read_bytes()
    with pytest.raises(E.AuditError, match="already exists"):
        make_audit(tmp_path, settings={"seed": 99})
    assert (first.path / "000000000001.json").read_bytes() == original


def test_independent_reevaluation_cannot_reuse_adr002_run_in_same_root(tmp_path):
    first = make_audit(tmp_path)
    with pytest.raises(E.AuditError, match="run_id already reserved"):
        make_audit(tmp_path, search_id="new-attempt", settings={"seed": 9})
    second = make_audit(tmp_path, search_id="new-attempt", run_id="new-run", settings={"seed": 9})
    assert first.context["run_id"] != second.context["run_id"]
    assert len(E.read_events(first.path)) == len(E.read_events(second.path)) == 1


def test_same_attempt_concurrent_create_one_winner(tmp_path):
    def create(_):
        try:
            return make_audit(tmp_path)
        except E.AuditError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result is not None)
    assert len(E.read_events(winner.path)) == 1


def test_unfinished_trial_is_not_invented_as_success_or_zero_n(tmp_path):
    audit = make_audit(tmp_path)
    scope = audit.start_scope("gradient", {"n_init": 10})
    audit.start_trial(scope, "trial-1", evidence={"seed": 7}, counting_owner="ADR002_S1", n_raw_role="distinct")
    report = E.inspect_attempt(audit.path)
    assert report["unfinished_trial_ids"] == ["trial-1"]
    assert report["historical_n_raw"] is None
    assert report["scope_stats"] == {}
    with pytest.raises(E.AuditError, match="unfinished"):
        audit.finish_scope(scope, {"n_distinct_expr": 0})
    with pytest.raises(E.AuditError, match="unfinished"):
        audit.finish()


def test_count_cannot_disagree_with_observed_final_formulas(tmp_path):
    audit = make_audit(tmp_path)
    scope = audit.start_scope("exhaustive", {})
    audit.start_trial(scope, "one", evidence={}, counting_owner="ADR002_S1", n_raw_role="distinct")
    audit.finish_trial("one", status="SUCCEEDED", result={}, formula_key="one")
    with pytest.raises(E.AuditError, match="disagree"):
        audit.finish_scope(scope, {"n_distinct_expr": 2})


@pytest.mark.parametrize("mode", ["mutate", "remove_middle", "truncate", "duplicate_key", "nonfinite"])
def test_damage_detected(tmp_path, mode):
    audit = make_audit(tmp_path)
    audit.start_scope("one", {})
    audit.note("RUN_FAILED", {"error_type": "SyntheticError"})
    target = audit.path / "000000000002.json"
    target.chmod(0o600)  # Simulate corruption by the filesystem owner.
    if mode == "mutate":
        payload = json.loads(target.read_bytes())
        payload["data"]["stage"] = "changed"
        target.write_text(json.dumps(payload))
    elif mode == "remove_middle":
        target.unlink()
    elif mode == "truncate":
        target.write_bytes(target.read_bytes()[:16])
    elif mode == "duplicate_key":
        target.write_text('{"sequence":2,"sequence":2}')
    else:
        target.write_text('{"value":NaN}')
    with pytest.raises(E.AuditError):
        E.inspect_attempt(audit.path)


@pytest.mark.parametrize("stage", ["file_fsync", "link", "directory_fsync"])
def test_disk_failure_stops_before_evaluation(tmp_path, monkeypatch, stage):
    audit = make_audit(tmp_path)
    old_fsync = E.os.fsync
    if stage == "link":
        monkeypatch.setattr(E.os, "link", lambda *args: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full")))
    else:
        calls = []
        def fail(fd):
            calls.append(fd)
            if stage == "file_fsync" or len(calls) == 2:
                raise OSError(errno.ENOSPC, "full")
            return old_fsync(fd)
        monkeypatch.setattr(E.os, "fsync", fail)
    with pytest.raises(E.AuditError, match="write failed"):
        audit.start_scope("must_not_evaluate", {})
    monkeypatch.undo()
    # A post-link directory-fsync error may leave a visible last file. It is
    # explicitly incomplete, and is never represented as a completed run.
    assert E.inspect_attempt(audit.path)["state"] == "INCOMPLETE"


def test_nonfinite_evidence_preserved_without_zero_imputation(tmp_path):
    audit = make_audit(tmp_path)
    scope = audit.start_scope("one", {})
    audit.start_trial(scope, "one", evidence={}, counting_owner="ADR002_S1", n_raw_role="distinct")
    audit.finish_trial("one", status="NONFINITE", result={"fitness": float("nan"), "missing": None,
                                                         "positive": float("inf"), "negative": -float("inf")})
    result = E.read_events(audit.path)[-1]["data"]["result"]
    assert result == {"fitness": {"$nonfinite": "nan"}, "missing": None,
                      "positive": {"$nonfinite": "+inf"}, "negative": {"$nonfinite": "-inf"}}


def test_naive_datetime_cannot_be_claimed_as_known_utc(tmp_path):
    with pytest.raises(E.AuditError, match="naive"):
        make_audit(tmp_path, settings={"observed_at": datetime(2020, 1, 1)})


def test_wrong_run_stops_before_scope(tmp_path):
    audit = make_audit(tmp_path)
    with pytest.raises(E.AuditError, match="run_id"):
        audit.require_run("another-run")
    assert len(E.read_events(audit.path)) == 1


def test_final_and_trial_rewrites_rejected(tmp_path):
    audit = make_audit(tmp_path)
    scope = audit.start_scope("one", {})
    audit.start_trial(scope, "one", evidence={}, counting_owner="ADR002_S1", n_raw_role="distinct")
    with pytest.raises(E.AuditError):
        audit.start_trial(scope, "one", evidence={"changed": True}, counting_owner="ADR002_S1", n_raw_role="distinct")
    audit.finish_trial("one", status="FAILED", result={"error_type": "ValueError"})
    with pytest.raises(E.AuditError):
        audit.finish_trial("one", status="SUCCEEDED", result={})


def test_hashes_bind_exact_evidence_and_result(tmp_path):
    audit = make_audit(tmp_path)
    scope = audit.start_scope("one", {})
    audit.start_trial(scope, "one", evidence={"seed": 7}, counting_owner="RESEARCH_ONLY", n_raw_role="research")
    audit.finish_trial("one", status="SUCCEEDED", result={"n": 1})
    for event in E.read_events(audit.path):
        data = event["data"]
        if "evidence" in data:
            assert data["evidence_sha256"] == E.evidence_hash(data["evidence"])
        if "result" in data:
            assert data["result_sha256"] == E.evidence_hash(data["result"])


@pytest.mark.parametrize("completed", [False, True])
def test_inspection_cli_verifies_persisted_journal_without_db(tmp_path, completed):
    audit = make_audit(tmp_path)
    if completed:
        audit.finish()
    script = Path(__file__).resolve().parents[2] / "scripts/audit/inspect_exploration.py"
    result = subprocess.run([sys.executable, str(script), str(audit.path)], capture_output=True, text=True, check=False)
    assert result.returncode == (0 if completed else 1)
    assert json.loads(result.stdout)["state"] == ("COMPLETED" if completed else "INCOMPLETE")
    assert not result.stderr


def test_readonly_events_and_orphan_staging_are_reported(tmp_path):
    audit = make_audit(tmp_path)
    event = audit.path / "000000000001.json"
    assert event.stat().st_mode & 0o777 == 0o400
    (audit.path / ".pending-synthetic-interruption").write_bytes(b"incomplete")
    assert E.inspect_attempt(audit.path)["orphan_staging_files"] == 1


def test_missing_provisioned_root_rejected_without_creating_ancestors(tmp_path):
    path = tmp_path / "missing" / "root"
    with pytest.raises(E.AuditError, match="existing provisioned"):
        make_audit(path)
    assert not (tmp_path / "missing").exists()


def test_inspection_resyncs_visible_event_and_directories_after_lost_ack(tmp_path, monkeypatch):
    audit = make_audit(tmp_path)
    real_sync = E._sync_dir
    monkeypatch.setattr(E, "_sync_dir", lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "fsync failure")))
    with pytest.raises(E.AuditError):
        audit.start_scope("visible_without_ack", {})
    monkeypatch.setattr(E, "_sync_dir", real_sync)
    synced_dirs = []
    def record(path):
        synced_dirs.append(Path(path))
        return real_sync(path)
    monkeypatch.setattr(E, "_sync_dir", record)
    events = E.read_events(audit.path)
    assert events[-1]["data"]["stage"] == "visible_without_ack"
    assert synced_dirs == [audit.path, audit.path.parent]
    assert E.inspect_attempt(audit.path)["state"] == "INCOMPLETE"


def test_inspection_does_not_ack_visible_files_when_resync_fails(tmp_path, monkeypatch):
    audit = make_audit(tmp_path)
    monkeypatch.setattr(E.os, "fsync", lambda *args: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full")))
    with pytest.raises(E.AuditError, match="durability recovery failed"):
        E.inspect_attempt(audit.path)
