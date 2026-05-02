"""
promotion_bridge.py
-------------------
EML alpha 候補を Q.E.D. チェーンへ昇格させるプロモーションブリッジ。

実際のDBスキーマに合わせた実装:
  audit_events          : id(uuid), trace_id, case_id, object_type, object_id,
                          requested_by, event_type, decision, decision_reason_code, metadata
  knowledge_artifacts   : artifact_id(text), trace_id, artifact_type, title, summary,
                          body_markdown, metadata, status
  artifact_links        : artifact_id(text), trace_id, target_type, target_id(uuid),
                          target_code, resolution_method, link_status, metadata
  event_study_experiment_report_bridge : run_id(text), trace_id, report_title,
                                         report_summary, report_metadata, promotion_status
  eml_alpha_promotion_bridge : bridge_id, candidate_id, trace_id, bridge_status,
                                fitness_score, report_id, artifact_id, link_id

フロー:
  1. eml_alpha_promotion_bridge に UPSERT (bridge_status = 'pending')
  2. knowledge_artifacts へ記録
  3. audit_events に APPLIED / REJECTED / DRY_RUN を記録
  4. eml_alpha_promotion_bridge.bridge_status を更新

trace_id は EML run から全フェーズに伝播する。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

import psycopg

from analytics.python.alpha.eml.eml_search import EMLCandidate
from analytics.python.alpha.eml.eml_evaluation_runner import EMLEvaluationResult


# ------------------------------------------------------------------ #
# 定数
# ------------------------------------------------------------------ #

ALLOWED_DECISIONS = {"APPLIED", "REJECTED", "CONFLICTED", "DRY_RUN"}
EML_REQUESTED_BY  = "eml_promotion_bridge"
EML_OBJECT_TYPE   = "eml_alpha_candidate"
EML_CASE_ID_PREFIX = "eml-case"


# ------------------------------------------------------------------ #
# メインプロモーション関数
# ------------------------------------------------------------------ #

def promote_alpha_candidate(
    conn: psycopg.Connection,
    candidate: EMLCandidate,
    eval_result: Optional[EMLEvaluationResult],
    dry_run: bool = False,
    experiment_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    EML 候補を Q.E.D. チェーンへ昇格させる。

    Returns
    -------
    dict: bridge_id, artifact_id, audit_event_id, decision, candidate_id, trace_id
    """
    bridge_id    = str(uuid.uuid4())
    artifact_id  = str(uuid.uuid4())
    trace_id     = candidate.trace_id
    candidate_id = candidate.candidate_id
    case_id      = f"{EML_CASE_ID_PREFIX}-{candidate_id[:8]}"

    decision = "DRY_RUN" if dry_run else "APPLIED"

    try:
        # ---- Step 1: bridge 初期化 (dry_run では書き込み不要) ----
        if not dry_run:
            _upsert_promotion_bridge(
                conn, bridge_id, candidate_id, trace_id,
                bridge_status="pending",
                fitness_score=candidate.fitness_score,
            )

        # ---- Step 2: knowledge_artifacts (dry_run では書き込み不要) ----
        if not dry_run:
            _insert_knowledge_artifact(
                conn, artifact_id, trace_id, candidate, eval_result, dry_run=dry_run
            )

        # ---- Step 3: audit_events ----
        audit_event_id = _emit_audit(
            conn,
            trace_id=trace_id,
            case_id=case_id,
            object_id=candidate_id,
            decision=decision,
            decision_reason_code="EML_FITNESS_THRESHOLD_MET" if decision == "APPLIED" else "DRY_RUN_MODE",
            metadata={
                "candidate_id":  candidate_id,
                "bridge_id":     bridge_id,
                "artifact_id":   artifact_id,
                "fitness_score": candidate.fitness_score,
                "compiled_expr": candidate.compiled_expr,
                "rank_ic":       eval_result.rank_ic if eval_result else 0.0,
                "sharpe":        eval_result.sharpe  if eval_result else 0.0,
                "dry_run":       dry_run,
            },
        )

        # ---- Step 4: bridge 完了 (dry_run では更新不要) ----
        if not dry_run:
            bridge_status = "applied"
            _update_bridge_status(
                conn, bridge_id,
                report_id=None,
                artifact_id=artifact_id,
                link_id=None,
                status=bridge_status,
            )

        return {
            "bridge_id":       bridge_id,
            "artifact_id":     artifact_id,
            "audit_event_id":  audit_event_id,
            "decision":        decision,
            "candidate_id":    candidate_id,
            "trace_id":        trace_id,
        }

    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass

        rejection_reason = f"PROMOTION_ERROR: {e}"

        # 失敗時は REJECTED を audit
        try:
            audit_event_id = _emit_audit(
                conn,
                trace_id=trace_id,
                case_id=case_id,
                object_id=candidate_id,
                decision="REJECTED",
                decision_reason_code="PROMOTION_EXCEPTION",
                metadata={"error": str(e), "candidate_id": candidate_id},
            )
            _update_bridge_status(conn, bridge_id, None, None, None, status="rejected")
        except Exception:
            audit_event_id = None

        return {
            "bridge_id":         bridge_id,
            "artifact_id":       None,
            "audit_event_id":    audit_event_id,
            "decision":          "REJECTED",
            "rejection_reason":  rejection_reason,
            "candidate_id":      candidate_id,
            "trace_id":          trace_id,
        }


def promote_batch(
    conn: psycopg.Connection,
    candidates: List[EMLCandidate],
    eval_results: List[EMLEvaluationResult],
    dry_run: bool = False,
    experiment_run_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """複数候補を一括プロモーション。"""
    eval_map = {e.candidate_id: e for e in eval_results}
    results = []
    for c in candidates:
        ev = eval_map.get(c.candidate_id)
        r = promote_alpha_candidate(
            conn, c, ev,
            dry_run=dry_run,
            experiment_run_id=experiment_run_id,
        )
        results.append(r)
    return results


# ------------------------------------------------------------------ #
# 内部ヘルパー
# ------------------------------------------------------------------ #

def _upsert_promotion_bridge(
    conn: psycopg.Connection,
    bridge_id: str,
    candidate_id: str,
    trace_id: str,
    bridge_status: str,
    fitness_score: float,
) -> None:
    sql = (
        "INSERT INTO eml_alpha_promotion_bridge "
        "(bridge_id, candidate_id, trace_id, bridge_status, fitness_score, "
        " metadata, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, now(), now()) "
        "ON CONFLICT (candidate_id) DO UPDATE SET "
        "  bridge_id     = EXCLUDED.bridge_id, "
        "  bridge_status = EXCLUDED.bridge_status, "
        "  fitness_score = EXCLUDED.fitness_score, "
        "  updated_at    = now()"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (bridge_id, candidate_id, trace_id, bridge_status, fitness_score))
    conn.commit()


def _insert_knowledge_artifact(
    conn: psycopg.Connection,
    artifact_id: str,
    trace_id: str,
    candidate: EMLCandidate,
    eval_result: Optional[EMLEvaluationResult],
    dry_run: bool,
) -> None:
    """knowledge_artifacts に EML artifact を記録 (実スキーマ準拠)。"""
    title   = f"EML Alpha: {candidate.compiled_expr[:80]}"
    summary = (
        f"EML alpha candidate promoted. "
        f"fitness={candidate.fitness_score:.4f}, "
        f"rank_ic={eval_result.rank_ic:.4f if eval_result else 0.0:.4f}, "
        f"sharpe={eval_result.sharpe:.4f if eval_result else 0.0:.4f}"
    )
    body_md = (
        f"## EML Alpha Report\n\n"
        f"**Expression**: `{candidate.compiled_expr}`\n\n"
        f"**Fitness**: {candidate.fitness_score:.4f}\n\n"
        f"**Rank IC**: {eval_result.rank_ic if eval_result else 0.0:.4f}\n\n"
        f"**Sharpe**: {eval_result.sharpe if eval_result else 0.0:.4f}\n\n"
        f"**Tree Depth**: {candidate.tree_depth()}\n\n"
        f"**Node Count**: {candidate.node_count()}\n\n"
        f"**DRY_RUN**: {dry_run}\n"
    )
    meta = json.dumps({
        "candidate_id":  candidate.candidate_id,
        "run_id":        candidate.run_id,
        "compiled_expr": candidate.compiled_expr,
        "fitness_score": candidate.fitness_score,
        "tree_depth":    candidate.tree_depth(),
        "node_count":    candidate.node_count(),
        "tree_json":     candidate.node.to_json(),
        "dry_run":       dry_run,
        "rank_ic":       eval_result.rank_ic    if eval_result else 0.0,
        "sharpe":        eval_result.sharpe     if eval_result else 0.0,
        "max_drawdown":  eval_result.max_drawdown if eval_result else 0.0,
    })

    sql = (
        "INSERT INTO knowledge_artifacts "
        "(artifact_id, trace_id, source_run_id, artifact_type, artifact_tag, "
        " title, summary, body_markdown, metadata, status, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, now(), now()) "
        "ON CONFLICT (artifact_id) DO UPDATE SET "
        "  title        = EXCLUDED.title, "
        "  summary      = EXCLUDED.summary, "
        "  body_markdown = EXCLUDED.body_markdown, "
        "  metadata     = EXCLUDED.metadata, "
        "  status       = EXCLUDED.status, "
        "  updated_at   = now()"
    )
    status = "draft"  # EML は draft で登録
    with conn.cursor() as cur:
        cur.execute(sql, (
            artifact_id, trace_id, candidate.run_id,
            "eml_alpha_candidate", "eml_alpha",
            title, summary, body_md, meta, status,
        ))
    conn.commit()


def _emit_audit(
    conn: psycopg.Connection,
    trace_id: str,
    case_id: str,
    object_id: str,
    decision: str,
    decision_reason_code: str,
    metadata: Dict[str, Any] | None = None,
    reject_reason_code: Optional[str] = None,
) -> str:
    """audit_events に INSERT (実スキーマ準拠)。"""
    if decision not in ALLOWED_DECISIONS:
        raise ValueError(
            f"audit decision '{decision}' は許可されていません。"
            f" 許可値: {sorted(ALLOWED_DECISIONS)}"
        )

    event_type = f"EML_PROMOTION_{decision}"
    sql = (
        "INSERT INTO audit_events "
        "(trace_id, case_id, object_type, object_id, requested_by, "
        " event_type, decision, decision_reason_code, reject_reason_code, "
        " metadata, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now()) "
        "RETURNING id"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (
            trace_id,
            case_id,
            EML_OBJECT_TYPE,
            object_id,
            EML_REQUESTED_BY,
            event_type,
            decision,
            decision_reason_code,
            reject_reason_code,
            json.dumps(metadata or {}),
        ))
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def _update_bridge_status(
    conn: psycopg.Connection,
    bridge_id: str,
    report_id: Optional[str],
    artifact_id: Optional[str],
    link_id: Optional[str],
    status: str,
) -> None:
    sql = (
        "UPDATE eml_alpha_promotion_bridge SET "
        "  bridge_status = %s, "
        "  report_id     = %s, "
        "  artifact_id   = %s, "
        "  link_id       = %s, "
        "  updated_at    = now() "
        "WHERE bridge_id = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (status, report_id, artifact_id, link_id, bridge_id))
    conn.commit()
