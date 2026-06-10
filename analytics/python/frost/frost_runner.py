"""
frost_runner.py
---------------
FROST Meta-Fitness Engine のエンドツーエンドオーケストレーター。

処理フロー:
  1. 候補受け取り & FrostCandidate 正規化
  2. バッチ評価 (frost_selector.py)
  3. ランキング + near-duplicate 抑制 (frost_ranker.py)
  4. 最終ポリシー適用 (frost_decision_engine.py)
  5. PostgreSQL 書き込み (postgres_frost_writer.py)
  6. Audit events 発行 (postgres_frost_audit_bridge.py)
  7. FrostRunOutput 返却

設計原則:
  - dry_run=True 時は frost_promotion_bridges / knowledge_artifacts に書かない
  - dry_run=True でも frost_runs / candidates / evaluations / decisions は書く
  - trace_id を全スコープで維持
  - rerun-safe: 全 UPSERT
  - 例外は捕捉して run_status=failed + error_message に記録
"""
from __future__ import annotations

import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg

from analytics.python.frost.frost_config import FrostConfig
from analytics.python.frost.frost_contracts import (
    FrostCandidate,
    FrostRunOutput,
)
from analytics.python.frost.frost_decision_engine import apply_final_policy
from analytics.python.frost.frost_ranker import assign_decisions
from analytics.python.frost.frost_selector import evaluate_candidates_batch
from analytics.python.io.postgres_frost_writer import write_frost_run_output


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# メイン実行関数
# ---------------------------------------------------------------------------

def run_frost_pipeline(
    candidates: List[FrostCandidate],
    config: FrostConfig,
    conn: Optional[psycopg.Connection] = None,
    trace_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> FrostRunOutput:
    """
    FROST パイプライン全体を実行する。

    Parameters
    ----------
    candidates : list of FrostCandidate
        評価対象の候補リスト
    config : FrostConfig
        FROST 設定
    conn : psycopg.Connection, optional
        PostgreSQL 接続。None の場合は config.effective_pg_dsn() から接続する。
    trace_id : str, optional
        実行 trace_id。未指定時は UUID 生成。
    run_id : str, optional
        実行 run_id。未指定時は UUID 生成。

    Returns
    -------
    FrostRunOutput
        実行結果。
    """
    run_id   = run_id   or str(uuid.uuid4())
    trace_id = trace_id or str(uuid.uuid4())
    started_at = _now()

    # run_id / trace_id を候補に設定 (未設定の場合)
    for c in candidates:
        if not c.run_id:
            c.run_id = run_id
        if not c.trace_id:
            c.trace_id = trace_id

    output = FrostRunOutput(
        run_id=run_id,
        trace_id=trace_id,
        batch_label=config.batch_label,
        engine_version=config.engine_version,
        candidates=candidates,
        candidate_count=len(candidates),
        config_snapshot=config.to_dict(),
        dry_run=config.dry_run,
        started_at=started_at,
        status="running",
    )

    # FROST が無効の場合は全 HOLD
    if not config.enabled:
        for c in candidates:
            from analytics.python.frost.frost_contracts import FrostDecision
            output.decisions.append(FrostDecision(
                run_id=run_id,
                candidate_id=c.candidate_id,
                trace_id=trace_id,
                decision="HOLD",
                decision_reason="FROST disabled (FROST_ENABLED=0)",
            ))
        output.hold_count = len(candidates)
        output.status = "skipped"
        output.ended_at = _now()
        _write_output(output, config, conn)
        return output

    try:
        # ── Step 1: 評価 ────────────────────────────────────────────────
        evaluations = evaluate_candidates_batch(candidates, run_id, trace_id, config)
        output.evaluations = evaluations
        output.evaluated_count = len(evaluations)

        # ── Step 2: ランキング + 決定 ──────────────────────────────────
        raw_decisions = assign_decisions(candidates, evaluations, config)

        # ── Step 3: 最終ポリシー ────────────────────────────────────────
        decisions, stats = apply_final_policy(raw_decisions, evaluations, config)
        output.decisions = decisions

        # 統計を output に反映
        output.selected_count  = stats["selected_count"]
        output.hold_count      = stats["hold_count"]
        output.rejected_count  = stats["rejected_count"]
        output.promotion_count = stats["promotion_eligible_count"]

        output.status    = "dry_run" if config.dry_run else "completed"
        output.ended_at  = _now()

    except Exception as exc:
        output.status        = "failed"
        output.error_message = f"{type(exc).__name__}: {exc}"
        output.ended_at      = _now()
        if config.verbose:
            traceback.print_exc()

    # ── Step 4: DB 書き込み ────────────────────────────────────────────
    _write_output(output, config, conn)

    return output


# ---------------------------------------------------------------------------
# DB 書き込みヘルパー
# ---------------------------------------------------------------------------

def _write_output(
    output: FrostRunOutput,
    config: FrostConfig,
    conn: Optional[psycopg.Connection],
) -> None:
    """
    FrostRunOutput を PostgreSQL に書き込む。
    接続が渡されていない場合は新規接続を作成する。
    """
    try:
        if conn is not None:
            write_frost_run_output(conn, output)
        else:
            dsn = config.effective_pg_dsn()
            with psycopg.connect(dsn) as new_conn:
                write_frost_run_output(new_conn, output)
    except Exception as exc:
        if config.verbose:
            print(f"[frost_runner] DB write error: {exc}")
            traceback.print_exc()
        # DB 書き込み失敗はサイレントに記録 (output.status は変更しない)
        output.error_message = (
            (output.error_message or "") + f" | DB write error: {exc}"
        ).strip(" |")


# ---------------------------------------------------------------------------
# 便利関数: EML 出力から FrostCandidate リストを生成
# ---------------------------------------------------------------------------

def frost_candidates_from_eml(
    eml_candidates: List[Any],
    run_id: str,
    trace_id: str,
    horizon: str = "5d",
) -> List[FrostCandidate]:
    """
    EMLCandidate (eml_search.py) のリストを FrostCandidate に変換する。

    Parameters
    ----------
    eml_candidates : list of EMLCandidate
    run_id : str
    trace_id : str
    horizon : str

    Returns
    -------
    list of FrostCandidate
    """
    result = []
    for eml_c in eml_candidates:
        # EMLCandidate の属性名を吸収
        candidate_id       = str(getattr(eml_c, "candidate_id", uuid.uuid4()))
        formula_text       = str(getattr(eml_c, "compiled_expr", "") or "")
        fitness_score      = float(getattr(eml_c, "fitness_score", 0.0) or 0.0)
        meta               = dict(getattr(eml_c, "metadata", {}) or {})
        source_cid         = str(getattr(eml_c, "candidate_id", ""))
        node               = getattr(eml_c, "node", None)
        complexity_score   = float(getattr(node, "depth", 2)) / 4.0 if node else 0.5

        # backtest_summary / metrics を metadata から取り出す
        backtest_summary = meta.get("backtest_summary", {})
        metrics          = meta.get("metrics", {})
        if not backtest_summary and fitness_score != 0.0:
            backtest_summary = {
                "oos_sharpe": fitness_score,
                "sharpe":     fitness_score,
            }

        fc = FrostCandidate(
            candidate_id=candidate_id,
            run_id=run_id,
            trace_id=trace_id,
            source_type="eml",
            source_candidate_id=source_cid,
            formula_text=formula_text,
            real_safe_formula_text=formula_text,
            feature_spec_json=meta,
            complexity_score=complexity_score,
            horizon=horizon,
            candidate_hash=str(hash(formula_text))[:16],
            backtest_summary=backtest_summary,
            metrics=metrics,
            fold_results=meta.get("fold_results", []),
            regime_breakdown=meta.get("regime_breakdown", {}),
        )
        result.append(fc)
    return result
