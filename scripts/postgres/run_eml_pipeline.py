#!/usr/bin/env python3
"""
run_eml_pipeline.py
-------------------
EML alpha discovery & backtest パイプライン マスタースクリプト。

実行フロー:
  Phase A : ターミナルセット構築 (event_study_summaries から)
  Phase B : EML 探索 (exhaustive + gradient)
  Phase C : 評価 (5 指標グループ)
  Phase D : バックテスト (walk-forward)
  Phase E : プロモーション (Q.E.D. チェーン)
  Phase F : audit_events 最終記録

使用:
  export QED_PG_DSN="postgresql://postgres:postgres@localhost:5432/qed_dev"
  export EML_ALPHA_ENABLED=1
  python scripts/postgres/run_eml_pipeline.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

import pandas as pd
import psycopg

# プロジェクトルートを PYTHONPATH に追加
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from analytics.python.frost.run_context import RunContext  # Phase 5 統合
from analytics.python.alpha.eml.eml_master_formula import (
    EMLDiscoveryConfig,
    run_eml_discovery,
)
from analytics.python.alpha.eml.eml_fitness import compute_fitness
from analytics.python.alpha.promotion_bridge import promote_batch
from analytics.python.backtest.harness import WalkForwardConfig, WalkForwardHarness
from analytics.python.features.build_terminal_set import (
    build_terminal_features,
    get_terminal_set_from_env,
    select_terminals,
)
from analytics.python.features.regime_features import build_crisis_mask
from analytics.python.io.postgres_eml_alpha_writer import (
    upsert_alpha_candidates,
    upsert_alpha_run,
)
from analytics.python.alpha.eml.eml_lineage import (
    eml_family_key,
    trial_batches_from_eml_output,
)
from analytics.python.pg_io.postgres_lineage_bridge import (
    fetch_trial_snapshot,
    insert_trial_batches,
    ledger_tables_exist,
)
from analytics.python.frost.policy_spec import load_policy_spec
from analytics.python.frost.promotion_gates import (
    MODE_OFF,
    PromotionEvidence,
    PromotionGateEngine,
)
from analytics.python.pg_io.postgres_promotion_evidence import fetch_promoted_signals
from analytics.python.io.postgres_eml_backtest_writer import (
    upsert_backtest_folds,
    upsert_backtest_run,
)

# ------------------------------------------------------------------ #
# ロギング設定
# ------------------------------------------------------------------ #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("eml_pipeline")


# ------------------------------------------------------------------ #
# ユーティリティ
# ------------------------------------------------------------------ #

def _get_dsn() -> str:
    dsn = os.environ.get("QED_PG_DSN", "")
    if not dsn:
        raise RuntimeError("QED_PG_DSN 環境変数が未設定です。")
    return dsn


def _load_panel(conn: psycopg.Connection, limit: int = 5000) -> pd.DataFrame:
    """event_study_summaries から最新パネルを取得。"""
    sql = """
        SELECT
            s.abnormal_return AS metric,
            s.event_offset,
            s.benchmark_id,
            r.run_id,
            r.batch_label,
            r.created_at
        FROM event_study_summaries s
        JOIN event_study_summary_runs r ON s.run_id = r.run_id
        ORDER BY r.created_at DESC, s.event_offset
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (limit,))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    df = pd.DataFrame(rows, columns=cols)
    df["metric"] = df["metric"].astype(float)
    return df


def _evaluate_promotion_gates(conn: psycopg.Connection, output, bt_results, lineage: dict):
    """
    G1 (DSR) + G2 (ポートフォリオ相関) の昇格前ゲートを評価する。

    - 証拠: walk-forward OOS 日次ネットリターン (combined_net_returns)
      → DSR の入力 / 相関ゲートのシグナル / 昇格時に promotion_signal として保存
    - バックテストされなかった候補 (上位 5 件以外) は証拠なし = 不合格扱い
    - モードは PolicySpec.promotion_gate_mode (env FROST_PROMOTION_GATE_MODE, 既定 shadow)

    Returns
    -------
    (verdicts, signals, gates_summary)
    """
    spec = load_policy_spec()
    engine = PromotionGateEngine.from_config(spec)
    if engine.mode == MODE_OFF:
        log.info("  [gates] FROST_PROMOTION_GATE_MODE=off — 昇格前ゲートをスキップ")
        return {}, {}, {"mode": MODE_OFF}

    family_key = eml_family_key(output)
    bt_map = {c.candidate_id: bt for c, bt in bt_results}
    signals = {
        cid: [float(x) for x in bt.combined_net_returns.fillna(0.0).tolist()]
        for cid, bt in bt_map.items()
    }
    evidences = [
        PromotionEvidence(
            candidate_id=c.candidate_id,
            oos_returns=signals.get(c.candidate_id),
            signal=signals.get(c.candidate_id),
            returns_source="walk_forward_oos_net_returns" if c.candidate_id in signals else "",
        )
        for c in output.promoted
    ]

    snapshot = None
    if lineage.get("status") == "recorded":
        snapshot = fetch_trial_snapshot(conn, family_key, sr_periodicity="daily")
    promoted = fetch_promoted_signals(conn, family_key=family_key)
    verdicts = engine.evaluate_batch(evidences, snapshot=snapshot,
                                     promoted_signals=promoted.signals)
    summary = {
        "mode": engine.mode,
        "policy_hash": spec.policy_hash,
        "evaluated": len(verdicts),
        "passed": sum(1 for v in verdicts.values() if v.passed),
        "blocked": sum(1 for v in verdicts.values() if v.blocks_promotion),
        "ledger_n_trials": snapshot.n_trials if snapshot else None,
        "ledger_snapshot_hash": snapshot.snapshot_hash if snapshot else None,
        "portfolio": promoted.to_dict(),
    }
    log.info(f"  [gates] {summary}")
    for cid, v in verdicts.items():
        log.info(f"    candidate={cid[:8]} passed={v.passed} reasons={v.reason_codes}")
    return verdicts, signals, summary


def _record_trial_ledger(conn: psycopg.Connection, output) -> dict:
    """
    ADR-002 S1: 探索統計を qed_trial_batches へ追記する。

    - EML_LINEAGE_ENABLED=0 で無効化 (既定 1)
    - migration 084 未適用なら警告してスキップ (既存環境を壊さない)
    - テーブルがあるのに書き込みに失敗した場合は例外を送出する
      (黙って欠落させると N の過少計上になるため)。batch_id は決定論的なので再実行で重複しない
    """
    if os.environ.get("EML_LINEAGE_ENABLED", "1") != "1":
        log.warning("  [lineage] EML_LINEAGE_ENABLED!=1 — 試行台帳への記録をスキップ")
        return {"status": "disabled"}
    if not ledger_tables_exist(conn):
        log.warning("  [lineage] qed_trial_batches 未作成 (migration 084 未適用) — 記録をスキップ")
        return {"status": "skipped_no_table"}
    batches = trial_batches_from_eml_output(output)
    inserted = insert_trial_batches(conn, batches)
    conn.commit()
    info = {
        "status": "recorded",
        "family_key": eml_family_key(output),
        "batches": len(batches),
        "inserted": inserted,
        "n_trials": sum(b.n_trials for b in batches),
    }
    log.info(f"  [lineage] {info}")
    return info


# ------------------------------------------------------------------ #
# メインパイプライン
# ------------------------------------------------------------------ #

def run_eml_pipeline() -> dict:
    enabled = os.environ.get("EML_ALPHA_ENABLED", "0")
    if enabled != "1":
        log.warning("EML_ALPHA_ENABLED=1 が設定されていません。終了します。")
        return {"status": "skipped", "reason": "EML_ALPHA_ENABLED != 1"}

    # Phase 5: RunContext でコンテキストを一元管理 (D5 負債解消)
    # 環境変数: EML_RUN_ID / EML_TRACE_ID / EML_BATCH_LABEL / EML_DRY_RUN / EML_VERBOSE
    ctx = RunContext.from_env(pipeline="eml", prefix="EML_")
    # 後方互換: EML_ALPHA_DRY_RUN も読む
    if os.environ.get("EML_ALPHA_DRY_RUN", "0") == "1":
        ctx.dry_run = True

    run_id   = ctx.run_id
    trace_id = ctx.trace_id
    dry_run  = ctx.dry_run

    log.info(f"=== EML Pipeline Start ===")
    log.info(f"  run_id   = {run_id}")
    log.info(f"  trace_id = {trace_id}")
    log.info(f"  dry_run  = {dry_run}")

    dsn = _get_dsn()
    conn = psycopg.connect(dsn)

    try:
        # ---------------------------------------------------------- #
        # Phase A: ターミナルセット構築
        # ---------------------------------------------------------- #
        log.info("Phase A: Terminal set 構築")
        panel_df = _load_panel(conn)
        log.info(f"  パネル行数: {len(panel_df)}")

        if len(panel_df) < 50:
            log.warning("パネルデータが不足 (<50 行)。テスト用合成データを使用。")
            import numpy as np
            rng = np.random.default_rng(42)
            n = 500
            panel_df = pd.DataFrame({
                "metric": rng.normal(0, 0.02, n),
                "run_id": "synthetic",
            })

        features = build_terminal_features(panel_df)
        terminals = get_terminal_set_from_env()
        terminal_df = select_terminals(features, terminals)

        target = panel_df["metric"].astype(float)
        # インデックスを整数に統一
        terminal_df = terminal_df.reset_index(drop=True)
        target      = target.reset_index(drop=True)

        # regime mask
        crisis_mask = build_crisis_mask(target)

        log.info(f"  terminals: {len(terminals)}, rows: {len(terminal_df)}")

        # ---------------------------------------------------------- #
        # Phase B: EML 探索
        # ---------------------------------------------------------- #
        log.info("Phase B: EML 探索 (exhaustive + gradient)")
        config = EMLDiscoveryConfig(
            run_id=run_id,
            trace_id=trace_id,
            batch_label=os.environ.get("EML_BATCH_LABEL", "eml_v1"),
            target_horizon=os.environ.get("EML_ALPHA_TARGET_HORIZON", "5d"),
            max_depth=int(os.environ.get("EML_ALPHA_MAX_DEPTH", "3")),
            terminal_set=terminals,
        )

        output = run_eml_discovery(
            config=config,
            feature_df=terminal_df,
            target=target,
            regime_mask=crisis_mask,
        )
        log.info(
            f"  探索完了: total={output.total_searched}, "
            f"promoted={len(output.promoted)}, rejected={len(output.rejected)}"
        )

        # ---------------------------------------------------------- #
        # Phase C: DB 書き込み (alpha run + candidates)
        # ---------------------------------------------------------- #
        log.info("Phase C: DB 書き込み")
        upsert_alpha_run(conn, output)
        upsert_alpha_candidates(conn, output.candidates)
        log.info(f"  eml_alpha_runs / eml_alpha_candidates UPSERT 完了")

        # ---------------------------------------------------------- #
        # Phase C': ADR-002 試行台帳 (DSR の試行数 N)
        #   dry_run でも記録する: 探索は実際に行われており、過少計上は DSR を楽観化する
        # ---------------------------------------------------------- #
        lineage = _record_trial_ledger(conn, output)

        # ---------------------------------------------------------- #
        # Phase D: バックテスト (walk-forward)
        # ---------------------------------------------------------- #
        log.info("Phase D: Walk-forward バックテスト")
        wf_config   = WalkForwardConfig.from_env()
        wf_harness  = WalkForwardHarness(wf_config)
        bt_results  = []

        for c in output.promoted[:5]:  # 上位5候補をバックテスト
            from analytics.python.alpha.eml.eml_runtime_lower import lower_and_rank_normalize
            from analytics.python.alpha.eml.eml_compiler import compile_to_expr
            signal = lower_and_rank_normalize(c.compiled_expr, terminal_df)

            bt_result = wf_harness.run(
                signal=signal,
                returns=target,
                run_id=run_id,
                trace_id=trace_id,
                crisis_mask=crisis_mask,
            )
            bt_results.append((c, bt_result))
            upsert_backtest_run(conn, bt_result, c.candidate_id)
            upsert_backtest_folds(conn, bt_result.folds)
            log.info(
                f"  backtest: candidate={c.candidate_id[:8]}, "
                f"sharpe={bt_result.overall_sharpe:.4f}, "
                f"mdd={bt_result.overall_max_drawdown:.4f}, "
                f"gate_triggers={bt_result.gate_trigger_count}"
            )

        # ---------------------------------------------------------- #
        # Phase E: プロモーション
        # ---------------------------------------------------------- #
        log.info("Phase E: Promotion → Q.E.D. チェーン")
        gate_verdicts, promo_signals, gates_summary = _evaluate_promotion_gates(
            conn, output, bt_results, lineage,
        )
        promo_results = promote_batch(
            conn=conn,
            candidates=output.promoted,
            eval_results=output.eval_results,
            dry_run=dry_run,
            gate_verdicts=gate_verdicts,
            promotion_signals=promo_signals,
            signal_family_key=eml_family_key(output),
        )

        applied  = [r for r in promo_results if r["decision"] == "APPLIED"]
        rejected = [r for r in promo_results if r["decision"] == "REJECTED"]
        dry_runs = [r for r in promo_results if r["decision"] == "DRY_RUN"]

        log.info(
            f"  プロモーション: APPLIED={len(applied)}, "
            f"REJECTED={len(rejected)}, DRY_RUN={len(dry_runs)}"
        )

        # ---------------------------------------------------------- #
        # Phase F: 最終サマリー
        # ---------------------------------------------------------- #
        summary = {
            "status":          "completed",
            "run_id":          run_id,
            "trace_id":        trace_id,
            "dry_run":         dry_run,
            "panel_rows":      len(panel_df),
            "terminals":       len(terminals),
            "total_searched":  output.total_searched,
            "promoted":        len(output.promoted),
            "rejected_search": len(output.rejected),
            "backtest_runs":   len(bt_results),
            "promo_applied":   len(applied),
            "promo_rejected":  len(rejected),
            "promo_dry_run":   len(dry_runs),
            "terminal_set_hash": output.terminal_set_hash,
            "n_trials_evaluated": output.n_trials_evaluated,
            "n_trials":        output.n_trials,
            "lineage":         lineage,
            "promotion_gates": gates_summary,
        }

        log.info("=== EML Pipeline Completed ===")
        log.info(json.dumps(summary, indent=2))
        print(f"RUN_ID={run_id}")
        print(f"TRACE_ID={trace_id}")
        print(f"PROMOTED={len(output.promoted)}")
        print(f"SUMMARY={json.dumps(summary)}")

        return summary

    finally:
        conn.close()


# ------------------------------------------------------------------ #

if __name__ == "__main__":
    result = run_eml_pipeline()
    # "completed" (promoted>=0 を含む) と "skipped" は正常終了
    sys.exit(0 if result.get("status") in ("completed", "skipped") else 1)
