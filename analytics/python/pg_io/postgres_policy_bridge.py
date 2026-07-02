"""
postgres_policy_bridge.py
--------------------------
Phase 1: PolicySpec → qed_policies テーブル upsert ブリッジ。

責務:
  1. upsert_policy_spec(): qed_policies への INSERT ON CONFLICT DO UPDATE
  2. load_policy_spec_by_hash(): policy_hash で PolicySpec を復元
  3. touch_policy_used_at(): frost_runs からの参照時に used_at を更新
  4. fetch_policy_for_run(): frost_runs.policy_hash から PolicySpec を取得

設計原則:
  - psycopg3 使用 (%s プレースホルダ)
  - conn は呼び出し側が管理 (接続の開閉はここで行わない)
  - dry_run=True の場合は書き込みをスキップ
  - JSON シリアライズは json.dumps / json.loads (標準ライブラリ)
  - pure Python, numpy 不使用
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

import psycopg

from analytics.python.frost.policy_spec import PolicySpec


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------

def upsert_policy_spec(
    conn: psycopg.Connection,
    spec: PolicySpec,
    dry_run: bool = False,
) -> str:
    """
    PolicySpec を qed_policies テーブルに upsert する。

    同一 policy_hash が既存の場合は used_at のみ更新する。
    新規の場合はフル INSERT。

    Parameters
    ----------
    conn : psycopg.Connection
        psycopg3 接続オブジェクト。呼び出し側がトランザクション管理を行う。
    spec : PolicySpec
        保存する PolicySpec。
    dry_run : bool
        True の場合は実際の DB 書き込みをスキップし policy_hash のみ返す。

    Returns
    -------
    str
        upsert した (または dry_run でスキップした) policy_hash。
    """
    policy_hash = spec.policy_hash

    if dry_run:
        return policy_hash

    spec_json = json.dumps(spec.to_dict(), sort_keys=True, ensure_ascii=True)
    source_env_vars = list(spec.source_env_vars)

    sql = """
        INSERT INTO qed_policies (
            policy_hash,
            spec_json,
            source_env_vars,
            engine_version,
            phase_tag,
            description,
            first_seen_at,
            used_at,
            created_at
        ) VALUES (
            %s, %s::jsonb, %s, %s, %s, %s,
            now(), now(), now()
        )
        ON CONFLICT (policy_hash) DO UPDATE
            SET used_at = now()
    """

    with conn.cursor() as cur:
        cur.execute(sql, (
            policy_hash,
            spec_json,
            source_env_vars,
            spec.engine_version,
            spec.phase_tag,
            spec.description,
        ))

    return policy_hash


# ---------------------------------------------------------------------------
# ロード
# ---------------------------------------------------------------------------

def load_policy_spec_by_hash(
    conn: psycopg.Connection,
    policy_hash: str,
) -> Optional[PolicySpec]:
    """
    policy_hash で qed_policies から PolicySpec を復元する。

    Parameters
    ----------
    conn : psycopg.Connection
        psycopg3 接続オブジェクト。
    policy_hash : str
        "sha256:<hex>" 形式のハッシュ文字列。

    Returns
    -------
    PolicySpec or None
        見つかった場合は PolicySpec。見つからない場合は None。
    """
    sql = """
        SELECT spec_json
        FROM qed_policies
        WHERE policy_hash = %s
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (policy_hash,))
        row = cur.fetchone()

    if row is None:
        return None

    spec_json = row[0]
    if isinstance(spec_json, str):
        d = json.loads(spec_json)
    elif isinstance(spec_json, dict):
        d = spec_json
    else:
        raise ValueError(
            f"qed_policies.spec_json の型が不正: {type(spec_json)}"
        )

    return PolicySpec.from_dict(d)


# ---------------------------------------------------------------------------
# used_at 更新
# ---------------------------------------------------------------------------

def touch_policy_used_at(
    conn: psycopg.Connection,
    policy_hash: str,
    dry_run: bool = False,
) -> None:
    """
    qed_policies.used_at を現在時刻に更新する。

    frost_runs が特定のポリシーを参照するたびに呼び出す。
    「最後に使われた日時」を記録するためだけの軽量操作。

    Parameters
    ----------
    conn : psycopg.Connection
    policy_hash : str
    dry_run : bool
        True の場合はスキップ。
    """
    if dry_run:
        return

    sql = """
        UPDATE qed_policies
           SET used_at = now()
         WHERE policy_hash = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (policy_hash,))


# ---------------------------------------------------------------------------
# frost_runs の policy_hash 更新
# ---------------------------------------------------------------------------

def set_run_policy_hash(
    conn: psycopg.Connection,
    run_id: str,
    policy_hash: str,
    dry_run: bool = False,
) -> None:
    """
    frost_runs.policy_hash を設定する。

    frost_runner.py が run_id を確定した直後に呼び出す。
    qed_policies への upsert よりも後に呼ぶこと（FK 制約のため）。

    Parameters
    ----------
    conn : psycopg.Connection
    run_id : str
        frost_runs.run_id (UUID 文字列)。
    policy_hash : str
        "sha256:<hex>" 形式。
    dry_run : bool
        True の場合はスキップ。
    """
    if dry_run:
        return

    sql = """
        UPDATE frost_runs
           SET policy_hash = %s,
               updated_at  = now()
         WHERE run_id = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (policy_hash, run_id))


# ---------------------------------------------------------------------------
# frost_runs から PolicySpec を取得 (convenience wrapper)
# ---------------------------------------------------------------------------

def fetch_policy_for_run(
    conn: psycopg.Connection,
    run_id: str,
) -> Optional[PolicySpec]:
    """
    frost_runs.run_id から紐づく PolicySpec を取得する。

    Parameters
    ----------
    conn : psycopg.Connection
    run_id : str
        frost_runs.run_id (UUID 文字列)。

    Returns
    -------
    PolicySpec or None
        ポリシーが記録されていない (policy_hash IS NULL) 場合は None。
    """
    sql = """
        SELECT r.policy_hash
        FROM frost_runs r
        WHERE r.run_id = %s
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (run_id,))
        row = cur.fetchone()

    if row is None or row[0] is None:
        return None

    return load_policy_spec_by_hash(conn, row[0])


# ---------------------------------------------------------------------------
# ポリシー一覧クエリ（デバッグ・監査用）
# ---------------------------------------------------------------------------

def list_policy_hashes(
    conn: psycopg.Connection,
    engine_version: Optional[str] = None,
    phase_tag: Optional[str] = None,
    limit: int = 100,
) -> list:
    """
    qed_policies の一覧を返す（デバッグ・監査用）。

    Parameters
    ----------
    conn : psycopg.Connection
    engine_version : str, optional
        フィルタ条件。
    phase_tag : str, optional
        フィルタ条件。
    limit : int
        最大返却件数。

    Returns
    -------
    list of dict
        各要素は {"policy_hash", "engine_version", "phase_tag",
                  "description", "first_seen_at", "used_at"} を持つ dict。
    """
    conditions = []
    params: list = []

    if engine_version is not None:
        conditions.append("engine_version = %s")
        params.append(engine_version)
    if phase_tag is not None:
        conditions.append("phase_tag = %s")
        params.append(phase_tag)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    sql = f"""
        SELECT policy_hash, engine_version, phase_tag,
               description, first_seen_at, used_at
        FROM qed_policies
        {where}
        ORDER BY used_at DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        {
            "policy_hash":    r[0],
            "engine_version": r[1],
            "phase_tag":      r[2],
            "description":    r[3],
            "first_seen_at":  r[4],
            "used_at":        r[5],
        }
        for r in rows
    ]
