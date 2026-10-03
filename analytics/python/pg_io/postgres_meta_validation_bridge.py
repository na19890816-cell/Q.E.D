"""
postgres_meta_validation_bridge.py
----------------------------------
P8 メタ検証結果 (MetaValidationReport) を frost_meta_validation に書き込む。

  - 冪等: meta_run_id は (policy_hash, dataset_hash, params) から決定論的に生成
  - テーブル未作成 (migration 085 未適用) の場合は書き込まずに False を返す
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Optional

from analytics.python.frost.frost_meta_sensitivity import MetaValidationReport

TABLE = "frost_meta_validation"

_COLUMNS = ("decision_flip_rate", "gate_flip_rate", "topk_jaccard",
            "promo_jaccard", "kendall_tau", "kendall_tau_passed")


def make_meta_run_id(report: MetaValidationReport) -> str:
    blob = json.dumps(
        {"policy_hash": report.policy_hash, "dataset_hash": report.dataset_hash, "params": report.params},
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode()
    return "meta:" + hashlib.sha256(blob).hexdigest()[:32]


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _json(v: Any) -> str:
    def clean(x: Any) -> Any:
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return None
        if isinstance(x, dict):
            return {str(k): clean(val) for k, val in x.items()}
        if isinstance(x, (list, tuple)):
            return [clean(i) for i in x]
        return x
    return json.dumps(clean(v), sort_keys=True, default=str)


def meta_validation_table_exists(conn: Any) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"public.{TABLE}",))
        row = cur.fetchone()
    return bool(row and row[0])


def insert_meta_validation(conn: Any, report: MetaValidationReport, meta_run_id: Optional[str] = None) -> int:
    """
    レポート全行を INSERT する (ON CONFLICT DO NOTHING)。

    Returns
    -------
    int : 送信した行数 (テーブル未作成なら 0)
    """
    if not meta_validation_table_exists(conn):
        return 0
    mrid = meta_run_id or make_meta_run_id(report)
    sql = (
        f"INSERT INTO {TABLE} (meta_run_id, policy_hash, dataset_hash, analysis_type, target, "
        f"perturbation, n_candidates, {', '.join(_COLUMNS)}, metrics, params) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, {', '.join(['%s'] * len(_COLUMNS))}, %s::jsonb, %s::jsonb) "
        f"ON CONFLICT (meta_run_id, analysis_type, target, perturbation) DO NOTHING"
    )
    params_json = _json(report.params)
    n = 0
    with conn.cursor() as cur:
        for row in report.rows:
            m = row.metrics
            cur.execute(sql, (
                mrid, report.policy_hash, report.dataset_hash, row.analysis_type, row.target,
                row.perturbation, int(m.get("n_candidates", report.n_candidates) or 0),
                *[_num(m.get(c)) for c in _COLUMNS],
                _json(m), params_json,
            ))
            n += 1
    return n
