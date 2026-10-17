"""S3 real PostgreSQL: atomicity, conflict detection, concurrency and as-of."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest

from analytics.python.frost.manual_trial_ledger import parse_manual_evidence
from analytics.python.pg_io.postgres_lineage_bridge import fetch_trial_snapshot, insert_trial_batch
from analytics.python.pg_io.postgres_manual_trial_bridge import ManualImportConflict, register_manual_trials
from tests.unit.test_adr002_s3_manual_ledger import evidence_doc, raw

DSN = os.environ.get("QED_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="QED_PG_DSN not configured")
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def conn():
    import psycopg
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("CREATE TABLE IF NOT EXISTS _migrations(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())")
        for _ in range(2):
            for name in ("084_qed_trial_ledger.sql", "086_qed_manual_trial_imports.sql",
                         "089_qed_manual_trial_evaluations.sql"):
                c.execute((ROOT / "qedschema/migrations" / name).read_text())
        yield c


@pytest.fixture
def doc():
    d = evidence_doc()
    d["source"] = uuid.uuid4().hex
    d["family"]["universe"] = uuid.uuid4().hex
    return d


def submission(doc):
    return parse_manual_evidence(raw(doc), doc["n_trials"], "integration-test")


def test_register_retry_exact_hash_and_asof(conn, doc):
    s = submission(doc)
    before = conn.execute("SELECT clock_timestamp()").fetchone()[0]
    first = register_manual_trials(conn, s)
    second = register_manual_trials(conn, s)
    assert first.inserted and not second.inserted
    assert first.recorded_at == second.recorded_at and first.recorded_at >= before
    assert fetch_trial_snapshot(conn, s.batch.family_key, as_of=before).n_trials == 0
    snapshot = fetch_trial_snapshot(conn, s.batch.family_key, as_of=first.recorded_at)
    assert snapshot.n_trials == 2 and snapshot.to_dsr_kwargs() == {"n_trials": 2}
    row = conn.execute("SELECT n_trials,metadata,recorded_at FROM qed_trial_batches WHERE batch_id=%s", (s.batch.batch_id,)).fetchone()
    assert row[0] == 2 and row[1]["evidence_sha256"] == s.evidence_sha256
    assert row[2] == first.recorded_at


@pytest.mark.parametrize("change", ["n", "family", "evidence", "completed_at"])
def test_same_identity_conflicting_payload_rejected(conn, doc, change):
    first = submission(doc)
    register_manual_trials(conn, first)
    changed = copy.deepcopy(doc)
    if change == "n":
        changed["n_trials"] = 1
        changed["trials"] = changed["trials"][:1]
    elif change == "family":
        changed["family"]["horizon"] = "20d"
    elif change == "completed_at":
        changed["completed_at"] = "2026-01-03T12:00:00Z"
    else:
        changed["trials"][0]["formula"] = "new-formula"
    with pytest.raises(ManualImportConflict):
        register_manual_trials(conn, submission(changed))
    assert fetch_trial_snapshot(conn, first.batch.family_key).n_trials == 2


def test_relabelled_reformatted_evidence_is_atomic_conflict(conn, doc):
    register_manual_trials(conn, submission(doc))
    doc["research_run_id"] = "renamed-run"
    doc["source"] += "-renamed"
    doc["trials"].reverse()
    s = submission(doc)
    with pytest.raises(ManualImportConflict, match="already registered"):
        register_manual_trials(conn, s)
    assert conn.execute("SELECT count(*) FROM qed_trial_batches WHERE batch_id=%s", (s.batch.batch_id,)).fetchone()[0] == 0


@pytest.mark.parametrize("rename_source", [False, True])
def test_changed_completion_time_cannot_recount_inventory(conn, doc, rename_source):
    original = submission(doc)
    register_manual_trials(conn, original)
    changed = copy.deepcopy(doc)
    changed.update(research_run_id="another-run", completed_at="2026-01-03T12:00:00Z")
    if rename_source:
        changed["source"] += "-renamed"
    duplicate = submission(changed)
    with pytest.raises(ManualImportConflict, match="already registered"):
        register_manual_trials(conn, duplicate)
    for table in ("qed_trial_batches", "qed_manual_trial_imports"):
        assert conn.execute(f"SELECT count(*) FROM {table} WHERE batch_id=%s", (duplicate.batch.batch_id,)).fetchone()[0] == 0
    assert fetch_trial_snapshot(conn, original.batch.family_key).n_trials == 2


def test_legacy_hash_receipt_requires_audit_before_new_family_imports(conn, doc):
    original = submission(doc)
    metadata = dict(original.batch.metadata)
    metadata.pop("inventory_hash_version", None)
    assert insert_trial_batch(conn, replace(original.batch, metadata=metadata))
    # A pre-review receipt has no inventory-hash version; its original inventory
    # cannot be recovered from the digest alone. Keep it immutable and fail closed.
    conn.execute(
        "INSERT INTO qed_manual_trial_imports "
        "(source,research_run_id,batch_id,evidence_sha256,inventory_sha256,n_trials,"
        "completed_at,submitted_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (original.source, original.research_run_id, original.batch.batch_id,
         original.evidence_sha256, original.inventory_sha256, original.batch.n_trials,
         original.completed_at, original.submitted_by),
    )
    changed = copy.deepcopy(doc)
    changed.update(research_run_id="another-run", completed_at="2026-01-03T12:00:00Z")
    with pytest.raises(ManualImportConflict, match="legacy.*audit"):
        register_manual_trials(conn, submission(changed))
    assert fetch_trial_snapshot(conn, original.batch.family_key).n_trials == 2
    changed["family"]["universe"] += "-independent"
    assert register_manual_trials(conn, submission(changed)).inserted


def test_independent_evaluation_times_remain_distinct_trials(conn, doc):
    first = submission(doc)
    assert register_manual_trials(conn, first).inserted
    changed = copy.deepcopy(doc)
    changed.update(research_run_id="independent-evaluation", completed_at="2026-01-03T12:00:00Z")
    for trial in changed["trials"]:
        trial["evaluated_at"] = "2026-01-03T11:00:00Z"
    assert register_manual_trials(conn, submission(changed)).inserted
    assert fetch_trial_snapshot(conn, first.batch.family_key).n_trials == 4


def test_unversioned_submission_is_rejected_without_writes(conn, doc):
    s = submission(doc)
    metadata = dict(s.batch.metadata)
    metadata.pop("inventory_hash_version", None)
    stale = replace(s, batch=replace(s.batch, metadata=metadata))
    with pytest.raises(ValueError, match="unsupported inventory hash version"):
        register_manual_trials(conn, stale)
    for table in ("qed_trial_batches", "qed_manual_trial_imports", "qed_manual_trial_evaluations"):
        assert conn.execute(f"SELECT count(*) FROM {table} WHERE batch_id=%s", (s.batch.batch_id,)).fetchone()[0] == 0


def test_future_evidence_rejected_without_partial_writes(conn, doc):
    doc["completed_at"] = (conn.execute("SELECT clock_timestamp()").fetchone()[0] + timedelta(days=1)).isoformat()
    s = submission(doc)
    with pytest.raises(ValueError, match="future"):
        register_manual_trials(conn, s)
    assert conn.execute("SELECT count(*) FROM qed_trial_batches WHERE batch_id=%s", (s.batch.batch_id,)).fetchone()[0] == 0


def test_long_running_transaction_cannot_backdate(conn, doc):
    import psycopg
    s = submission(doc)
    with psycopg.connect(DSN) as c:
        c.execute("SELECT now()")  # establish transaction start before as_of
        before = conn.execute("SELECT clock_timestamp()").fetchone()[0]
        result = register_manual_trials(c, s)
    assert result.recorded_at > before
    assert fetch_trial_snapshot(conn, s.batch.family_key, as_of=before).n_trials == 0


@pytest.mark.parametrize("isolation", ["READ_COMMITTED", "REPEATABLE_READ"])
def test_asof_between_registration_and_commit_is_not_a_frozen_history(conn, doc, isolation):
    """Characterize the existing reader's limitation, not an immutability guarantee."""
    import psycopg
    s = submission(doc)
    with psycopg.connect(DSN) as writer, psycopg.connect(DSN) as reader:
        reader.isolation_level = getattr(psycopg.IsolationLevel, isolation)
        result = register_manual_trials(writer, s)
        cutoff = reader.execute("SELECT clock_timestamp()").fetchone()[0]
        assert result.recorded_at < cutoff
        before = fetch_trial_snapshot(reader, s.batch.family_key, as_of=cutoff)
        assert before.n_trials == 0
        writer.commit()
        same_transaction = fetch_trial_snapshot(reader, s.batch.family_key, as_of=cutoff)
        assert same_transaction.n_trials == (0 if isolation == "REPEATABLE_READ" else 2)
        reader.commit()
        later = fetch_trial_snapshot(reader, s.batch.family_key, as_of=cutoff)
        assert later.n_trials == 2
        assert later.snapshot_hash != before.snapshot_hash
        assert fetch_trial_snapshot(reader, s.batch.family_key,
                                    as_of=result.recorded_at - timedelta(microseconds=1)).n_trials == 0


def test_orphan_existing_batch_is_not_silently_accepted(conn, doc):
    s = submission(doc)
    assert insert_trial_batch(conn, s.batch)
    with pytest.raises(ManualImportConflict, match="without a matching"):
        register_manual_trials(conn, s)


def test_caller_rollback_removes_receipt_and_batch(conn, doc):
    import psycopg
    s = submission(doc)
    with pytest.raises(RuntimeError, match="caller failed"):
        with psycopg.connect(DSN) as c:
            register_manual_trials(c, s)
            raise RuntimeError("caller failed")
    for table in ("qed_trial_batches", "qed_manual_trial_imports", "qed_manual_trial_evaluations"):
        assert conn.execute(f"SELECT count(*) FROM {table} WHERE batch_id=%s", (s.batch.batch_id,)).fetchone()[0] == 0
    assert register_manual_trials(conn, s).inserted


def test_receipts_append_only(conn, doc):
    import psycopg
    result = register_manual_trials(conn, submission(doc))
    for operation in ("UPDATE qed_manual_trial_imports SET n_trials=1", "DELETE FROM qed_manual_trial_imports"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(operation + " WHERE batch_id=%s", (result.batch_id,))


@pytest.mark.parametrize("mode", ["retry", "relabel", "relabel_and_completion_time"])
def test_concurrent_duplicate_registration(conn, doc, mode):
    import psycopg
    docs = [doc, copy.deepcopy(doc)]
    if mode != "retry":
        docs[1]["research_run_id"] += "-other"
    if mode == "relabel_and_completion_time":
        docs[1]["completed_at"] = "2026-01-03T12:00:00Z"
    barrier = Barrier(2)

    def write(d):
        with psycopg.connect(DSN) as c:
            barrier.wait(timeout=10)
            try:
                return register_manual_trials(c, submission(d)).inserted
            except ManualImportConflict:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, docs))
    assert results.count(True) == 1
    assert ("conflict" in results) if mode != "retry" else (results.count(False) == 1)
    assert fetch_trial_snapshot(conn, submission(doc).batch.family_key).n_trials == 2
    for table in ("qed_trial_batches", "qed_manual_trial_imports"):
        assert conn.execute(
            f"SELECT count(*) FROM {table} WHERE batch_id=ANY(%s)",
            ([submission(d).batch.batch_id for d in docs],),
        ).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM qed_manual_trial_evaluations WHERE batch_id=ANY(%s)",
                        ([submission(d).batch.batch_id for d in docs],)).fetchone()[0] == 2


def test_cli_write_and_retry(conn, doc, tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_bytes(raw(doc))
    command = [sys.executable, str(ROOT / "scripts/postgres/register_manual_trials.py"),
               "--evidence", str(evidence), "--n-trials", "2", "--submitted-by", "cli-test", "--write-db"]
    for expected in ("inserted", "already_registered"):
        result = subprocess.run(command, text=True, capture_output=True, check=True)
        assert json.loads(result.stdout)["status"] == expected


@pytest.mark.parametrize("repack", ["subset", "superset", "replacement", "equivalent_row"])
def test_repackaged_inventory_cannot_recount_an_existing_evaluation(conn, doc, repack):
    original = submission(doc)
    register_manual_trials(conn, original)
    changed = copy.deepcopy(doc)
    changed.update(source="other-desk", research_run_id=uuid.uuid4().hex,
                   completed_at="2026-01-03T12:00:00Z")
    new_trial = dict(changed["trials"][1], formula="new-evaluation")
    if repack == "subset":
        changed["trials"] = changed["trials"][:1]
        changed["n_trials"] = 1
    elif repack == "superset":
        changed["trials"].append(new_trial)
        changed["n_trials"] = 3
    else:
        changed["trials"][1] = new_trial
        if repack == "equivalent_row":
            changed["trials"][0].update(formula="r1+r5",
                evaluated_at="2026-01-02T20:00:00+09:00",
                data_cutoff="2026-01-01T09:00:00+09:00")
    duplicate = submission(changed)
    assert duplicate.inventory_sha256 != original.inventory_sha256
    with pytest.raises(ManualImportConflict, match="already registered"):
        register_manual_trials(conn, duplicate)
    assert fetch_trial_snapshot(conn, original.batch.family_key).n_trials == 2
    for table in ("qed_trial_batches", "qed_manual_trial_imports"):
        assert conn.execute(f"SELECT count(*) FROM {table} WHERE batch_id=%s",
                            (duplicate.batch.batch_id,)).fetchone()[0] == 0


def test_concurrent_partially_overlapping_inventories_are_atomic(conn, doc):
    import psycopg
    other = copy.deepcopy(doc)
    other["research_run_id"] += "-other"
    other["trials"][1]["formula"] = "different-formula"
    gate = Barrier(2)

    def write(d):
        with psycopg.connect(DSN) as c:
            gate.wait(timeout=10)
            try:
                return register_manual_trials(c, submission(d)).inserted
            except ManualImportConflict:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, (doc, other)))
    assert sorted(map(str, results)) == ["True", "conflict"]
    assert fetch_trial_snapshot(conn, submission(doc).batch.family_key).n_trials == 2
    submissions = [submission(d) for d in (doc, other)]
    ids = [s.batch.batch_id for s in submissions]
    for table in ("qed_trial_batches", "qed_manual_trial_imports"):
        assert conn.execute(f"SELECT count(*) FROM {table} WHERE batch_id=ANY(%s)",
                            (ids,)).fetchone()[0] == 1
    rows = conn.execute("SELECT evaluation_sha256,batch_id FROM qed_manual_trial_evaluations "
                        "WHERE batch_id=ANY(%s) ORDER BY evaluation_sha256", (ids,)).fetchall()
    assert len(rows) == 2 and len({r[1] for r in rows}) == 1
    winner = next(s for s in submissions if s.batch.batch_id == rows[0][1])
    assert tuple(r[0] for r in rows) == winner.evaluation_sha256s


def test_exact_retry_is_idempotent_with_sql_ascii_text_results(conn, doc):
    import psycopg
    s = submission(doc)
    first = register_manual_trials(conn, s)
    with psycopg.connect(DSN, autocommit=True,
                         options="-cclient_encoding=SQL_ASCII") as ascii_conn:
        repeated = register_manual_trials(ascii_conn, s)
    assert not repeated.inserted
    assert repeated.batch_id == first.batch_id and repeated.recorded_at == first.recorded_at


def test_v2_receipt_without_evaluation_registry_requires_audit(conn, doc):
    s = submission(doc)
    metadata = dict(s.batch.metadata)
    metadata.pop("evaluation_hash_version", None)
    metadata.pop("evaluation_rows", None)
    assert insert_trial_batch(conn, replace(s.batch, metadata=metadata))
    conn.execute(
        "INSERT INTO qed_manual_trial_imports "
        "(source,research_run_id,batch_id,evidence_sha256,inventory_sha256,n_trials,"
        "completed_at,submitted_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (s.source, s.research_run_id, s.batch.batch_id, s.evidence_sha256,
         s.inventory_sha256, s.batch.n_trials, s.completed_at, s.submitted_by))
    other = copy.deepcopy(doc)
    other["research_run_id"] += "-other"
    other["trials"][0]["formula"] = "new-formula"
    with pytest.raises(ManualImportConflict, match="audit"):
        register_manual_trials(conn, submission(other))


def test_evaluation_insert_failure_rolls_back_all_registration_rows(conn, doc):
    import psycopg
    s = submission(doc)
    # The deliberate failure occurs after the batch and receipt INSERTs. All
    # test DDL is rolled back too, even when an assertion fails unexpectedly.
    with conn.transaction(force_rollback=True):
        conn.execute("CREATE FUNCTION pg_temp.reject_evaluation_test() RETURNS trigger "
                     "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'evaluation insert failed'; END $$")
        conn.execute("CREATE TRIGGER test_reject_evaluation BEFORE INSERT ON qed_manual_trial_evaluations "
                     "FOR EACH ROW EXECUTE FUNCTION pg_temp.reject_evaluation_test()")
        with pytest.raises(psycopg.errors.RaiseException, match="evaluation insert failed"):
            register_manual_trials(conn, s)
        for table in ("qed_trial_batches", "qed_manual_trial_imports", "qed_manual_trial_evaluations"):
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE batch_id=%s",
                                (s.batch.batch_id,)).fetchone()[0] == 0
    assert register_manual_trials(conn, s).inserted


@pytest.mark.parametrize("operation", [
    "UPDATE qed_manual_trial_evaluations SET evaluation_sha256=evaluation_sha256 WHERE batch_id=%s",
    "DELETE FROM qed_manual_trial_evaluations WHERE batch_id=%s",
    "TRUNCATE qed_manual_trial_evaluations",
])
def test_evaluation_registry_is_append_only(conn, doc, operation):
    import psycopg
    s = submission(doc)
    register_manual_trials(conn, s)
    with conn.transaction(force_rollback=True):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(operation, (s.batch.batch_id,) if "%s" in operation else None)
    rows = conn.execute("SELECT evaluation_sha256 FROM qed_manual_trial_evaluations "
                        "WHERE batch_id=%s ORDER BY evaluation_sha256", (s.batch.batch_id,)).fetchall()
    assert tuple(r[0] for r in rows) == s.evaluation_sha256s


def test_registry_migration_reapplication_preserves_receipts_and_evaluations(conn, doc):
    s = submission(doc)
    register_manual_trials(conn, s)
    before = conn.execute("SELECT evaluation_sha256,batch_id FROM qed_manual_trial_evaluations "
                          "WHERE batch_id=%s ORDER BY evaluation_sha256", (s.batch.batch_id,)).fetchall()
    for _ in range(2):
        conn.execute((ROOT / "qedschema/migrations/089_qed_manual_trial_evaluations.sql").read_text())
    assert conn.execute("SELECT evaluation_sha256,batch_id FROM qed_manual_trial_evaluations "
                        "WHERE batch_id=%s ORDER BY evaluation_sha256", (s.batch.batch_id,)).fetchall() == before
    assert not register_manual_trials(conn, s).inserted
