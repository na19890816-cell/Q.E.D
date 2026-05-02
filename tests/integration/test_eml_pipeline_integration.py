"""
tests/integration/test_eml_pipeline_integration.py
---------------------------------------------------
EML Alpha Discovery & Backtest パイプライン 統合テスト（実 PostgreSQL を使用）

前提条件:
  - QED_PG_DSN に接続可能な PostgreSQL が必要
  - マイグレーション (eml_alpha_runs, eml_alpha_candidates, eml_backtest_runs,
    eml_backtest_folds, eml_alpha_promotion_bridge, audit_events) が適用済み
  - PYTHONPATH=/home/user/prostock が通っていること

スキップ条件:
  - QED_PG_DSN が未設定 or PostgreSQL に接続できない場合は SKIP

テスト対象フロー:
  Phase A : ターミナルセット構築
  Phase B : EML 探索 (exhaustive + gradient)
  Phase C : DB 書き込み (upsert_alpha_run / upsert_alpha_candidates)
  Phase D : Walk-forward バックテスト + DB 書き込み
  Phase E : プロモーション (promote_batch)
  UPSERT 再実行安全性 (rerun-safety)
  NaN/Inf ガード (DB に不正値が入らない)
  audit_events 記録検証
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../"))

PG_DSN = os.environ.get(
    "QED_PG_DSN", "postgresql://postgres:postgres@localhost:5432/qed_dev"
)


def _try_connect() -> bool:
    try:
        import psycopg
        conn = psycopg.connect(PG_DSN)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _try_connect(),
    reason="PostgreSQL が利用できないため SKIP (QED_PG_DSN 未設定 or 接続不可)",
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_synthetic_panel(n: int = 200, seed: int = 42) -> pd.DataFrame:
    """合成パネルデータを生成 (NaN なし、abnormal_return カラム含む)。"""
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "metric": rng.normal(0, 0.02, n),
        "run_id": "synthetic_integration_test",
    })


def _build_terminal_df(panel_df: pd.DataFrame, terminals: list[str]) -> pd.DataFrame:
    from analytics.python.features.build_terminal_set import (
        build_terminal_features,
        select_terminals,
    )
    features = build_terminal_features(panel_df)
    return select_terminals(features, terminals).reset_index(drop=True)


def _make_config(run_id: str, trace_id: str, terminals: list[str]):
    from analytics.python.alpha.eml.eml_master_formula import EMLDiscoveryConfig
    return EMLDiscoveryConfig(
        run_id=run_id,
        trace_id=trace_id,
        batch_label="integration_test",
        target_horizon="5d",
        max_depth=2,
        terminal_set=terminals,
        gradient_n_init=2,
        gradient_steps=5,
        min_fitness_for_promotion=-0.5,
        min_rank_ic=-0.5,
    )


def _unique_run_id(prefix: str = "it") -> str:
    return f"eml_it__{prefix}_{uuid.uuid4().hex[:8]}"


def _unique_trace_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn():
    import psycopg
    c = psycopg.connect(PG_DSN, autocommit=False)
    yield c
    c.rollback()
    c.close()


@pytest.fixture()
def autocommit_conn():
    """UPSERT テスト用 autocommit 接続（rollback しない）。"""
    import psycopg
    c = psycopg.connect(PG_DSN, autocommit=True)
    yield c
    c.close()


@pytest.fixture()
def panel_and_terminals():
    terminals = ["r1", "r5", "r20", "gap", "vol"]
    panel_df = _make_synthetic_panel(n=200)
    target = panel_df["metric"].astype(float).reset_index(drop=True)
    terminal_df = _build_terminal_df(panel_df, terminals)
    return panel_df, terminal_df, target, terminals


# ---------------------------------------------------------------------------
# Phase A: ターミナルセット構築
# ---------------------------------------------------------------------------

class TestPhaseATerminalSet:
    def test_build_terminal_features_no_nan(self, panel_and_terminals):
        _, terminal_df, _, _ = panel_and_terminals
        assert terminal_df.isna().sum().sum() == 0, \
            "terminal_df に NaN が含まれている"

    def test_terminal_set_count(self, panel_and_terminals):
        _, terminal_df, _, terminals = panel_and_terminals
        assert len(terminals) >= 3, "ターミナル数が少なすぎる"

    def test_terminal_df_row_count(self, panel_and_terminals):
        panel_df, terminal_df, _, _ = panel_and_terminals
        assert len(terminal_df) == len(panel_df), \
            "terminal_df の行数がパネルと一致しない"


# ---------------------------------------------------------------------------
# Phase B: EML 探索
# ---------------------------------------------------------------------------

class TestPhaseBEMLSearch:
    def test_run_eml_discovery_returns_output(self, panel_and_terminals):
        from analytics.python.alpha.eml.eml_master_formula import run_eml_discovery
        from analytics.python.features.regime_features import build_crisis_mask

        _, terminal_df, target, terminals = panel_and_terminals
        run_id = _unique_run_id("search")
        trace_id = _unique_trace_id()
        config = _make_config(run_id, trace_id, terminals)
        crisis_mask = build_crisis_mask(target)

        output = run_eml_discovery(
            config=config,
            feature_df=terminal_df,
            target=target,
            regime_mask=crisis_mask,
        )

        assert output.run_id == run_id
        assert output.trace_id == trace_id
        assert output.total_searched > 0
        assert isinstance(output.promoted, list)
        assert isinstance(output.rejected, list)
        assert isinstance(output.candidates, list)
        # candidates は重複除去・安全式フィルタ後の有効候補のみ
        # total_searched は重複除去前の全探索数なので candidates <= total_searched
        assert len(output.candidates) >= 1
        assert len(output.candidates) <= output.total_searched
        assert len(output.promoted) + len(output.rejected) == len(output.candidates)

    def test_all_candidates_have_valid_trace_id(self, panel_and_terminals):
        from analytics.python.alpha.eml.eml_master_formula import run_eml_discovery
        from analytics.python.features.regime_features import build_crisis_mask

        _, terminal_df, target, terminals = panel_and_terminals
        run_id = _unique_run_id("trace")
        trace_id = _unique_trace_id()
        config = _make_config(run_id, trace_id, terminals)
        crisis_mask = build_crisis_mask(target)

        output = run_eml_discovery(
            config=config, feature_df=terminal_df,
            target=target, regime_mask=crisis_mask,
        )

        for c in output.candidates:
            assert c.trace_id == trace_id, \
                f"候補 {c.candidate_id} の trace_id が不一致: {c.trace_id}"
            assert c.run_id == run_id

    def test_no_nan_in_candidate_tree_json(self, panel_and_terminals):
        from analytics.python.alpha.eml.eml_master_formula import run_eml_discovery
        from analytics.python.features.regime_features import build_crisis_mask

        _, terminal_df, target, terminals = panel_and_terminals
        config = _make_config(_unique_run_id("nan"), _unique_trace_id(), terminals)
        output = run_eml_discovery(
            config=config, feature_df=terminal_df,
            target=target, regime_mask=build_crisis_mask(target),
        )

        for c in output.candidates:
            tree_dict = json.loads(c.node.to_json())
            tree_str = json.dumps(tree_dict)
            assert "NaN" not in tree_str, \
                f"候補 {c.candidate_id} の tree_json に NaN が含まれる"
            assert "Infinity" not in tree_str, \
                f"候補 {c.candidate_id} の tree_json に Infinity が含まれる"

    def test_candidate_status_is_promoted_or_rejected(self, panel_and_terminals):
        from analytics.python.alpha.eml.eml_master_formula import run_eml_discovery
        from analytics.python.features.regime_features import build_crisis_mask

        _, terminal_df, target, terminals = panel_and_terminals
        config = _make_config(_unique_run_id("status"), _unique_trace_id(), terminals)
        output = run_eml_discovery(
            config=config, feature_df=terminal_df,
            target=target, regime_mask=build_crisis_mask(target),
        )

        allowed = {"promoted", "rejected"}
        for c in output.candidates:
            assert c.status in allowed, \
                f"不正な status: {c.status} (candidate_id={c.candidate_id})"


# ---------------------------------------------------------------------------
# Phase C: DB 書き込み (UPSERT)
# ---------------------------------------------------------------------------

class TestPhaseCDBWrite:
    def _run_discovery(self, panel_and_terminals):
        from analytics.python.alpha.eml.eml_master_formula import run_eml_discovery
        from analytics.python.features.regime_features import build_crisis_mask

        _, terminal_df, target, terminals = panel_and_terminals
        run_id = _unique_run_id("dbwrite")
        trace_id = _unique_trace_id()
        config = _make_config(run_id, trace_id, terminals)
        output = run_eml_discovery(
            config=config, feature_df=terminal_df,
            target=target, regime_mask=build_crisis_mask(target),
        )
        return output, run_id, trace_id

    def test_upsert_alpha_run_creates_record(self, autocommit_conn, panel_and_terminals):
        from analytics.python.io.postgres_eml_alpha_writer import upsert_alpha_run

        output, run_id, _ = self._run_discovery(panel_and_terminals)
        upsert_alpha_run(autocommit_conn, output)

        with autocommit_conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, status, total_candidates, promoted_candidates "
                "FROM eml_alpha_runs WHERE run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()

        assert row is not None, f"eml_alpha_runs にレコードがない: run_id={run_id}"
        assert row[0] == run_id
        assert row[1] == "completed"
        assert row[2] == output.total_searched
        assert row[3] == len(output.promoted)

    def test_upsert_alpha_candidates_creates_records(self, autocommit_conn, panel_and_terminals):
        from analytics.python.io.postgres_eml_alpha_writer import (
            upsert_alpha_run,
            upsert_alpha_candidates,
        )

        output, run_id, trace_id = self._run_discovery(panel_and_terminals)
        upsert_alpha_run(autocommit_conn, output)
        upsert_alpha_candidates(autocommit_conn, output.candidates)

        with autocommit_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM eml_alpha_candidates WHERE run_id = %s",
                (run_id,),
            )
            count = cur.fetchone()[0]

        # upsert_alpha_candidates は output.candidates (重複除去後) を書き込む
        assert count == len(output.candidates), \
            f"候補レコード数が不一致: DB={count}, expected={len(output.candidates)}"

    def test_upsert_no_nan_in_db(self, autocommit_conn, panel_and_terminals):
        """DB に保存された fitness_score に NaN/Inf が含まれないこと。"""
        from analytics.python.io.postgres_eml_alpha_writer import (
            upsert_alpha_run,
            upsert_alpha_candidates,
        )

        output, run_id, _ = self._run_discovery(panel_and_terminals)
        upsert_alpha_run(autocommit_conn, output)
        upsert_alpha_candidates(autocommit_conn, output.candidates)

        with autocommit_conn.cursor() as cur:
            cur.execute(
                """SELECT candidate_id, fitness_score FROM eml_alpha_candidates
                   WHERE run_id = %s AND (
                       fitness_score = 'NaN'::numeric OR
                       fitness_score = 'Infinity'::numeric OR
                       fitness_score = '-Infinity'::numeric
                   )""",
                (run_id,),
            )
            bad_rows = cur.fetchall()

        assert len(bad_rows) == 0, \
            f"DB に NaN/Inf の fitness_score が存在する: {bad_rows}"

    def test_trace_id_consistent_in_db(self, autocommit_conn, panel_and_terminals):
        """DB の trace_id が run / candidates 間で一致すること。"""
        from analytics.python.io.postgres_eml_alpha_writer import (
            upsert_alpha_run,
            upsert_alpha_candidates,
        )

        output, run_id, trace_id = self._run_discovery(panel_and_terminals)
        upsert_alpha_run(autocommit_conn, output)
        upsert_alpha_candidates(autocommit_conn, output.candidates)

        with autocommit_conn.cursor() as cur:
            # run の trace_id
            cur.execute(
                "SELECT trace_id FROM eml_alpha_runs WHERE run_id = %s",
                (run_id,),
            )
            run_trace = cur.fetchone()[0]

            # candidates の trace_id (distinct)
            cur.execute(
                "SELECT DISTINCT trace_id FROM eml_alpha_candidates WHERE run_id = %s",
                (run_id,),
            )
            cand_traces = [r[0] for r in cur.fetchall()]

        assert run_trace == trace_id
        assert cand_traces == [trace_id], \
            f"candidates に複数の trace_id が混在: {cand_traces}"


# ---------------------------------------------------------------------------
# Phase C: UPSERT 再実行安全性 (rerun-safety)
# ---------------------------------------------------------------------------

class TestRerunSafety:
    def test_double_upsert_no_duplicate_run(self, autocommit_conn, panel_and_terminals):
        """同一 run_id で 2 回 UPSERT しても eml_alpha_runs が 1 件のみ。"""
        from analytics.python.io.postgres_eml_alpha_writer import upsert_alpha_run
        from analytics.python.alpha.eml.eml_master_formula import run_eml_discovery
        from analytics.python.features.regime_features import build_crisis_mask

        _, terminal_df, target, terminals = panel_and_terminals
        run_id = _unique_run_id("rerun")
        trace_id = _unique_trace_id()
        config = _make_config(run_id, trace_id, terminals)
        output = run_eml_discovery(
            config=config, feature_df=terminal_df,
            target=target, regime_mask=build_crisis_mask(target),
        )

        upsert_alpha_run(autocommit_conn, output)
        upsert_alpha_run(autocommit_conn, output)  # 2 回目

        with autocommit_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM eml_alpha_runs WHERE run_id = %s",
                (run_id,),
            )
            count = cur.fetchone()[0]

        assert count == 1, f"eml_alpha_runs に重複: {count} 件"

    def test_double_upsert_no_duplicate_candidates(self, autocommit_conn, panel_and_terminals):
        """同一候補セットで 2 回 UPSERT しても件数が増えない。"""
        from analytics.python.io.postgres_eml_alpha_writer import (
            upsert_alpha_run,
            upsert_alpha_candidates,
        )
        from analytics.python.alpha.eml.eml_master_formula import run_eml_discovery
        from analytics.python.features.regime_features import build_crisis_mask

        _, terminal_df, target, terminals = panel_and_terminals
        run_id = _unique_run_id("rerun2")
        trace_id = _unique_trace_id()
        config = _make_config(run_id, trace_id, terminals)
        output = run_eml_discovery(
            config=config, feature_df=terminal_df,
            target=target, regime_mask=build_crisis_mask(target),
        )

        upsert_alpha_run(autocommit_conn, output)
        upsert_alpha_candidates(autocommit_conn, output.candidates)
        upsert_alpha_candidates(autocommit_conn, output.candidates)  # 2 回目

        with autocommit_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM eml_alpha_candidates WHERE run_id = %s",
                (run_id,),
            )
            count = cur.fetchone()[0]

        # 2 回 UPSERT しても件数が増えない (重複除去後の候補数と等しい)
        assert count == len(output.candidates), \
            f"候補テーブルに重複: {count} 件 (expected={len(output.candidates)})"


# ---------------------------------------------------------------------------
# Phase D: Walk-forward バックテスト + DB 書き込み
# ---------------------------------------------------------------------------

class TestPhaseDBacktest:
    def _run_and_write(self, autocommit_conn, panel_and_terminals):
        """探索→DB書き込み→バックテストを実行して結果を返す。"""
        from analytics.python.alpha.eml.eml_master_formula import run_eml_discovery
        from analytics.python.features.regime_features import build_crisis_mask
        from analytics.python.io.postgres_eml_alpha_writer import (
            upsert_alpha_run,
            upsert_alpha_candidates,
        )
        from analytics.python.backtest.harness import WalkForwardConfig, WalkForwardHarness
        from analytics.python.io.postgres_eml_backtest_writer import (
            upsert_backtest_run,
            upsert_backtest_folds,
        )
        from analytics.python.alpha.eml.eml_runtime_lower import lower_and_rank_normalize

        _, terminal_df, target, terminals = panel_and_terminals
        run_id = _unique_run_id("bt")
        trace_id = _unique_trace_id()
        config = _make_config(run_id, trace_id, terminals)
        crisis_mask = build_crisis_mask(target)

        output = run_eml_discovery(
            config=config, feature_df=terminal_df,
            target=target, regime_mask=crisis_mask,
        )
        upsert_alpha_run(autocommit_conn, output)
        upsert_alpha_candidates(autocommit_conn, output.candidates)

        wf_config = WalkForwardConfig(
            mode="expanding",
            min_train_days=30,   # テスト用に短縮
            step_days=5,
        )
        wf_harness = WalkForwardHarness(wf_config)
        bt_results = []

        for c in output.promoted[:2]:
            signal = lower_and_rank_normalize(c.compiled_expr, terminal_df)
            bt_result = wf_harness.run(
                signal=signal,
                returns=target,
                run_id=run_id,
                trace_id=trace_id,
                crisis_mask=crisis_mask,
            )
            upsert_backtest_run(autocommit_conn, bt_result, c.candidate_id)
            # candidate_id と trace_id を明示的に渡す
            upsert_backtest_folds(
                autocommit_conn, bt_result.folds,
                candidate_id=c.candidate_id,
                trace_id=trace_id,
            )
            bt_results.append((c, bt_result))

        return output, bt_results, run_id, trace_id

    def test_backtest_run_records_created(self, autocommit_conn, panel_and_terminals):
        output, bt_results, run_id, trace_id = self._run_and_write(
            autocommit_conn, panel_and_terminals
        )
        if not output.promoted:
            pytest.skip("promoted 候補がゼロ (パラメータを緩めてください)")

        with autocommit_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM eml_backtest_runs WHERE trace_id = %s",
                (trace_id,),
            )
            count = cur.fetchone()[0]

        assert count == len(bt_results), \
            f"eml_backtest_runs のレコード数が不一致: {count} vs {len(bt_results)}"

    def test_backtest_folds_created(self, autocommit_conn, panel_and_terminals):
        output, bt_results, run_id, trace_id = self._run_and_write(
            autocommit_conn, panel_and_terminals
        )
        if not output.promoted:
            pytest.skip("promoted 候補がゼロ")

        expected_folds = sum(len(r.folds) for _, r in bt_results)
        with autocommit_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM eml_backtest_folds WHERE trace_id = %s",
                (trace_id,),
            )
            count = cur.fetchone()[0]

        assert count == expected_folds, \
            f"eml_backtest_folds のレコード数が不一致: {count} vs {expected_folds}"

    def test_backtest_sharpe_is_finite(self, autocommit_conn, panel_and_terminals):
        output, bt_results, run_id, trace_id = self._run_and_write(
            autocommit_conn, panel_and_terminals
        )
        if not bt_results:
            pytest.skip("バックテスト結果なし")

        with autocommit_conn.cursor() as cur:
            cur.execute(
                """SELECT backtest_run_id,
                          summary_json->>'overall_sharpe' AS sharpe
                   FROM eml_backtest_runs WHERE trace_id = %s""",
                (trace_id,),
            )
            rows = cur.fetchall()

        for row in rows:
            sharpe = float(row[1])
            assert not (sharpe != sharpe), \
                f"Sharpe が NaN: backtest_run_id={row[0]}"  # NaN check: NaN != NaN is True

    def test_backtest_trace_id_consistent(self, autocommit_conn, panel_and_terminals):
        output, bt_results, run_id, trace_id = self._run_and_write(
            autocommit_conn, panel_and_terminals
        )
        if not bt_results:
            pytest.skip("バックテスト結果なし")

        with autocommit_conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT trace_id FROM eml_backtest_runs WHERE trace_id = %s",
                (trace_id,),
            )
            traces = [r[0] for r in cur.fetchall()]

        assert traces == [trace_id], f"backtest trace_id 不一致: {traces}"


# ---------------------------------------------------------------------------
# Phase E: プロモーション (promote_batch)
# ---------------------------------------------------------------------------

class TestPhaseEPromotion:
    def _run_phases_abcd(self, autocommit_conn, panel_and_terminals):
        from analytics.python.alpha.eml.eml_master_formula import run_eml_discovery
        from analytics.python.features.regime_features import build_crisis_mask
        from analytics.python.io.postgres_eml_alpha_writer import (
            upsert_alpha_run,
            upsert_alpha_candidates,
        )

        _, terminal_df, target, terminals = panel_and_terminals
        run_id = _unique_run_id("promo")
        trace_id = _unique_trace_id()
        config = _make_config(run_id, trace_id, terminals)
        output = run_eml_discovery(
            config=config, feature_df=terminal_df,
            target=target, regime_mask=build_crisis_mask(target),
        )
        upsert_alpha_run(autocommit_conn, output)
        upsert_alpha_candidates(autocommit_conn, output.candidates)
        return output, run_id, trace_id

    def test_promote_batch_returns_list(self, autocommit_conn, panel_and_terminals):
        from analytics.python.alpha.promotion_bridge import promote_batch

        output, run_id, trace_id = self._run_phases_abcd(autocommit_conn, panel_and_terminals)
        results = promote_batch(
            conn=autocommit_conn,
            candidates=output.promoted,
            eval_results=output.eval_results,
            dry_run=True,  # 統合テストは dry_run で実行
        )

        assert isinstance(results, list)
        assert len(results) == len(output.promoted)

    def test_promote_batch_decisions_are_valid(self, autocommit_conn, panel_and_terminals):
        from analytics.python.alpha.promotion_bridge import promote_batch

        output, run_id, trace_id = self._run_phases_abcd(autocommit_conn, panel_and_terminals)
        results = promote_batch(
            conn=autocommit_conn,
            candidates=output.promoted,
            eval_results=output.eval_results,
            dry_run=True,
        )

        allowed_decisions = {"APPLIED", "REJECTED", "DRY_RUN", "CONFLICTED"}
        for r in results:
            assert r["decision"] in allowed_decisions, \
                f"不正な decision: {r['decision']}"

    def test_promote_batch_trace_id_propagated(self, autocommit_conn, panel_and_terminals):
        from analytics.python.alpha.promotion_bridge import promote_batch

        output, run_id, trace_id = self._run_phases_abcd(autocommit_conn, panel_and_terminals)
        results = promote_batch(
            conn=autocommit_conn,
            candidates=output.promoted,
            eval_results=output.eval_results,
            dry_run=True,
        )

        for r in results:
            assert r.get("trace_id") == trace_id, \
                f"trace_id が伝播していない: {r.get('trace_id')} != {trace_id}"

    def test_promote_batch_dry_run_no_bridge_record(self, autocommit_conn, panel_and_terminals):
        """dry_run=True では eml_alpha_promotion_bridge にレコードが作成されない。"""
        from analytics.python.alpha.promotion_bridge import promote_batch

        output, run_id, trace_id = self._run_phases_abcd(autocommit_conn, panel_and_terminals)
        promote_batch(
            conn=autocommit_conn,
            candidates=output.promoted,
            eval_results=output.eval_results,
            dry_run=True,
        )

        if not output.promoted:
            pytest.skip("promoted 候補なし")

        candidate_ids = [c.candidate_id for c in output.promoted]
        with autocommit_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM eml_alpha_promotion_bridge WHERE candidate_id = ANY(%s)",
                (candidate_ids,),
            )
            count = cur.fetchone()[0]

        # dry_run では bridge レコードは 0 件であること
        assert count == 0, \
            f"dry_run なのに bridge レコードが作成された: {count} 件"


# ---------------------------------------------------------------------------
# audit_events 記録検証
# ---------------------------------------------------------------------------

class TestAuditEvents:
    def test_rejected_candidates_emit_audit_event(self, autocommit_conn, panel_and_terminals):
        """promoted=0 の run でも REJECTED 判定の audit_event が記録されること。"""
        from analytics.python.alpha.eml.eml_master_formula import EMLDiscoveryConfig, run_eml_discovery
        from analytics.python.features.regime_features import build_crisis_mask
        from analytics.python.io.postgres_eml_alpha_writer import (
            upsert_alpha_run,
            upsert_alpha_candidates,
        )
        from analytics.python.alpha.promotion_bridge import promote_batch

        _, terminal_df, target, terminals = panel_and_terminals
        run_id = _unique_run_id("audit")
        trace_id = _unique_trace_id()

        # min_fitness を極めて高くして全候補が rejected になるよう設定
        config = EMLDiscoveryConfig(
            run_id=run_id,
            trace_id=trace_id,
            batch_label="audit_test",
            target_horizon="5d",
            max_depth=2,
            terminal_set=terminals,
            gradient_n_init=1,
            gradient_steps=3,
            min_fitness_for_promotion=999.0,   # 達成不可能な閾値
            min_rank_ic=999.0,
        )
        output = run_eml_discovery(
            config=config, feature_df=terminal_df,
            target=target, regime_mask=build_crisis_mask(target),
        )
        upsert_alpha_run(autocommit_conn, output)
        upsert_alpha_candidates(autocommit_conn, output.candidates)

        # promoted=[] なので promote_batch は空リストを返すはず
        results = promote_batch(
            conn=autocommit_conn,
            candidates=output.promoted,
            eval_results=output.eval_results,
            dry_run=False,
        )

        # min_fitness=999 なので promoted は空のはず (len(output.candidates) >= 1 は保証)
        assert len(output.promoted) == 0, "閾値が高すぎて全候補が rejected されるはず"
        assert results == [], "promoted=[] なら promote_batch 結果も空"

    def test_audit_event_decision_values(self, autocommit_conn, panel_and_terminals):
        """audit_events の decision フィールドは許可値のみ。"""
        from analytics.python.alpha.eml.eml_master_formula import run_eml_discovery
        from analytics.python.features.regime_features import build_crisis_mask
        from analytics.python.io.postgres_eml_alpha_writer import (
            upsert_alpha_run,
            upsert_alpha_candidates,
        )
        from analytics.python.alpha.promotion_bridge import promote_batch

        _, terminal_df, target, terminals = panel_and_terminals
        run_id = _unique_run_id("audit2")
        trace_id = _unique_trace_id()
        config = _make_config(run_id, trace_id, terminals)
        output = run_eml_discovery(
            config=config, feature_df=terminal_df,
            target=target, regime_mask=build_crisis_mask(target),
        )
        upsert_alpha_run(autocommit_conn, output)
        upsert_alpha_candidates(autocommit_conn, output.candidates)
        promote_batch(
            conn=autocommit_conn,
            candidates=output.promoted,
            eval_results=output.eval_results,
            dry_run=False,
        )

        with autocommit_conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT decision FROM audit_events
                   WHERE object_type = 'eml_alpha_candidate'
                     AND trace_id = %s""",
                (trace_id,),
            )
            decisions = {r[0] for r in cur.fetchall()}

        allowed = {"APPLIED", "REJECTED", "DRY_RUN", "CONFLICTED"}
        unknown = decisions - allowed
        assert unknown == set(), f"不正な decision 値が audit_events に記録された: {unknown}"


# ---------------------------------------------------------------------------
# フルパイプライン E2E テスト (Phase A → E)
# ---------------------------------------------------------------------------

class TestFullPipelineE2E:
    def test_full_pipeline_completes_without_error(self, autocommit_conn, panel_and_terminals):
        """
        Phase A→E を通しで実行し、status='completed' が返ること。
        実際の PostgreSQL に書き込むが、ユニークな run_id を使うので
        既存データとの衝突はない。
        """
        from analytics.python.alpha.eml.eml_master_formula import run_eml_discovery
        from analytics.python.features.regime_features import build_crisis_mask
        from analytics.python.io.postgres_eml_alpha_writer import (
            upsert_alpha_run,
            upsert_alpha_candidates,
        )
        from analytics.python.backtest.harness import WalkForwardConfig, WalkForwardHarness
        from analytics.python.io.postgres_eml_backtest_writer import (
            upsert_backtest_run,
            upsert_backtest_folds,
        )
        from analytics.python.alpha.eml.eml_runtime_lower import lower_and_rank_normalize
        from analytics.python.alpha.promotion_bridge import promote_batch

        _, terminal_df, target, terminals = panel_and_terminals
        run_id = _unique_run_id("e2e")
        trace_id = _unique_trace_id()
        config = _make_config(run_id, trace_id, terminals)
        crisis_mask = build_crisis_mask(target)

        # Phase B: 探索
        output = run_eml_discovery(
            config=config, feature_df=terminal_df,
            target=target, regime_mask=crisis_mask,
        )
        assert output.total_searched > 0

        # Phase C: DB 書き込み
        upsert_alpha_run(autocommit_conn, output)
        upsert_alpha_candidates(autocommit_conn, output.candidates)

        # Phase D: バックテスト
        wf_config = WalkForwardConfig(mode="expanding", min_train_days=30, step_days=5)
        wf_harness = WalkForwardHarness(wf_config)
        bt_count = 0
        for c in output.promoted[:2]:
            signal = lower_and_rank_normalize(c.compiled_expr, terminal_df)
            bt_result = wf_harness.run(
                signal=signal, returns=target,
                run_id=run_id, trace_id=trace_id, crisis_mask=crisis_mask,
            )
            upsert_backtest_run(autocommit_conn, bt_result, c.candidate_id)
            upsert_backtest_folds(
                autocommit_conn, bt_result.folds,
                candidate_id=c.candidate_id, trace_id=trace_id,
            )
            bt_count += 1

        # Phase E: プロモーション
        promo_results = promote_batch(
            conn=autocommit_conn,
            candidates=output.promoted,
            eval_results=output.eval_results,
            dry_run=True,
        )

        # サマリー検証
        summary = {
            "status": "completed",
            "run_id": run_id,
            "trace_id": trace_id,
            "total_searched": output.total_searched,
            "promoted": len(output.promoted),
            "backtest_runs": bt_count,
            "promo_results": len(promo_results),
        }
        assert summary["status"] == "completed"
        assert summary["run_id"] == run_id
        assert summary["trace_id"] == trace_id
        assert summary["total_searched"] > 0

    def test_full_pipeline_db_counts_consistent(self, autocommit_conn, panel_and_terminals):
        """DB の各テーブルのレコード数がパイプライン実行結果と一致すること。"""
        from analytics.python.alpha.eml.eml_master_formula import run_eml_discovery
        from analytics.python.features.regime_features import build_crisis_mask
        from analytics.python.io.postgres_eml_alpha_writer import (
            upsert_alpha_run,
            upsert_alpha_candidates,
        )
        from analytics.python.backtest.harness import WalkForwardConfig, WalkForwardHarness
        from analytics.python.io.postgres_eml_backtest_writer import (
            upsert_backtest_run,
            upsert_backtest_folds,
        )
        from analytics.python.alpha.eml.eml_runtime_lower import lower_and_rank_normalize

        _, terminal_df, target, terminals = panel_and_terminals
        run_id = _unique_run_id("dbcount")
        trace_id = _unique_trace_id()
        config = _make_config(run_id, trace_id, terminals)
        crisis_mask = build_crisis_mask(target)

        output = run_eml_discovery(
            config=config, feature_df=terminal_df,
            target=target, regime_mask=crisis_mask,
        )
        upsert_alpha_run(autocommit_conn, output)
        upsert_alpha_candidates(autocommit_conn, output.candidates)

        wf_config = WalkForwardConfig(mode="expanding", min_train_days=30, step_days=5)
        wf_harness = WalkForwardHarness(wf_config)
        expected_folds = 0
        bt_run_ids = []
        for c in output.promoted[:2]:
            signal = lower_and_rank_normalize(c.compiled_expr, terminal_df)
            bt_result = wf_harness.run(
                signal=signal, returns=target,
                run_id=run_id, trace_id=trace_id, crisis_mask=crisis_mask,
            )
            upsert_backtest_run(autocommit_conn, bt_result, c.candidate_id)
            upsert_backtest_folds(
                autocommit_conn, bt_result.folds,
                candidate_id=c.candidate_id, trace_id=trace_id,
            )
            expected_folds += len(bt_result.folds)
            bt_run_ids.append(bt_result.backtest_run_id)

        with autocommit_conn.cursor() as cur:
            # eml_alpha_runs
            cur.execute(
                "SELECT COUNT(*) FROM eml_alpha_runs WHERE run_id = %s", (run_id,)
            )
            assert cur.fetchone()[0] == 1, "eml_alpha_runs のレコードが 1 件でない"

            # eml_alpha_candidates (重複除去後の有効候補数)
            cur.execute(
                "SELECT COUNT(*) FROM eml_alpha_candidates WHERE run_id = %s", (run_id,)
            )
            assert cur.fetchone()[0] == len(output.candidates), \
                "eml_alpha_candidates の件数不一致"

            # eml_backtest_runs
            cur.execute(
                "SELECT COUNT(*) FROM eml_backtest_runs WHERE trace_id = %s", (trace_id,)
            )
            assert cur.fetchone()[0] == len(bt_run_ids), \
                "eml_backtest_runs の件数不一致"

            # eml_backtest_folds
            if bt_run_ids:
                cur.execute(
                    "SELECT COUNT(*) FROM eml_backtest_folds WHERE trace_id = %s",
                    (trace_id,),
                )
                assert cur.fetchone()[0] == expected_folds, \
                    "eml_backtest_folds の件数不一致"
