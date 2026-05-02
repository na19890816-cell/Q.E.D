"""
postgres_eml_alpha_writer.py
----------------------------
EML alpha run / candidate を PostgreSQL へ UPSERT する IO 層。

テーブル: eml_alpha_runs, eml_alpha_candidates
"""
from __future__ import annotations

import json
import math
from typing import Any, List

import psycopg
from psycopg.sql import SQL, Identifier


# ------------------------------------------------------------------ #
# NaN/Inf サニタイズ ユーティリティ
# ------------------------------------------------------------------ #

def _safe_float(v: Any) -> float:
    """NaN / Inf を 0.0 に変換。"""
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return 0.0
        return f
    except (TypeError, ValueError):
        return 0.0


def _safe_json(obj: Any) -> str:
    """NaN/Inf を JSON セーフ値に変換してシリアライズ。"""
    def _sanitize(v: Any) -> Any:
        if isinstance(v, float):
            if math.isnan(v) or math.isinf(v):
                return None
            return v
        if isinstance(v, dict):
            return {k: _sanitize(vv) for k, vv in v.items()}
        if isinstance(v, list):
            return [_sanitize(x) for x in v]
        return v
    return json.dumps(_sanitize(obj))

from analytics.python.alpha.eml.eml_search import EMLCandidate
from analytics.python.alpha.eml.eml_master_formula import EMLDiscoveryOutput


# ------------------------------------------------------------------ #
# eml_alpha_runs UPSERT
# ------------------------------------------------------------------ #

def upsert_alpha_run(
    conn: psycopg.Connection,
    output: EMLDiscoveryOutput,
    status: str = "completed",
) -> None:
    """
    eml_alpha_runs に UPSERT する。
    """
    sql = SQL(
        "INSERT INTO {tbl} "
        "(run_id, trace_id, batch_label, terminal_set_hash, "
        " target_horizon, fitness_kind, max_depth, max_nodes, "
        " status, total_candidates, promoted_candidates, "
        " run_metadata, started_at, completed_at, created_at, updated_at) "
        "VALUES "
        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, "
        " now(), now(), now(), now()) "
        "ON CONFLICT (run_id) DO UPDATE SET "
        "  status              = EXCLUDED.status, "
        "  total_candidates    = EXCLUDED.total_candidates, "
        "  promoted_candidates = EXCLUDED.promoted_candidates, "
        "  run_metadata        = EXCLUDED.run_metadata, "
        "  completed_at        = EXCLUDED.completed_at, "
        "  updated_at          = now()"
    ).format(tbl=Identifier("eml_alpha_runs"))

    terminals = output.candidates[0].metadata.get("terminals_used", []) if output.candidates else []
    meta = {
        "total_searched": output.total_searched,
        "terminal_set_hash": output.terminal_set_hash,
    }

    with conn.cursor() as cur:
        cur.execute(sql, (
            output.run_id,
            output.trace_id,
            output.batch_label,
            output.terminal_set_hash,
            "5d",                           # target_horizon
            "rank_ic_cost_adj",             # fitness_kind
            3,                              # max_depth
            8,                              # max_nodes
            status,
            output.total_searched,
            len(output.promoted),
            json.dumps(meta),
        ))
    conn.commit()


# ------------------------------------------------------------------ #
# eml_alpha_candidates UPSERT
# ------------------------------------------------------------------ #

def upsert_alpha_candidates(
    conn: psycopg.Connection,
    candidates: List[EMLCandidate],
) -> None:
    """
    eml_alpha_candidates に一括 UPSERT する。
    """
    sql = SQL(
        "INSERT INTO {tbl} "
        "(candidate_id, run_id, trace_id, tree_json, tree_depth, "
        " node_count, compiled_safe_expr, fitness_score, "
        " status, rejection_reason, run_metadata, created_at, updated_at) "
        "VALUES "
        "(%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s::jsonb, now(), now()) "
        "ON CONFLICT (candidate_id) DO UPDATE SET "
        "  fitness_score    = EXCLUDED.fitness_score, "
        "  status           = EXCLUDED.status, "
        "  rejection_reason = EXCLUDED.rejection_reason, "
        "  run_metadata     = EXCLUDED.run_metadata, "
        "  updated_at       = now()"
    ).format(tbl=Identifier("eml_alpha_candidates"))

    rows = []
    for c in candidates:
        rows.append((
            c.candidate_id,
            c.run_id,
            c.trace_id,
            c.node.to_json(),
            c.tree_depth(),
            c.node_count(),
            c.compiled_expr,
            _safe_float(c.fitness_score),
            c.status,
            c.rejection_reason,
            _safe_json(c.metadata),
        ))

    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
