"""
audit_hook.py
-------------
バックテスト結果を QED audit_events テーブルに記録するフック。

emit_backtest_audit(conn, run_id, trace_id, decision, detail) を呼ぶと
audit_events に INSERT する。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict

import psycopg


# 許可される audit status
ALLOWED_STATUSES = {"APPLIED", "DRY_RUN", "CONFLICTED", "REJECTED"}


def emit_backtest_audit(
    conn: psycopg.Connection,
    run_id: str,
    trace_id: str,
    decision: str,
    detail: Dict[str, Any] | None = None,
    phase: str = "backtest",
    dry_run: bool = False,
    schema: str = "public",
    table: str = "audit_events",
) -> str:
    """
    バックテスト完了時に audit_events へイベントを INSERT する。

    Parameters
    ----------
    conn       : psycopg3 コネクション
    run_id     : EML バックテスト run_id
    trace_id   : 伝播 trace_id
    decision   : APPLIED / REJECTED / CONFLICTED / DRY_RUN
    detail     : 追加メタデータ (JSONB)
    phase      : audit フェーズ名
    dry_run    : True の場合は DRY_RUN ステータスを強制
    schema     : スキーマ名
    table      : テーブル名

    Returns
    -------
    audit_event_id (UUID 文字列)
    """
    if dry_run:
        decision = "DRY_RUN"

    if decision not in ALLOWED_STATUSES:
        raise ValueError(
            f"audit status '{decision}' は許可されていません。"
            f" 許可値: {sorted(ALLOWED_STATUSES)}"
        )

    event_id   = str(uuid.uuid4())
    event_type = f"EML_BACKTEST_{decision}"
    payload    = {
        "run_id": run_id,
        "trace_id": trace_id,
        "phase": phase,
        "decision": decision,
        **(detail or {}),
    }

    from psycopg.sql import SQL, Identifier

    sql = SQL(
        "INSERT INTO {schema}.{table} "
        "(event_id, trace_id, event_type, status, payload, created_at) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, now()) "
        "ON CONFLICT (event_id) DO NOTHING"
    ).format(
        schema=Identifier(schema),
        table=Identifier(table),
    )

    with conn.cursor() as cur:
        cur.execute(sql, (
            event_id,
            trace_id,
            event_type,
            decision,
            json.dumps(payload),
        ))
    conn.commit()

    return event_id
