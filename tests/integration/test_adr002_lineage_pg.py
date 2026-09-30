"""
ADR-002 試行台帳の実 PostgreSQL 統合テスト。

QED_PG_DSN が未設定ならスキップ。migration 084 は冪等に適用する。
台帳は append-only のため、テストデータは実行ごとに一意な family_key / run_id で隔離する。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DSN = os.environ.get("QED_PG_DSN", "")
pytestmark = [
    pytest.mark.adr002_lineage,
    pytest.mark.skipif(not DSN, reason="QED_PG_DSN 環境変数が設定されていないため統合テストをスキップ"),
]

MIGRATION = Path(__file__).resolve().parents[2] / "qedschema/migrations/084_qed_trial_ledger.sql"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def conn():
    psycopg = pytest.importorskip("psycopg")
    c = psycopg.connect(DSN, autocommit=True)
    c.execute("CREATE TABLE IF NOT EXISTS _migrations(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())")
    c.execute(MIGRATION.read_text(encoding="utf-8"))
    yield c
    c.close()


@pytest.fixture
def fams():
    from analytics.python.frost.frost_lineage import make_family_key
    tag = uuid.uuid4().hex
    return [make_family_key("5d", "IT", "fwd", f"{tag}-{i}") for i in range(3)]


def test_insert_idempotent_and_snapshot(conn, fams):
    from analytics.python.frost.frost_lineage import LineageEdge, TrialBatch
    from analytics.python.pg_io.postgres_lineage_bridge import (
        fetch_trial_snapshot, insert_lineage_edge, insert_trial_batch,
    )
    a, b, c = fams
    run = uuid.uuid4().hex
    b1 = TrialBatch.create(a, run + "a", "exhaustive", 1000, sharpes=[0.01, 0.02, 0.04], recorded_at=T0)
    b2 = TrialBatch.create(b, run + "b", "gradient", 10, sharpes=[0.05, 0.03], recorded_at=T0)
    b3 = TrialBatch.create(c, run + "c", "manual", 1, recorded_at=T0)
    late = TrialBatch.create(a, run + "late", "exhaustive", 5000, recorded_at=T0 + timedelta(days=30))
    assert all(insert_trial_batch(conn, x) for x in (b1, b2, b3, late))
    assert insert_trial_batch(conn, b1) is False
    assert insert_lineage_edge(conn, LineageEdge.create(a, b, "mutation", recorded_at=T0))
    assert insert_lineage_edge(conn, LineageEdge.create(b, c, "retrain", recorded_at=T0))

    s = fetch_trial_snapshot(conn, c, as_of=T0 + timedelta(days=1))
    assert s.n_trials == 1011
    assert s.sr_stats.count == 5
    assert fetch_trial_snapshot(conn, c).n_trials == 6011
    assert fetch_trial_snapshot(conn, a, as_of=T0 + timedelta(days=1)).n_trials == 1000
    assert s.snapshot_hash == fetch_trial_snapshot(conn, c, as_of=T0 + timedelta(days=1)).snapshot_hash


def test_append_only_enforced(conn, fams):
    import psycopg
    from analytics.python.frost.frost_lineage import TrialBatch
    from analytics.python.pg_io.postgres_lineage_bridge import insert_trial_batch
    b = TrialBatch.create(fams[0], uuid.uuid4().hex, "manual", 1, recorded_at=T0)
    insert_trial_batch(conn, b)
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE qed_trial_batches SET n_trials = 0 WHERE batch_id = %s", (b.batch_id,))
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM qed_trial_batches WHERE batch_id = %s", (b.batch_id,))


def test_check_constraints(conn):
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO qed_trial_batches(batch_id, family_key, run_id, stage, n_trials, sr_count) "
            "VALUES (%s, 'fam:x', 'r', 'exhaustive', 1, 5)", (uuid.uuid4().hex,))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO qed_trial_batches(batch_id, family_key, run_id, stage, n_trials) "
            "VALUES (%s, 'fam:x', 'r', 'bogus', 1)", (uuid.uuid4().hex,))
