"""
postgres_lineage_bridge.py
--------------------------
ADR-002: 試行台帳 (qed_trial_batches / qed_lineage_edges) の PostgreSQL ブリッジ。

責務:
  1. insert_trial_batch()   : 冪等 INSERT (ON CONFLICT DO NOTHING)。UPDATE は行わない
  2. insert_lineage_edge()  : 同上
  3. load_ledger_for_family(): 対象 family + 祖先 family の batch / edge を読み出し TrialLedger を構築
  4. fetch_trial_snapshot() : load → TrialLedger.snapshot() の便宜ラッパー

設計原則:
  - psycopg3 (%s プレースホルダ) / conn は呼び出し側が管理
  - dry_run=True なら書き込みスキップ
  - append-only: テーブル側トリガが UPDATE/DELETE を拒否するため、本モジュールも発行しない
  - 系譜の BFS は DB の再帰 CTE で祖先 family 集合を得た上で、as-of 判定は
    TrialLedger 側 (純 Python) に一元化する (判定ロジックを 1 か所に保つ)
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import psycopg

from analytics.python.frost.frost_lineage import (
    LineageEdge,
    SharpeStats,
    TrialBatch,
    TrialLedger,
    TrialSnapshot,
    _to_utc,
)


# ---------------------------------------------------------------------------
# 書き込み
# ---------------------------------------------------------------------------

_INSERT_BATCH_SQL = """
    INSERT INTO qed_trial_batches (
        batch_id, family_key, run_id, trace_id, source_type, stage,
        n_trials, sr_count, sr_mean, sr_m2, sr_periodicity,
        family_spec, metadata, recorded_at
    ) VALUES (
        %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s,
        %s::jsonb, %s::jsonb, COALESCE(%s, now())
    )
    ON CONFLICT (batch_id) DO NOTHING
"""

_INSERT_EDGE_SQL = """
    INSERT INTO qed_lineage_edges (
        edge_id, parent_family_key, child_family_key,
        parent_formula_hash, child_formula_hash, relation,
        run_id, metadata, recorded_at
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s::jsonb, COALESCE(%s, now())
    )
    ON CONFLICT (edge_id) DO NOTHING
"""


def _json(d: Dict[str, Any]) -> str:
    return json.dumps(d or {}, sort_keys=True, ensure_ascii=True, default=str)


def insert_trial_batch(
    conn: psycopg.Connection,
    batch: TrialBatch,
    dry_run: bool = False,
) -> bool:
    """
    TrialBatch を追記する。

    Returns
    -------
    bool
        新規挿入なら True、既存 (冪等スキップ) または dry_run なら False。
    """
    if dry_run:
        return False
    r = batch.to_row()
    with conn.cursor() as cur:
        cur.execute(_INSERT_BATCH_SQL, (
            r["batch_id"], r["family_key"], r["run_id"], r["trace_id"],
            r["source_type"], r["stage"],
            r["n_trials"], r["sr_count"], r["sr_mean"], r["sr_m2"], r["sr_periodicity"],
            _json(r["family_spec"]), _json(r["metadata"]), r["recorded_at"],
        ))
        return (getattr(cur, "rowcount", 0) or 0) > 0


def insert_trial_batches(
    conn: psycopg.Connection,
    batches: List[TrialBatch],
    dry_run: bool = False,
) -> int:
    """複数 batch を追記し、新規挿入件数を返す。"""
    return sum(1 for b in batches if insert_trial_batch(conn, b, dry_run=dry_run))


def insert_lineage_edge(
    conn: psycopg.Connection,
    edge: LineageEdge,
    dry_run: bool = False,
) -> bool:
    """LineageEdge を追記する。新規なら True。"""
    if dry_run:
        return False
    r = edge.to_row()
    with conn.cursor() as cur:
        cur.execute(_INSERT_EDGE_SQL, (
            r["edge_id"], r["parent_family_key"], r["child_family_key"],
            r["parent_formula_hash"], r["child_formula_hash"], r["relation"],
            r["run_id"], _json(r["metadata"]), r["recorded_at"],
        ))
        return (getattr(cur, "rowcount", 0) or 0) > 0


def ledger_tables_exist(conn: psycopg.Connection) -> bool:
    """migration 084 が適用済みか (両テーブルの存在) を返す。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('public.qed_trial_batches') IS NOT NULL "
            "   AND to_regclass('public.qed_lineage_edges') IS NOT NULL"
        )
        row = cur.fetchone()
    return bool(row and row[0])


# ---------------------------------------------------------------------------
# 読み出し
# ---------------------------------------------------------------------------

# One statement gives READ COMMITTED callers one visibility snapshot for the
# ancestor graph and its batches. UNION makes recursive ancestry cycle-safe.
_SELECT_LEDGER_SQL = """
    WITH RECURSIVE anc(family_key) AS (
        SELECT %(family_key)s::text
        UNION
        SELECT e.parent_family_key
          FROM qed_lineage_edges e
          JOIN anc a ON e.child_family_key = a.family_key
         WHERE %(include_ancestors)s::boolean
           AND (%(as_of)s::timestamptz IS NULL OR e.recorded_at <= %(as_of)s::timestamptz)
    )
    SELECT 'batch'::text AS kind, to_jsonb(b) AS data,
           b.sr_mean, b.sr_m2, b.recorded_at
      FROM qed_trial_batches b JOIN anc a ON b.family_key = a.family_key
    UNION ALL
    SELECT 'edge'::text, to_jsonb(e), NULL::double precision,
           NULL::double precision, e.recorded_at
      FROM qed_lineage_edges e JOIN anc a ON e.child_family_key = a.family_key
     WHERE %(include_ancestors)s::boolean
"""

# Direct-only callers historically need SELECT on batches alone. Do not make
# them depend on access to (or even the presence of) the lineage-edge table.
_SELECT_DIRECT_SQL = """
    SELECT 'batch'::text AS kind, to_jsonb(b) AS data,
           b.sr_mean, b.sr_m2, b.recorded_at
      FROM qed_trial_batches b WHERE b.family_key = %(family_key)s::text
"""

_BATCH_FIELDS = (
    "batch_id", "family_key", "run_id", "trace_id", "source_type", "stage",
    "n_trials", "sr_count", "sr_mean", "sr_m2", "sr_periodicity", "family_spec", "metadata",
)
_EDGE_FIELDS = (
    "edge_id", "parent_family_key", "child_family_key", "parent_formula_hash",
    "child_formula_hash", "relation", "run_id", "metadata",
)


def _s(v: Any, default: str = "") -> str:
    """TEXT 列の防御的デコード (SQL_ASCII DB では psycopg が bytes を返すため)。"""
    if v is None:
        return default
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).decode("utf-8")
    return str(v)


def _as_dict(v: Any) -> Dict[str, Any]:
    if v is None:
        return {}
    if isinstance(v, (bytes, bytearray, memoryview)):
        v = bytes(v).decode("utf-8")
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        return json.loads(v) if v else {}
    raise ValueError(f"JSONB 列の型が不正です: {type(v)}")


def _row_to_batch(row: Any) -> TrialBatch:
    (batch_id, family_key, run_id, trace_id, source_type, stage,
     n_trials, sr_count, sr_mean, sr_m2, sr_periodicity,
     family_spec, metadata, recorded_at) = row
    return TrialBatch(
        batch_id=_s(batch_id),
        family_key=_s(family_key),
        run_id=_s(run_id),
        stage=_s(stage),
        n_trials=int(n_trials),
        sr_stats=SharpeStats(int(sr_count or 0), float(sr_mean or 0.0), float(sr_m2 or 0.0)),
        sr_periodicity=_s(sr_periodicity, "daily") or "daily",
        trace_id=_s(trace_id),
        source_type=_s(source_type, "eml") or "eml",
        family_spec=_as_dict(family_spec),
        metadata=_as_dict(metadata),
        recorded_at=recorded_at,
    )


def _row_to_edge(row: Any) -> LineageEdge:
    (edge_id, parent_fk, child_fk, parent_fh, child_fh, relation,
     run_id, metadata, recorded_at) = row
    return LineageEdge(
        edge_id=_s(edge_id),
        parent_family_key=_s(parent_fk),
        child_family_key=_s(child_fk),
        relation=_s(relation),
        parent_formula_hash=_s(parent_fh) if parent_fh is not None else None,
        child_formula_hash=_s(child_fh) if child_fh is not None else None,
        run_id=_s(run_id),
        metadata=_as_dict(metadata),
        recorded_at=recorded_at,
    )


def load_ledger_for_family(
    conn: psycopg.Connection,
    family_key: str,
    as_of: Optional[datetime] = None,
    include_ancestors: bool = True,
) -> TrialLedger:
    """
    family_key (+ 祖先) に関係する batch / edge を読み出して TrialLedger を返す。

    as_of による最終的な採否判定は TrialLedger.snapshot() が行う
    (ここでは祖先探索の枝刈りにのみ as_of を用いる)。
    祖先・edge・batch は単一 SQL の同じ可視性で取得し、呼出し元の
    トランザクションや分離レベルは変更しない。
    SQL の as_of も純関数と同じ UTC 規則（naive は UTC）で解釈する。
    """
    batches: List[TrialBatch] = []
    edges: List[LineageEdge] = []
    with conn.cursor() as cur:
        cur.execute(_SELECT_LEDGER_SQL if include_ancestors else _SELECT_DIRECT_SQL, {
            "family_key": family_key, "as_of": _to_utc(as_of),
            "include_ancestors": bool(include_ancestors),
        })
        for kind, payload, sr_mean, sr_m2, recorded_at in cur.fetchall() or []:
            data = dict(_as_dict(payload))
            if _s(kind) == "batch":
                # Keep native float8/timestamptz decoding for SR precision and
                # datetime compatibility instead of round-tripping these via JSON.
                data.update(sr_mean=sr_mean, sr_m2=sr_m2)
                batches.append(_row_to_batch(tuple(data[k] for k in _BATCH_FIELDS) + (recorded_at,)))
            elif _s(kind) == "edge":
                edges.append(_row_to_edge(tuple(data[k] for k in _EDGE_FIELDS) + (recorded_at,)))
            else:
                raise ValueError("invalid ledger row kind")

    return TrialLedger(batches=batches, edges=edges)


def fetch_trial_snapshot(
    conn: psycopg.Connection,
    family_key: str,
    as_of: Optional[datetime] = None,
    sr_periodicity: Optional[str] = None,
    include_ancestors: bool = True,
) -> TrialSnapshot:
    """load_ledger_for_family → snapshot の便宜ラッパー。"""
    ledger = load_ledger_for_family(conn, family_key, as_of, include_ancestors)
    return ledger.snapshot(
        family_key, as_of=as_of, sr_periodicity=sr_periodicity,
        include_ancestors=include_ancestors,
    )
