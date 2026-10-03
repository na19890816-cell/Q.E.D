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
  0. 昇格前ゲート (G1 DSR + G2 ポートフォリオ相関) — gate_verdict が渡された場合
       shadow : 判定結果を audit.metadata.promotion_gates に記録し、昇格は継続
       enforce: 不合格なら knowledge_artifacts へ登録せず REJECTED を audit
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
from analytics.python.frost.promotion_gates import PromotionGateVerdict
from analytics.python.pg_io.postgres_promotion_evidence import (
    BASIS_KEY,
    SIGNAL_KEY,
    encode_signal,
    make_signal_basis,
)


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
    gate_verdict: Optional[PromotionGateVerdict] = None,
    promotion_signal: Optional[List[float]] = None,
    signal_family_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    EML 候補を Q.E.D. チェーンへ昇格させる。

    Parameters
    ----------
    gate_verdict : PromotionGateVerdict, optional
        昇格前ゲートの判定。None なら従来どおりゲートなしで昇格する。
    promotion_signal : list of float, optional
        knowledge_artifacts.metadata.promotion_signal に保存するシグナル
        (後続候補の G2 相関ゲートの比較対象になる)。
    signal_family_key : str, optional
        シグナルのデータ基盤 (ADR-002 family_key)。同一基盤の artifact 同士のみ比較される。

    Returns
    -------
    dict: bridge_id, artifact_id, audit_event_id, decision, candidate_id, trace_id
          (+ gate_verdict / rejection_reason)
    """
    bridge_id    = str(uuid.uuid4())
    artifact_id  = str(uuid.uuid4())
    trace_id     = candidate.trace_id
    candidate_id = candidate.candidate_id
    case_id      = f"{EML_CASE_ID_PREFIX}-{candidate_id[:8]}"

    decision = "DRY_RUN" if dry_run else "APPLIED"
    gates_meta = gate_verdict.to_dict() if gate_verdict is not None else None

    # ---- Step 0: 昇格前ゲート (enforce で不合格なら登録しない) ----
    if gate_verdict is not None and gate_verdict.blocks_promotion:
        return _reject_by_gate(conn, candidate, eval_result, bridge_id, case_id,
                               gate_verdict, dry_run)

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
                conn, artifact_id, trace_id, candidate, eval_result, dry_run=dry_run,
                promotion_signal=promotion_signal, gates_meta=gates_meta,
                signal_family_key=signal_family_key,
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
                "promotion_gates": gates_meta,
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
            "gate_verdict":    gates_meta,
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
    gate_verdicts: Optional[Dict[str, PromotionGateVerdict]] = None,
    promotion_signals: Optional[Dict[str, List[float]]] = None,
    signal_family_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    複数候補を一括プロモーション。

    gate_verdicts / promotion_signals は candidate_id をキーとする辞書。
    未指定なら従来どおり (ゲートなし)。
    """
    eval_map = {e.candidate_id: e for e in eval_results}
    verdicts = gate_verdicts or {}
    signals = promotion_signals or {}
    results = []
    for c in candidates:
        ev = eval_map.get(c.candidate_id)
        r = promote_alpha_candidate(
            conn, c, ev,
            dry_run=dry_run,
            experiment_run_id=experiment_run_id,
            gate_verdict=verdicts.get(c.candidate_id),
            promotion_signal=signals.get(c.candidate_id),
            signal_family_key=signal_family_key,
        )
        results.append(r)
    return results


# ------------------------------------------------------------------ #
# 内部ヘルパー
# ------------------------------------------------------------------ #

def _reject_by_gate(
    conn: psycopg.Connection,
    candidate: EMLCandidate,
    eval_result: Optional[EMLEvaluationResult],
    bridge_id: str,
    case_id: str,
    verdict: PromotionGateVerdict,
    dry_run: bool,
) -> Dict[str, Any]:
    """
    enforce モードでゲート不合格の候補を REJECTED として audit に記録する。
    knowledge_artifacts には登録しない。dry_run でも audit は残す
    (他の DRY_RUN 判定と同様、判断の記録は副作用ではなく監査証跡)。
    """
    meta = {
        "candidate_id":    candidate.candidate_id,
        "bridge_id":       bridge_id,
        "fitness_score":   candidate.fitness_score,
        "compiled_expr":   candidate.compiled_expr,
        "rank_ic":         eval_result.rank_ic if eval_result else 0.0,
        "sharpe":          eval_result.sharpe if eval_result else 0.0,
        "dry_run":         dry_run,
        "promotion_gates": verdict.to_dict(),
    }
    if not dry_run:
        _upsert_promotion_bridge(
            conn, bridge_id, candidate.candidate_id, candidate.trace_id,
            bridge_status="rejected", fitness_score=candidate.fitness_score,
        )
    audit_event_id = _emit_audit(
        conn,
        trace_id=candidate.trace_id,
        case_id=case_id,
        object_id=candidate.candidate_id,
        decision="REJECTED",
        decision_reason_code="PROMOTION_GATE_FAILED",
        reject_reason_code=verdict.primary_reason,
        metadata=meta,
    )
    return {
        "bridge_id":        bridge_id,
        "artifact_id":      None,
        "audit_event_id":   audit_event_id,
        "decision":         "REJECTED",
        "rejection_reason": ",".join(verdict.reason_codes),
        "candidate_id":     candidate.candidate_id,
        "trace_id":         candidate.trace_id,
        "gate_verdict":     verdict.to_dict(),
    }


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
    promotion_signal: Optional[List[float]] = None,
    gates_meta: Optional[Dict[str, Any]] = None,
    signal_family_key: Optional[str] = None,
) -> None:
    """knowledge_artifacts に EML artifact を記録 (実スキーマ準拠)。"""
    title   = f"EML Alpha: {candidate.compiled_expr[:80]}"
    # NOTE: 旧実装は f"{x:.4f if ev else 0.0:.4f}" という不正な書式指定で、
    #       eval_result の有無にかかわらず例外 → 昇格が常に PROMOTION_EXCEPTION で失敗していた
    rank_ic_v = eval_result.rank_ic if eval_result else 0.0
    sharpe_v  = eval_result.sharpe  if eval_result else 0.0
    summary = (
        f"EML alpha candidate promoted. "
        f"fitness={candidate.fitness_score:.4f}, "
        f"rank_ic={rank_ic_v:.4f}, "
        f"sharpe={sharpe_v:.4f}"
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
    meta_d: Dict[str, Any] = {
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
    }
    if promotion_signal is not None:
        meta_d[SIGNAL_KEY] = encode_signal(promotion_signal)
        meta_d[BASIS_KEY] = make_signal_basis(signal_family_key or "", len(promotion_signal))
    if gates_meta is not None:
        meta_d["promotion_gates"] = gates_meta
    meta = json.dumps(meta_d)

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
