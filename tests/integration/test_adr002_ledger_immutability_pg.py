"""ADR-002 append-only audit; every DDL/data operation is rolled back.

The pre-existing public ledger is never committed by these tests. Even an
unexpectedly successful TRUNCATE stays inside a rollback-only savepoint and the
connection's outer transaction is unconditionally rolled back in fixture cleanup.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest


DSN = os.environ.get("QED_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="QED_PG_DSN not configured")
ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "qedschema/migrations/088_qed_trial_ledger_no_truncate.sql"
TABLES = ("qed_trial_batches", "qed_lineage_edges", "qed_manual_trial_imports")


def ledger_rows(conn):
    # Static identifiers only; compare full values, not just sentinel counts.
    return {
        table: conn.execute(f"SELECT to_jsonb(t) FROM {table} t ORDER BY to_jsonb(t)::text").fetchall()
        for table in TABLES
    }


@pytest.fixture
def conn():
    import psycopg

    c = psycopg.connect(DSN)
    try:
        c.execute("SET LOCAL lock_timeout = '10s'")
        c.execute("CREATE TABLE IF NOT EXISTS _migrations(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())")
        for name in ("084_qed_trial_ledger.sql", "086_qed_manual_trial_imports.sql"):
            c.execute((ROOT / "qedschema/migrations" / name).read_text(encoding="utf-8"))
        c.execute(MIGRATION.read_text(encoding="utf-8"))
        tag = uuid.uuid4().hex
        c.execute(
            "INSERT INTO qed_trial_batches(batch_id,family_key,run_id,stage,n_trials) "
            "VALUES (%s,%s,%s,'manual',2)", (tag, "fam:" + tag, tag),
        )
        c.execute(
            "INSERT INTO qed_lineage_edges(edge_id,parent_family_key,child_family_key,relation) "
            "VALUES (%s,%s,%s,'manual_edit')", (tag, "fam:" + tag, "fam:" + uuid.uuid4().hex),
        )
        c.execute(
            "INSERT INTO qed_manual_trial_imports(source,research_run_id,batch_id,evidence_sha256,"
            "inventory_sha256,n_trials,completed_at,submitted_by) "
            "VALUES (%s,%s,%s,%s,%s,2,'2000-01-01T00:00:00Z','immutability-audit')",
            (tag, tag, tag, uuid.uuid4().hex * 2, uuid.uuid4().hex * 2),
        )
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("cascade", [False, True])
def test_each_table_rejects_truncate_without_losing_existing_values(conn, table, cascade):
    import psycopg

    before = ledger_rows(conn)
    # A parent-only TRUNCATE may be rejected by a receipt/evaluation FK before
    # triggers. The evaluation registry is optional for this 088 migration audit.
    expected = (psycopg.errors.RaiseException, psycopg.errors.FeatureNotSupported)
    with conn.transaction(force_rollback=True):
        with pytest.raises(expected) as exc:
            conn.execute(f"TRUNCATE TABLE {table}" + (" CASCADE" if cascade else ""))
        if isinstance(exc.value, psycopg.errors.RaiseException):
            assert "append-only" in str(exc.value)
        else:
            assert table in ("qed_trial_batches", "qed_manual_trial_imports") and not cascade
            assert "foreign key" in str(exc.value)
    assert ledger_rows(conn) == before


@pytest.mark.parametrize("statement", [
    "TRUNCATE TABLE qed_trial_batches, qed_manual_trial_imports CASCADE",
    "TRUNCATE TABLE qed_manual_trial_imports, qed_trial_batches, qed_lineage_edges CASCADE",
])
def test_multi_table_truncate_is_rejected_atomically(conn, statement):
    import psycopg

    before = ledger_rows(conn)
    with conn.transaction(force_rollback=True):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(statement)
    assert ledger_rows(conn) == before


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("operation", ["update", "delete"])
def test_update_and_delete_remain_rejected(conn, table, operation):
    import psycopg

    before = ledger_rows(conn)
    # Self-assignment exercises row triggers without changing any value.
    key = {"qed_trial_batches": "batch_id", "qed_lineage_edges": "edge_id",
           "qed_manual_trial_imports": "source"}[table]
    statement = f"UPDATE {table} SET {key}={key}" if operation == "update" else f"DELETE FROM {table}"
    with conn.transaction(force_rollback=True):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(statement)
    assert ledger_rows(conn) == before


def test_migration_reapplication_preserves_rows_and_statement_triggers(conn):
    before = ledger_rows(conn)
    for _ in range(2):
        # Reapplying the prerequisite migrations must not remove 088 protection.
        for name in ("084_qed_trial_ledger.sql", "086_qed_manual_trial_imports.sql",
                     "088_qed_trial_ledger_no_truncate.sql"):
            conn.execute((ROOT / "qedschema/migrations" / name).read_text(encoding="utf-8"))
    assert ledger_rows(conn) == before
    triggers = conn.execute(
        "SELECT c.relname,t.tgtype FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relname=ANY(%s) AND NOT t.tgisinternal "
        "AND t.tgtype & 32 <> 0 ORDER BY c.relname", (list(TABLES),),
    ).fetchall()
    # PostgreSQL bit 32 = TRUNCATE; bit 2 = BEFORE; no ROW bit (1).
    assert triggers == [(table, 34) for table in sorted(TABLES)]
    assert conn.execute("SELECT count(*) FROM _migrations WHERE filename=%s", (MIGRATION.name,)).fetchone()[0] == 1


def test_migration_has_no_snapshot_capture_dependency(conn):
    before = ledger_rows(conn)
    # Rollback-only DDL proves 087 is optional without discarding its real data.
    with conn.transaction(force_rollback=True):
        conn.execute("DROP TABLE IF EXISTS qed_trial_snapshot_captures")
        conn.execute(MIGRATION.read_text(encoding="utf-8"))
        assert conn.execute("SELECT to_regclass('public.qed_trial_snapshot_captures')").fetchone()[0] is None
        assert ledger_rows(conn) == before


@pytest.mark.parametrize("missing", ["qed_lineage_edges", "qed_manual_trial_imports"])
def test_migration_requires_084_and_086(conn, missing):
    import psycopg

    before = ledger_rows(conn)
    with conn.transaction(force_rollback=True):
        conn.execute(f"DROP TABLE {missing} CASCADE")
        with pytest.raises(psycopg.errors.UndefinedTable):
            conn.execute(MIGRATION.read_text(encoding="utf-8"))
    assert ledger_rows(conn) == before
