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
import uuid
from datetime import datetime, timezone

import pandas as pd
import psycopg

# プロジェクトルートを PYTHONPATH に追加
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

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


# ------------------------------------------------------------------ #
# メインパイプライン
# ------------------------------------------------------------------ #

def run_eml_pipeline() -> dict:
    enabled = os.environ.get("EML_ALPHA_ENABLED", "0")
    if enabled != "1":
        log.warning("EML_ALPHA_ENABLED=1 が設定されていません。終了します。")
        return {"status": "skipped", "reason": "EML_ALPHA_ENABLED != 1"}

    dry_run = os.environ.get("EML_ALPHA_DRY_RUN", "0") == "1"
    trace_id = os.environ.get(
        "EML_TRACE_ID",
        str(uuid.uuid5(uuid.NAMESPACE_DNS, f"eml-{datetime.now(timezone.utc).isoformat()}")),  # noqa: DTZ
    )
    run_id = os.environ.get(
        "EML_RUN_ID",
        f"eml_v1__{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
    )

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
        promo_results = promote_batch(
            conn=conn,
            candidates=output.promoted,
            eval_results=output.eval_results,
            dry_run=dry_run,
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
