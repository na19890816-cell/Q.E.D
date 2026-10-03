"""
全 migration を実 PostgreSQL へ 2 回適用できること (冪等性) の統合テスト。

QED_MIGRATION_TEST_DSN が未設定ならスキップ (既存 DB を汚さないよう専用の空 DB を指定すること)。
QED 本体側テーブル (experiment_runs / audit_events / update_updated_at_column) は
本リポジトリ外のため最小スタブを作成する。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

DSN = os.environ.get("QED_MIGRATION_TEST_DSN", "")
pytestmark = [
    pytest.mark.p1_repairs,
    pytest.mark.skipif(not DSN, reason="QED_MIGRATION_TEST_DSN 未設定のため migration 適用テストをスキップ"),
]
MIG = Path(__file__).resolve().parents[2] / "qedschema" / "migrations"

STUBS = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS _migrations(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now());
CREATE OR REPLACE FUNCTION update_updated_at_column() RETURNS trigger AS $f$
BEGIN NEW.updated_at = now(); RETURN NEW; END $f$ LANGUAGE plpgsql;
CREATE TABLE IF NOT EXISTS experiment_runs(id UUID PRIMARY KEY DEFAULT gen_random_uuid());
CREATE TABLE IF NOT EXISTS audit_events(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  trace_id TEXT, case_id TEXT, object_type TEXT, object_id TEXT, requested_by TEXT,
  event_type TEXT, decision TEXT, decision_reason_code TEXT, reject_reason_code TEXT,
  metadata JSONB DEFAULT '{}', created_at TIMESTAMPTZ DEFAULT now());
"""


def test_all_migrations_apply_twice():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute(STUBS)
        for _ in range(2):
            for f in sorted(MIG.glob("*.sql")):
                try:
                    c.execute(f.read_text(encoding="utf-8"))
                except Exception as e:  # pragma: no cover - 失敗時の診断
                    pytest.fail(f"{f.name}: {e}")
        n = c.execute("SELECT count(*) FROM pg_matviews").fetchone()[0]
        assert n >= 3
        c.execute("SELECT frost_log_table_sizes()")
