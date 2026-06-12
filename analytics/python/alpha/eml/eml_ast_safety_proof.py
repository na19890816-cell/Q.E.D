"""
eml_ast_safety_proof.py
------------------------
EML AST 安全証明の保存・検索・集計ユーティリティ。

機能
----
- save_safety_proof_to_candidate()  : eml_alpha_candidates の safety_proof_json に保存
- load_safety_proof()               : 候補 ID から proof を復元
- assert_no_future_leakage()        : leakage があれば ValueError を raise
- summarize_proofs()                : 複数候補の安全証明をサマリー化

設計原則
--------
- proof は JSON として eml_alpha_candidates.run_metadata->safety_proof に保存
- DB 書き込みは psycopg3 + %s プレースホルダー + UPSERT
- trace_id を proof に含めることで横断検索可能
- dry_run=True 時は DB に書かず proof dict を返すだけ
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Dataclass
# --------------------------------------------------------------------------- #

@dataclass
class ASTSafetyRecord:
    """DB 保存単位。eml_alpha_candidates と 1:1 対応。"""
    candidate_id: str
    run_id: str
    trace_id: str
    formula_text: str
    is_safe: bool
    future_dependency_flag: bool
    violation_terminals: List[str]
    min_lag_overall: int
    max_lag_overall: int
    strict_mode: bool
    proof_json: Dict[str, Any]


# --------------------------------------------------------------------------- #
# 安全チェック
# --------------------------------------------------------------------------- #

def assert_no_future_leakage(
    proof_dict: Dict[str, Any],
    strict: bool = True,
) -> None:
    """
    proof_dict に future_dependency_flag=True があれば ValueError を raise する。

    Parameters
    ----------
    proof_dict : LagSafetyProof を proof_to_dict() した結果
    strict : True の場合 leakage があれば例外、False の場合は警告のみ

    Raises
    ------
    ValueError
        future_dependency_flag=True かつ strict=True の場合
    """
    if proof_dict.get("future_dependency_flag", False):
        violations = proof_dict.get("violation_terminals", [])
        msg = (
            f"Future leakage detected in formula: {proof_dict.get('formula_text', '')!r}\n"
            f"Violation terminals: {violations}"
        )
        if strict:
            raise ValueError(msg)
        else:
            import warnings
            warnings.warn(msg, UserWarning, stacklevel=2)


# --------------------------------------------------------------------------- #
# DB 書き込み (psycopg3)
# --------------------------------------------------------------------------- #

def save_safety_proof(
    conn: Any,
    record: ASTSafetyRecord,
    dry_run: bool = False,
) -> None:
    """
    eml_alpha_candidates.run_metadata の safety_proof キーに proof を保存する。

    psycopg3 接続を前提とする。
    dry_run=True のときは DB 書き込みをスキップする。

    Parameters
    ----------
    conn : psycopg3 connection
    record : ASTSafetyRecord
    dry_run : bool
    """
    if dry_run:
        return

    proof_json_str = json.dumps(record.proof_json, default=str)

    sql = """
        UPDATE eml_alpha_candidates
        SET run_metadata = COALESCE(run_metadata, '{}'::jsonb)
            || jsonb_build_object(
                'safety_proof', %s::jsonb,
                'is_safe', %s::boolean,
                'future_dependency_flag', %s::boolean,
                'violation_terminals', %s::jsonb
            ),
            updated_at = now()
        WHERE candidate_id = %s
          AND run_id       = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            proof_json_str,
            record.is_safe,
            record.future_dependency_flag,
            json.dumps(record.violation_terminals),
            record.candidate_id,
            record.run_id,
        ))
    conn.commit()


def load_safety_proof(
    conn: Any,
    candidate_id: str,
    run_id: str,
) -> Optional[Dict[str, Any]]:
    """
    候補の safety_proof_json を取得する。

    Returns
    -------
    dict | None  (保存されていない場合は None)
    """
    sql = """
        SELECT run_metadata->'safety_proof'
        FROM eml_alpha_candidates
        WHERE candidate_id = %s AND run_id = %s
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (candidate_id, run_id))
        row = cur.fetchone()

    if row is None or row[0] is None:
        return None
    val = row[0]
    if isinstance(val, str):
        return json.loads(val)
    return val


# --------------------------------------------------------------------------- #
# サマリー
# --------------------------------------------------------------------------- #

def summarize_proofs(records: List[ASTSafetyRecord]) -> Dict[str, Any]:
    """
    複数候補の安全証明をまとめて集計する。

    Returns
    -------
    dict with keys:
        total, safe_count, unsafe_count, leakage_rate,
        all_violation_terminals, min_lag_min, max_lag_max
    """
    total = len(records)
    safe_count = sum(1 for r in records if r.is_safe)
    unsafe_count = total - safe_count
    all_violations: List[str] = []
    for r in records:
        all_violations.extend(r.violation_terminals)

    min_lags = [r.min_lag_overall for r in records if r.min_lag_overall > 0]
    max_lags = [r.max_lag_overall for r in records]

    return {
        "total": total,
        "safe_count": safe_count,
        "unsafe_count": unsafe_count,
        "leakage_rate": unsafe_count / total if total > 0 else 0.0,
        "all_violation_terminals": sorted(set(all_violations)),
        "min_lag_min": min(min_lags) if min_lags else None,
        "max_lag_max": max(max_lags) if max_lags else None,
    }


def build_ast_safety_record(
    candidate_id: str,
    run_id: str,
    trace_id: str,
    proof_dict: Dict[str, Any],
) -> ASTSafetyRecord:
    """
    proof_to_dict() の結果から ASTSafetyRecord を構築する。

    Parameters
    ----------
    candidate_id, run_id, trace_id : str
    proof_dict : eml_lag_analyzer.proof_to_dict() の戻り値

    Returns
    -------
    ASTSafetyRecord
    """
    return ASTSafetyRecord(
        candidate_id=candidate_id,
        run_id=run_id,
        trace_id=trace_id,
        formula_text=proof_dict.get("formula_text", ""),
        is_safe=bool(proof_dict.get("is_safe", True)),
        future_dependency_flag=bool(proof_dict.get("future_dependency_flag", False)),
        violation_terminals=proof_dict.get("violation_terminals", []),
        min_lag_overall=int(proof_dict.get("min_lag_overall", 1)),
        max_lag_overall=int(proof_dict.get("max_lag_overall", 1)),
        strict_mode=bool(proof_dict.get("strict_mode", True)),
        proof_json=proof_dict,
    )
