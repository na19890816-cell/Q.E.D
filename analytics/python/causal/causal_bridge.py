"""
causal_bridge.py
----------------
Causal Discovery Layer の PostgreSQL 書き込みブリッジ。

CausalRunResult を DB の causal テーブル群に保存する。
psycopg3 の %s プレースホルダー + INSERT ... ON CONFLICT DO UPDATE を使用。

設計原則:
  - dry_run=True 時は書き込みなし
  - trace_id end-to-end
  - rerun-safe（upsert）
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .causal_runner import CausalRunResult


# ---------------------------------------------------------------------------
# 保存関数
# ---------------------------------------------------------------------------

def save_causal_run_result(
    conn: Any,
    result: CausalRunResult,
    dry_run: bool = False,
) -> None:
    """
    CausalRunResult を causal_candidate_tests テーブルに保存する。

    Parameters
    ----------
    conn : psycopg3 connection
    result : CausalRunResult
    dry_run : bool
        True の場合は DB 書き込みを行わない
    """
    if dry_run or result.dry_run:
        return

    diagnostics = result.diagnostics
    direction = result.direction_result
    invariance = result.invariance_result

    sql = """
    INSERT INTO causal_candidate_tests (
        candidate_id,
        run_id,
        trace_id,
        causal_direction_score,
        invariance_pass_ratio,
        intervention_consistency_score,
        confounding_risk_score,
        causal_composite_score,
        forward_correlation,
        backward_correlation,
        direction_asymmetry,
        granger_proxy_score,
        coefficient_stability,
        regime_consistency_score,
        n_regimes_tested,
        n_regimes_passed,
        gate_pass,
        gate_reason,
        direction_details_json,
        invariance_details_json,
        diagnostics_json
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    )
    ON CONFLICT (candidate_id, run_id) DO UPDATE SET
        causal_direction_score        = EXCLUDED.causal_direction_score,
        invariance_pass_ratio         = EXCLUDED.invariance_pass_ratio,
        intervention_consistency_score = EXCLUDED.intervention_consistency_score,
        confounding_risk_score        = EXCLUDED.confounding_risk_score,
        causal_composite_score        = EXCLUDED.causal_composite_score,
        gate_pass                     = EXCLUDED.gate_pass,
        gate_reason                   = EXCLUDED.gate_reason,
        diagnostics_json              = EXCLUDED.diagnostics_json,
        updated_at                    = now()
    """

    conn.execute(sql, (
        result.candidate_id,
        result.run_id,
        result.trace_id,
        diagnostics.causal_direction_score,
        diagnostics.invariance_pass_ratio,
        diagnostics.intervention_consistency_score,
        diagnostics.confounding_risk_score,
        diagnostics.causal_composite_score,
        direction.forward_correlation,
        direction.backward_correlation,
        direction.direction_asymmetry,
        direction.granger_proxy_score,
        invariance.coefficient_stability,
        invariance.regime_consistency_score,
        invariance.n_regimes_tested,
        invariance.n_regimes_passed,
        diagnostics.all_gates_pass,
        "; ".join(diagnostics.failure_reasons) if diagnostics.failure_reasons else None,
        json.dumps(direction.to_dict()),
        json.dumps(invariance.to_dict()),
        json.dumps(diagnostics.to_dict()),
    ))


def load_causal_run_result(
    conn: Any,
    candidate_id: str,
    run_id: str,
) -> Optional[dict]:
    """
    causal_candidate_tests テーブルから保存結果を読み込む。
    """
    sql = """
    SELECT *
    FROM causal_candidate_tests
    WHERE candidate_id = %s AND run_id = %s
    LIMIT 1
    """
    row = conn.execute(sql, (candidate_id, run_id)).fetchone()
    if row is None:
        return None
    return dict(row)
