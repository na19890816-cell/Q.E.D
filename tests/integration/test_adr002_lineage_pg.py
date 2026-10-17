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


@pytest.mark.parametrize("autocommit", [False, True])
def test_one_read_never_mixes_ancestry_before_and_after_commit(conn, fams, autocommit):
    import psycopg
    from analytics.python.frost.frost_lineage import LineageEdge, TrialBatch
    from analytics.python.pg_io.postgres_lineage_bridge import (
        fetch_trial_snapshot, insert_lineage_edge, insert_trial_batch,
    )
    parent, child, _ = fams
    p = TrialBatch.create(parent, uuid.uuid4().hex, "exhaustive", 100, recorded_at=T0)
    c = TrialBatch.create(child, uuid.uuid4().hex, "manual", 10, recorded_at=T0)
    assert insert_trial_batch(conn, p) and insert_trial_batch(conn, c)
    with psycopg.connect(DSN) as writer:
        assert insert_lineage_edge(writer, LineageEdge.create(parent, child, "mutation", recorded_at=T0))
        fired = False

        class CommitAfterFirstRead(psycopg.Cursor):
            def execute(self, query, params=None, **kwargs):
                nonlocal fired
                result = super().execute(query, params, **kwargs)
                if isinstance(query, str) and "WITH RECURSIVE" in query and not fired:
                    fired = True
                    writer.commit()  # first result is ready; subsequent statements would see this edge
                return result

        with psycopg.connect(DSN, autocommit=autocommit, cursor_factory=CommitAfterFirstRead) as reader:
            first = fetch_trial_snapshot(reader, child, as_of=T0)
            assert fired
            assert first.n_trials == 10
            assert first.families == (child,)
            assert first.batch_ids == (c.batch_id,)
            assert reader.info.transaction_status == (
                psycopg.pq.TransactionStatus.IDLE if autocommit else psycopg.pq.TransactionStatus.INTRANS)
            later = fetch_trial_snapshot(reader, child, as_of=T0)
            assert later.n_trials == 110
            assert set(later.families) == {parent, child}


def test_database_reader_matches_pure_ledger_across_history_and_filters(conn, fams):
    from analytics.python.frost.frost_lineage import LineageEdge, TrialBatch, TrialLedger, make_family_key
    from analytics.python.pg_io.postgres_lineage_bridge import (
        fetch_trial_snapshot, insert_lineage_edge, insert_trial_batch, load_ledger_for_family,
    )
    a, b, c = fams
    d = make_family_key("5d", uuid.uuid4().hex, "fwd")
    batches = [
        TrialBatch.create(fam, "研究-" + uuid.uuid4().hex, "exhaustive", 10 + i,
                          sharpes=[0.12345678901234568, -0.23456789012345678],
                          sr_periodicity="daily" if i % 2 else "weekly",
                          metadata={"nested": ["日本株", 1.25, None]},
                          family_spec={"target": "日本語"},
                          recorded_at=T0 + timedelta(days=2 if fam == d else 0))
        for i, fam in enumerate((a, b, c, d))
    ]
    edges = [LineageEdge.create(x, y, "mutation", recorded_at=T0)
             for x, y in ((a, b), (b, c), (c, a))]
    edges.append(LineageEdge.create(d, c, "retrain", recorded_at=T0 + timedelta(days=2)))
    assert all(insert_trial_batch(conn, batch) for batch in batches)
    assert all(insert_lineage_edge(conn, edge) for edge in edges)
    loaded = load_ledger_for_family(conn, c)
    assert {batch.batch_id: batch.to_row() for batch in loaded.batches} == {
        batch.batch_id: batch.to_row() for batch in batches}
    assert {edge.edge_id: edge.to_row() for edge in loaded.edges} == {
        edge.edge_id: edge.to_row() for edge in edges}
    reference = TrialLedger(batches, edges)
    for family in (a, b, c, d):
        for cutoff in (None, T0 + timedelta(days=1)):
            for ancestry in (True, False):
                for periodicity in (None, "daily", "weekly"):
                    context = dict(as_of=cutoff, sr_periodicity=periodicity, include_ancestors=ancestry)
                    expected = reference.snapshot(family, **context)
                    actual = fetch_trial_snapshot(conn, family, **context)
                    assert actual.to_dict() == expected.to_dict(), (family, context)
                    assert actual.batch_ids == expected.batch_ids, (family, context)


def test_reader_keeps_callers_uncommitted_writes_and_rollback(conn, fams):
    import psycopg
    from analytics.python.frost.frost_lineage import TrialBatch
    from analytics.python.pg_io.postgres_lineage_bridge import fetch_trial_snapshot, insert_trial_batch
    family = fams[0]
    batch = TrialBatch.create(family, uuid.uuid4().hex, "manual", 2, recorded_at=T0)
    with psycopg.connect(DSN) as owner:
        assert insert_trial_batch(owner, batch)
        before = owner.execute("SHOW transaction_isolation").fetchone()[0]
        result = fetch_trial_snapshot(owner, family, as_of=T0)
        assert result.n_trials == 2
        assert owner.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
        assert owner.execute("SHOW transaction_isolation").fetchone()[0] == before
        assert fetch_trial_snapshot(conn, family, as_of=T0).n_trials == 0
        owner.rollback()
    assert fetch_trial_snapshot(conn, family, as_of=T0).n_trials == 0


def test_naive_asof_uses_the_cores_utc_meaning_in_non_utc_session(conn, fams):
    import psycopg
    from analytics.python.frost.frost_lineage import LineageEdge, TrialBatch
    from analytics.python.pg_io.postgres_lineage_bridge import (
        fetch_trial_snapshot, insert_lineage_edge, insert_trial_batch,
    )
    parent, child, _ = fams
    assert insert_trial_batch(conn, TrialBatch.create(
        parent, uuid.uuid4().hex, "exhaustive", 100, recorded_at=T0))
    assert insert_trial_batch(conn, TrialBatch.create(
        child, uuid.uuid4().hex, "manual", 10, recorded_at=T0))
    assert insert_lineage_edge(conn, LineageEdge.create(
        parent, child, "mutation", recorded_at=T0 + timedelta(hours=18)))
    naive = datetime(2026, 1, 2)
    with psycopg.connect(DSN, autocommit=True, options="-cTimeZone=Asia/Tokyo") as reader:
        expected = fetch_trial_snapshot(reader, child, as_of=naive.replace(tzinfo=timezone.utc))
        actual = fetch_trial_snapshot(reader, child, as_of=naive)
        assert expected.n_trials == 110
        assert actual.to_dict() == expected.to_dict()
        assert actual.batch_ids == expected.batch_ids


def test_direct_only_reader_does_not_depend_on_an_edge_table(conn, fams):
    import psycopg
    from analytics.python.frost.frost_lineage import TrialBatch
    from analytics.python.pg_io.postgres_lineage_bridge import fetch_trial_snapshot, insert_trial_batch
    family = fams[0]
    with psycopg.connect(DSN, autocommit=True) as reader:
        reader.execute("CREATE TEMP TABLE qed_trial_batches (LIKE public.qed_trial_batches INCLUDING ALL)")
        reader.execute("SET search_path TO pg_temp,pg_catalog")
        batch = TrialBatch.create(family, uuid.uuid4().hex, "manual", 2, recorded_at=T0)
        assert insert_trial_batch(reader, batch)
        result = fetch_trial_snapshot(reader, family, as_of=T0, include_ancestors=False)
        assert result.n_trials == 2 and result.batch_ids == (batch.batch_id,)
        with pytest.raises(psycopg.errors.UndefinedTable):
            fetch_trial_snapshot(reader, family, as_of=T0, include_ancestors=True)
