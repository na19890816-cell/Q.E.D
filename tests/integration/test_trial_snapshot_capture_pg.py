"""Frozen audit inputs across late commits, concurrent captures and replay."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
import uuid

import pytest
import psycopg

from analytics.python.frost.frost_lineage import LineageEdge, TrialBatch, make_family_key
from analytics.python.pg_io.postgres_lineage_bridge import (
    fetch_trial_snapshot, insert_lineage_edge, insert_trial_batch,
)
from analytics.python.pg_io.postgres_manual_trial_bridge import register_manual_trials
from analytics.python.frost.manual_trial_ledger import parse_manual_evidence
from analytics.python.pg_io import postgres_trial_snapshot_capture as captures
from tests.unit.test_adr002_s3_manual_ledger import evidence_doc, raw

DSN = os.environ.get("QED_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="QED_PG_DSN not configured")
ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("CREATE TABLE IF NOT EXISTS _migrations(filename TEXT PRIMARY KEY)")
        for _ in range(2):
            for name in ("084_qed_trial_ledger.sql", "086_qed_manual_trial_imports.sql",
                         "087_qed_trial_snapshot_captures.sql"):
                c.execute((ROOT / "qedschema/migrations" / name).read_text())
        yield c


@pytest.fixture
def family():
    return make_family_key("5d", uuid.uuid4().hex, "fwd")


def pending_submission():
    doc = evidence_doc()
    doc["source"] = uuid.uuid4().hex
    doc["family"]["universe"] = uuid.uuid4().hex
    return parse_manual_evidence(raw(doc), 2, "capture-test")


@pytest.mark.parametrize("commit", [True, False])
def test_capture_remains_fixed_after_delayed_writer_finishes(conn, commit):
    submission = pending_submission()
    family = submission.batch.family_key
    with psycopg.connect(DSN) as writer:
        registered = register_manual_trials(writer, submission)
        cutoff = conn.execute("SELECT clock_timestamp()").fetchone()[0]
        assert registered.recorded_at < cutoff
        first = captures.capture_trial_snapshot(conn, family, cutoff)
        assert first.snapshot.n_trials == 0 and first.snapshot.batch_ids == ()
        writer.commit() if commit else writer.rollback()
    assert fetch_trial_snapshot(conn, family, as_of=cutoff).n_trials == (2 if commit else 0)
    repeated = captures.capture_trial_snapshot(conn, family, cutoff)
    replay = captures.get_snapshot_capture(conn, first.capture_id)
    assert first.to_dict() == repeated.to_dict() == replay.to_dict()
    later_cutoff = conn.execute("SELECT clock_timestamp()").fetchone()[0]
    later = captures.capture_trial_snapshot(conn, family, later_cutoff)
    assert later.capture_id != first.capture_id
    assert later.snapshot.n_trials == (2 if commit else 0)


def test_competing_first_captures_replay_one_committed_winner(conn, family, monkeypatch):
    batch = TrialBatch.create(family, uuid.uuid4().hex, "exhaustive", 2,
                              sharpes=[0.1, 0.3], recorded_at=T0)
    assert insert_trial_batch(conn, batch)
    gate = Barrier(2)
    real_fetch = captures.fetch_trial_snapshot
    calls = []

    def synchronized_fetch(*args, **kwargs):
        calls.append(1)
        gate.wait(timeout=10)  # both initial receipt reads have observed absence
        return real_fetch(*args, **kwargs)

    monkeypatch.setattr(captures, "fetch_trial_snapshot", synchronized_fetch)

    def capture(_):
        with psycopg.connect(DSN, autocommit=True) as c:
            return captures.capture_trial_snapshot(c, family, T0, "daily")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(capture, range(2)))
    assert len(calls) == 2
    assert first.to_dict() == second.to_dict()
    assert first.snapshot.n_trials == 2
    assert first.snapshot.batch_ids == (batch.batch_id,)
    assert conn.execute("SELECT count(*) FROM qed_trial_snapshot_captures WHERE capture_id=%s",
                        (first.capture_id,)).fetchone()[0] == 1


def test_capture_uses_one_visibility_snapshot_across_ledger_reads(conn, monkeypatch):
    submission = pending_submission()
    family = submission.batch.family_key
    real_fetch = captures.fetch_trial_snapshot
    isolation_before = conn.execute("SHOW transaction_isolation").fetchone()[0]
    with psycopg.connect(DSN) as writer:
        register_manual_trials(writer, submission)
        cutoff = conn.execute("SELECT clock_timestamp()").fetchone()[0]

        def commit_before_aggregation(*args, **kwargs):
            writer.commit()  # after capture's initial lookup fixed its MVCC snapshot
            return real_fetch(*args, **kwargs)

        monkeypatch.setattr(captures, "fetch_trial_snapshot", commit_before_aggregation)
        frozen = captures.capture_trial_snapshot(conn, family, cutoff)
    assert frozen.snapshot.n_trials == 0
    assert fetch_trial_snapshot(conn, family, as_of=cutoff).n_trials == 2
    assert conn.execute("SHOW transaction_isolation").fetchone()[0] == isolation_before


@pytest.mark.parametrize("ancestry", [True, False])
def test_capture_round_trips_inputs_statistics_and_query_options(conn, family, ancestry):
    parent = make_family_key("5d", uuid.uuid4().hex, "fwd")
    for fam, count, sharpes, period in ((parent, 2, [0.1, 0.3], "daily"),
                                       (family, 3, [0.2, 0.4, 0.6], "weekly")):
        assert insert_trial_batch(conn, TrialBatch.create(
            fam, uuid.uuid4().hex, "exhaustive", count, sharpes=sharpes,
            sr_periodicity=period, recorded_at=T0))
    assert insert_lineage_edge(conn, LineageEdge.create(parent, family, "mutation", recorded_at=T0))
    expected = fetch_trial_snapshot(conn, family, T0, "daily", ancestry)
    frozen = captures.capture_trial_snapshot(conn, family, T0, "daily", ancestry)
    assert frozen.snapshot == expected
    assert frozen.snapshot.n_trials == (5 if ancestry else 3)
    assert frozen.snapshot.sr_stats.count == (2 if ancestry else 0)
    assert frozen.snapshot.excluded_periodicity_batches == 1
    assert captures.get_snapshot_capture(conn, frozen.capture_id).to_dict() == frozen.to_dict()
    equivalent = captures.capture_trial_snapshot(
        conn, family, T0.astimezone(timezone(timedelta(hours=9))), "daily", ancestry)
    assert equivalent.capture_id == frozen.capture_id
    unfiltered = captures.capture_trial_snapshot(conn, family, T0, None, ancestry)
    assert unfiltered.capture_id != frozen.capture_id


@pytest.mark.parametrize("operation", ["UPDATE qed_trial_snapshot_captures SET payload='{}'",
                                       "DELETE FROM qed_trial_snapshot_captures"])
def test_capture_receipts_are_append_only(conn, family, operation):
    frozen = captures.capture_trial_snapshot(conn, family, T0)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        conn.execute(operation + " WHERE capture_id=%s", (frozen.capture_id,))
    assert captures.get_snapshot_capture(conn, frozen.capture_id).to_dict() == frozen.to_dict()


def test_missing_capture_never_falls_back_to_live_ledger(conn):
    capture_id = "capture:" + hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    with pytest.raises(LookupError, match="not found"):
        captures.get_snapshot_capture(conn, capture_id)


def test_capture_requires_idle_autocommit_connection(conn, family):
    with psycopg.connect(DSN) as c:
        with pytest.raises(ValueError, match="idle autocommit"):
            captures.capture_trial_snapshot(c, family, T0)
        assert c.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_capture_does_not_commit_or_roll_back_callers_transaction(conn, family):
    batch = TrialBatch.create(family, uuid.uuid4().hex, "manual", 2, recorded_at=T0)
    with psycopg.connect(DSN, autocommit=True) as c:
        with pytest.raises(RuntimeError, match="caller abort"):
            with c.transaction():
                insert_trial_batch(c, batch)
                with pytest.raises(ValueError, match="idle autocommit"):
                    captures.capture_trial_snapshot(c, family, T0)
                assert conn.execute("SELECT count(*) FROM qed_trial_batches WHERE batch_id=%s",
                                    (batch.batch_id,)).fetchone()[0] == 0
                raise RuntimeError("caller abort")
    assert fetch_trial_snapshot(conn, family, as_of=T0).n_trials == 0


def test_capture_rejects_future_cutoff(conn, family):
    future = conn.execute("SELECT clock_timestamp()").fetchone()[0] + timedelta(days=1)
    with pytest.raises(ValueError, match="future"):
        captures.capture_trial_snapshot(conn, family, future)
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_capture_failure_rolls_back_and_can_be_retried(conn, family, monkeypatch):
    real_fetch = captures.fetch_trial_snapshot

    def fail(*args, **kwargs):
        raise ValueError("unusable ledger input")

    monkeypatch.setattr(captures, "fetch_trial_snapshot", fail)
    with pytest.raises(ValueError, match="unusable ledger"):
        captures.capture_trial_snapshot(conn, family, T0)
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    monkeypatch.setattr(captures, "fetch_trial_snapshot", real_fetch)
    frozen = captures.capture_trial_snapshot(conn, family, T0)
    assert frozen.snapshot.n_trials == 0


def test_replay_rejects_corrupted_payload_instead_of_recalculating(conn):
    capture_id = "capture:" + hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    conn.execute("INSERT INTO qed_trial_snapshot_captures(capture_id,payload_sha256,payload) "
                 "VALUES (%s,%s,'{}')", (capture_id, "0" * 64))
    with pytest.raises(ValueError, match="checksum"):
        captures.get_snapshot_capture(conn, capture_id)


def test_cli_capture_and_show_return_the_same_saved_inputs(conn, family):
    command = [sys.executable, str(ROOT / "scripts/postgres/capture_trial_snapshot.py")]
    def run(*args):
        p = subprocess.run(command + list(args), capture_output=True, text=True, check=True)
        return json.loads(p.stdout)
    first = run("capture", "--family-key", family, "--as-of", T0.isoformat())
    assert first["snapshot"]["n_trials"] == 0
    assert first["snapshot"]["batch_ids"] == []
    assert insert_trial_batch(conn, TrialBatch.create(
        family, uuid.uuid4().hex, "manual", 2, recorded_at=T0))
    assert fetch_trial_snapshot(conn, family, T0).n_trials == 2
    assert run("show", "--capture-id", first["capture_id"]) == first
    assert run("capture", "--family-key", family, "--as-of", T0.isoformat()) == first


def test_missing_migration_fails_without_live_fallback(conn, family):
    with psycopg.connect(DSN, autocommit=True, options="-csearch_path=pg_catalog") as c:
        with pytest.raises(psycopg.errors.UndefinedTable):
            captures.capture_trial_snapshot(c, family, T0)
        assert c.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
