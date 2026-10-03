"""
frost_runner.py
---------------
FROST Meta-Fitness Engine のエンドツーエンドオーケストレーター。

処理フロー:
  1. 候補受け取り & FrostCandidate 正規化
  2. PolicySpec スナップショット生成 & qed_policies upsert (Phase 1)
  3. バッチ評価 (frost_selector.py)
  4. ランキング + near-duplicate 抑制 (frost_ranker.py)
  5. 最終ポリシー適用 (frost_decision_engine.py)
  6. PostgreSQL 書き込み (postgres_frost_writer.py)
  7. frost_runs.policy_hash 更新 (Phase 1)
  8. Audit events 発行 (postgres_frost_audit_bridge.py)
  9. FrostRunOutput 返却

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
from analytics.python.frost.policy_spec import (
    PolicySpec,
    policy_spec_from_frost_config,
)
from analytics.python.frost.run_context import RunContext
from analytics.python.io.postgres_frost_writer import write_frost_run_output
from analytics.python.pg_io.postgres_policy_bridge import (
    upsert_policy_spec,
    set_run_policy_hash,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def stable_candidate_hash(formula_text: Optional[str]) -> str:
    """
    プロセス非依存の candidate_hash (16 hex)。

    修正 (2026-10-03, ADR-002 L3): 旧実装 str(hash(formula_text))[:16] は PYTHONHASHSEED に
    依存し、同じ式でもプロセスごとに値が変わっていた。影響:
      - run 間で同一式を同定できない / golden の「candidate_hash は安定」前提が不成立
      - DedupStage の hash 前方一致類似度が乱数化 (near_duplicate_threshold を下げると
        抑制される候補集合が PYTHONHASHSEED で変わることを実測)
    frost_lineage.formula_hash と同じ正規化 (空白除去) + SHA-256 の先頭 16 hex を使う。
    空式は "empty" 固定 (旧実装でも hash("") は全空式で同値だったため意味論を維持)。
    """
    from analytics.python.frost.frost_lineage import formula_hash
    try:
        return formula_hash(formula_text)[len("sha256:"):][:16]
    except ValueError:
        return "empty"


#: RunContext の run_id (例 "frost__20261003_024025") を FROST の UUID 列へ写像する namespace
_FROST_RUN_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "qed.frost.run_id")


def normalize_frost_run_id(run_id: Optional[str]) -> str:
    """
    frost_runs.run_id (UUID 列) に格納可能な run_id を返す。

    - None / 空 → 新規 uuid4
    - UUID 文字列 → そのまま (正規化表記)
    - それ以外 (RunContext 形式 "frost__<ts>" 等) → UUID5(namespace, run_id) で決定論的に写像
      (同じ論理 run_id は常に同じ UUID になる = 再実行の UPSERT 安全性を保つ)

    修正 (2026-10-03): 旧実装は RunContext の非 UUID run_id をそのまま渡し、
    DB 書き込みが「invalid input syntax for type uuid」で必ず失敗していた。
    """
    if not run_id:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(str(run_id)))
    except ValueError:
        return str(uuid.uuid5(_FROST_RUN_ID_NAMESPACE, str(run_id)))


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
    run_id   = normalize_frost_run_id(run_id)
    trace_id = trace_id or str(uuid.uuid4())
    started_at = _now()

    # run_id / trace_id を候補に設定 (未設定の場合)
    for c in candidates:
        if not c.run_id:
            c.run_id = run_id
        if not c.trace_id:
            c.trace_id = trace_id

    # ── Phase 1: PolicySpec スナップショット生成 ──────────────────────────
    policy_spec: PolicySpec = policy_spec_from_frost_config(config)
    policy_hash: str = policy_spec.policy_hash

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
        policy_hash=policy_hash,
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

    # ── Phase 1: qed_policies upsert & frost_runs.policy_hash 更新 ─────
    _upsert_policy(output, policy_spec, config, conn)

    return output


# ---------------------------------------------------------------------------
# Phase 1: PolicySpec upsert ヘルパー
# ---------------------------------------------------------------------------

def _upsert_policy(
    output: FrostRunOutput,
    policy_spec: PolicySpec,
    config: FrostConfig,
    conn: Optional[psycopg.Connection],
) -> None:
    """
    qed_policies へ PolicySpec を upsert し、
    frost_runs.policy_hash を設定する。

    DB 接続がない場合・例外が発生した場合はサイレントにスキップ
    (policy upsert の失敗でパイプライン全体を止めない)。

    修正 (2026-10-03): dry_run でも記録する。frost_runs / evaluations / decisions は
    dry_run でも書かれる設計 (モジュール docstring) なのに、ポリシーだけ書かないと
    dry_run の run は「どの閾値で判定したか」が再現できない (D10 の再発)。
    ポリシースナップショットは破壊的書き込みではない。
    """
    _policy_dry_run = False
    try:
        if conn is not None:
            upsert_policy_spec(conn, policy_spec, dry_run=_policy_dry_run)
            set_run_policy_hash(
                conn, output.run_id, policy_spec.policy_hash,
                dry_run=_policy_dry_run,
            )
        else:
            # 接続なし: DSN が設定されていれば一時接続を試みる
            try:
                dsn = config.effective_pg_dsn()
                with psycopg.connect(dsn) as tmp_conn:
                    upsert_policy_spec(tmp_conn, policy_spec, dry_run=_policy_dry_run)
                    set_run_policy_hash(
                        tmp_conn, output.run_id, policy_spec.policy_hash,
                        dry_run=_policy_dry_run,
                    )
            except Exception:
                pass  # DSN 未設定は許容 (ローカル/テスト環境)
    except Exception as exc:
        if config.verbose:
            import traceback as _tb
            print(f"[frost_runner] policy upsert error: {exc}")
            _tb.print_exc()


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
            candidate_hash=stable_candidate_hash(formula_text),
            backtest_summary=backtest_summary,
            metrics=metrics,
            fold_results=meta.get("fold_results", []),
            regime_breakdown=meta.get("regime_breakdown", {}),
        )
        result.append(fc)
    return result


# ---------------------------------------------------------------------------
# Phase 5: RunContext ラッパー API
# ---------------------------------------------------------------------------

def run_frost_pipeline_with_context(
    candidates: List[FrostCandidate],
    config: FrostConfig,
    ctx: RunContext,
    conn: Optional[psycopg.Connection] = None,
) -> FrostRunOutput:
    """
    RunContext を使った run_frost_pipeline() の統一 API。

    Phase 5: run_id / trace_id を RunContext から一元取得して
    run_frost_pipeline() に委譲する (D5 負債解消)。

    Parameters
    ----------
    candidates : list of FrostCandidate
    config : FrostConfig
    ctx : RunContext
        実行コンテキスト。run_id / trace_id / dry_run / verbose を保持。
    conn : psycopg.Connection, optional

    Returns
    -------
    FrostRunOutput
    """
    # ctx の dry_run / verbose を config に反映する
    # (config を直接書き換えると副作用があるので上書きは最小限に)
    # dataclasses.replace を使う (FrostConfig(**__dict__) は将来の init=False フィールドで壊れる)
    from dataclasses import replace as _replace
    if ctx.dry_run and not config.dry_run:
        config = _replace(config, dry_run=True)
    if ctx.verbose and not config.verbose:
        config = _replace(config, verbose=True)

    return run_frost_pipeline(
        candidates=candidates,
        config=config,
        conn=conn,
        trace_id=ctx.trace_id,
        run_id=ctx.run_id,
    )


# ---------------------------------------------------------------------------
# __main__ エントリポイント (run_frost_engine.sh から呼び出される)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json as _json
    import sys as _sys

    from analytics.python.frost.frost_config import FrostConfig
    from analytics.python.frost.run_context import RunContext

    parser = argparse.ArgumentParser(
        description="FROST Meta-Fitness Engine — CLI エントリポイント",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="DB への destructive 書き込みをスキップ (frost_promotion_bridges 等)",
    )
    parser.add_argument(
        "--batch-label",
        type=str,
        default=None,
        help="バッチラベル (例: frost_v2). 未指定時は FROST_BATCH_LABEL 環境変数を使用",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="選抜候補数 (例: 25). 未指定時は FROST_TOP_K 環境変数 or デフォルト値を使用",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="詳細ログを出力",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="run_id を明示指定. 未指定時は自動生成",
    )
    parser.add_argument(
        "--trace-id",
        type=str,
        default=None,
        help="trace_id を明示指定. 未指定時は自動生成",
    )

    args = parser.parse_args()

    # RunContext 生成 (Phase 5 統合)
    ctx = RunContext.from_args(
        args,
        pipeline="frost",
        run_id=args.run_id,
        trace_id=args.trace_id,
    )

    # FrostConfig 生成 (環境変数ベース + CLI オプション上書き)
    # 修正 (2026-10-03): 旧コードは存在しない FrostConfig.from_env() を呼んでおり、
    #   run_frost_engine.sh 経由の CLI は起動直後に AttributeError で必ず失敗していた。
    from dataclasses import replace as _replace
    from analytics.python.frost.frost_config import load_frost_config
    _overrides = {}
    if args.dry_run:
        _overrides["dry_run"] = True
    if args.verbose:
        _overrides["verbose"] = True
    if args.batch_label:
        _overrides["batch_label"] = args.batch_label
    if args.top_k is not None:
        _overrides["top_k"] = args.top_k
    config = load_frost_config()
    if _overrides:
        config = _replace(config, **_overrides)
        config.validate()

    if config.verbose:
        print(ctx.log_header())

    # 候補なし (CLI から直接呼ぶ場合は DB から取得するフローが別途必要)
    # ここでは「候補は外部から注入する」設計のためサンプルとして空リストで起動確認のみ
    candidates: List[FrostCandidate] = []
    print("[frost_runner] WARNING: CLI は候補ローダー未実装のため 0 件で起動確認のみ行います",
          file=_sys.stderr)

    output = run_frost_pipeline_with_context(
        candidates=candidates,
        config=config,
        ctx=ctx,
    )

    result = {
        "run_id":          output.run_id,
        "trace_id":        output.trace_id,
        "status":          output.status,
        "candidate_count": output.candidate_count,
        "selected_count":  output.selected_count,
        "hold_count":      output.hold_count,
        "rejected_count":  output.rejected_count,
        "dry_run":         output.dry_run,
        "error_message":   output.error_message,
    }

    print(_json.dumps(result, indent=2, default=str))
    _sys.exit(0 if output.status in ("completed", "dry_run", "skipped") else 1)
