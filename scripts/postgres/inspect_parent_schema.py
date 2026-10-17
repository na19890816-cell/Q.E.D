#!/usr/bin/env python3
"""Read-only catalog evidence for external integration prerequisites; never create DDL."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

import psycopg


TABLES = ("audit_events", "evidence_bundles", "experiment_runs", "factor_candidates", "hypotheses")


def inspect(conn, schema: str) -> dict:
    tables = {}
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        for table in TABLES:
            relation = conn.execute(
                "SELECT c.oid,c.relkind FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND c.relname=%s", (schema, table),
            ).fetchone()
            if relation is None:
                tables[table] = None
                continue
            oid, kind = relation
            columns = conn.execute(
                "SELECT a.attname,pg_catalog.format_type(a.atttypid,a.atttypmod),a.attnotnull,"
                "pg_catalog.pg_get_expr(d.adbin,d.adrelid),a.attidentity,a.attgenerated "
                "FROM pg_catalog.pg_attribute a LEFT JOIN pg_catalog.pg_attrdef d "
                "ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
                "WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum", (oid,),
            ).fetchall()
            constraints = conn.execute(
                "SELECT conname,contype,pg_catalog.pg_get_constraintdef(oid,true) "
                "FROM pg_catalog.pg_constraint WHERE conrelid=%s ORDER BY conname", (oid,),
            ).fetchall()
            indexes = conn.execute(
                "SELECT pg_catalog.pg_get_indexdef(indexrelid) "
                "FROM pg_catalog.pg_index WHERE indrelid=%s ORDER BY pg_catalog.pg_get_indexdef(indexrelid)",
                (oid,),
            ).fetchall()
            triggers = conn.execute(
                "SELECT tgname,pg_catalog.pg_get_triggerdef(oid,true) "
                "FROM pg_catalog.pg_trigger WHERE tgrelid=%s AND NOT tgisinternal ORDER BY tgname", (oid,),
            ).fetchall()
            tables[table] = dict(kind=kind, columns=columns, constraints=constraints,
                                 indexes=[r[0] for r in indexes], triggers=triggers)
    canonical = json.dumps(tables, sort_keys=True, separators=(",", ":"))
    return {"schema": schema, "relations": tables,
            "catalog_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "missing_relations": [t for t in TABLES if tables[t] is None],
            "integration_verified": False,
            "scope": "catalog observation only; compare with pinned parent DDL and run actual integration tests"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", default="QED_PG_DSN", help="name of a configured DSN variable; value is never printed")
    parser.add_argument("--schema", default="public")
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        print("schema inspection requires a configured DSN environment variable", file=sys.stderr)
        return 1
    try:
        with psycopg.connect(dsn, autocommit=True, client_encoding="UTF8") as conn:
            result = inspect(conn, args.schema)
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        return 2 if result["missing_relations"] else 0
    except psycopg.Error as exc:
        print(f"schema inspection failed ({type(exc).__name__}); check connection and catalog access", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
