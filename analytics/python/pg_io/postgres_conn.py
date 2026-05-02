"""
postgres_conn.py
----------------
psycopg3 ベースの PostgreSQL 接続ユーティリティ。
環境変数 QED_PG_DSN から接続文字列を読む。
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Generator

import psycopg
from psycopg import Connection

logger = logging.getLogger(__name__)


def get_dsn() -> str:
    dsn = os.environ.get("QED_PG_DSN", "")
    if not dsn:
        raise EnvironmentError("QED_PG_DSN が未設定です。config/env/.env.local を確認してください。")
    return dsn


@contextmanager
def get_connection(autocommit: bool = False) -> Generator[Connection, None, None]:
    """
    psycopg3 接続コンテキストマネージャ。
    autocommit=False (デフォルト) では明示的 commit / rollback が必要。
    """
    dsn = get_dsn()
    conn = psycopg.connect(dsn, autocommit=autocommit)
    try:
        yield conn
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def check_table_columns(conn: Connection, schema: str, table: str) -> dict[str, str]:
    """
    information_schema.columns から {列名: データ型} を返す。
    テーブル/列の存在確認に使用する。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def table_exists(conn: Connection, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
            """,
            (schema, table),
        )
        return cur.fetchone() is not None
