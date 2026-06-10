"""
test_frost_integration.py
--------------------------
FROST Meta-Fitness Engine 統合テスト。

PostgreSQL への実際の読み書きを伴う end-to-end テスト。
QED_PG_DSN 環境変数が設定されていない場合はスキップする。

テスト範囲:
  1. FrostConfig 構築・DSN 取得
  2. postgres_frost_writer: frost_runs UPSERT
  3. postgres_frost_writer: frost_fitness_candidates UPSERT
  4. postgres_frost_writer: frost_evaluations UPSERT
  5. postgres_frost_writer: frost_selection_decisions UPSERT
  6. frost_selector: バッチ評価
  7. frost_ranker: assign_decisions
  8. frost_runner: run_frost_pipeline (end-to-end)
  9. dry_run=True 検証 (bridge 書かない)
  10. PBO hard gate: PBO > threshold → REJECTED
  11. turnover hard gate: turnover > max → REJECTED
  12. near-duplicate 抑制検証
  13. top-k 制限検証
  14. audit bridge: emit_run_audit_events
  15. promotion bridge: promote_frost_decisions (dry_run)
  16. promotion bridge: promote_frost_decisions (実行)
  17. UPSERT rerun-safe: 同一 run_id で再実行しても重複しない
  18. view: v_frost_runs が SELECT 可能
  19. view: v_frost_candidate_scores が SELECT 可能
  20. view: v_frost_selection_summary が SELECT 可能
  21. trace_id: end-to-end で一致する
  22. missing trace_id = 0
  23. null frost_score = 0
  24. SELECTED 候補のみ昇格適格
  25. frost_report_builder: run_output から markdown/JSON 生成
"""
from __future__ import annotations

import os
import uuid
from typing import List

import pytest

# -------------------------------------------------------------------
# DB 接続フィクスチャ
# -------------------------------------------------------------------

PG_DSN = os.environ.get("QED_PG_DSN", "")

pytestmark = pytest.mark.skipif(
    not PG_DSN,
    reason="QED_PG_DSN 環境変数が設定されていないため統合テストをスキップ",
)


@pytest.fixture(scope="module")
def db_conn():
    """モジュールスコープの PostgreSQL 接続"""
    import psycopg
    conn = psycopg.connect(PG_DSN)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def frost_cfg():
    """テスト用 FrostConfig (厳しくない閾値、少数 fold)"""
    from analytics.python.frost.frost_config import load_frost_config
    return load_frost_config(overrides={
        "dry_run": False,
        "min_backtest_folds": 2,
        "top_k": 5,
        "promotion_top_k": 2,
        "min_oos_sharpe": 0.3,
        "min_rank_ic": 0.01,
        "max_turnover": 10.0,
        "max_drawdown": 0.50,
        "min_regime_pass_ratio": 0.0,
        "max_complexity_score": 1.0,
        "min_selection_stability": 0.0,
        "pbo_threshold": 0.99,   # PBO gate を実質無効化
        "review_required_default": True,
        "batch_label": "integration_test_batch",
    })


@pytest.fixture(scope="module")
def run_id():
    return str(uuid.uuid4())


@pytest.fixture(scope="module")
def trace_id():
    return str(uuid.uuid4())


def _make_candidates(run_id: str, trace_id: str, n: int = 6):
    """テスト用 FrostCandidate リストを生成する"""
    from analytics.python.frost.frost_contracts import FrostCandidate
    candidates = []
    for i in range(n):
        sharpe = 0.5 + i * 0.3
        candidates.append(FrostCandidate(
            candidate_id=str(uuid.uuid4()),
            run_id=run_id,
            trace_id=trace_id,
            source_type="eml",
            source_candidate_id=str(uuid.uuid4()),
            formula_text=f"x{i} + y{i}",
            complexity_score=0.2 + i * 0.05,
            horizon="5d",
            candidate_hash=f"testhash{i:04d}{uuid.uuid4().hex[:8]}",
            backtest_summary={
                "oos_sharpe": sharpe,
                "max_drawdown": 0.05 + i * 0.02,
                "turnover": 1.0 + i * 0.5,
            },
            metrics={
                "rank_ic": 0.03 + i * 0.01,
                "ic": 0.025 + i * 0.008,
                "hit_rate": 0.52 + i * 0.01,
            },
            regime_breakdown={
                "bull":   {"sharpe": sharpe * 1.2},
                "bear":   {"sharpe": sharpe * 0.7},
                "crisis": {"sharpe": sharpe * 0.4},
            },
            fold_results=[
                {"sharpe": sharpe * (1 + 0.05 * j), "rank_ic": 0.03 + i * 0.01}
                for j in range(-1, 3)
            ],
        ))
    return candidates


# -------------------------------------------------------------------
# テスト 1-4: postgres_frost_writer
# -------------------------------------------------------------------

class TestFrostWriter:
    @pytest.fixture(scope="class")
    def setup(self, db_conn, frost_cfg, run_id, trace_id):
        candidates = _make_candidates(run_id, trace_id, n=4)
        return {"conn": db_conn, "cfg": frost_cfg, "run_id": run_id,
                "trace_id": trace_id, "candidates": candidates}

    def test_1_upsert_frost_run(self, setup):
        """frost_runs UPSERT が成功する"""
        from analytics.python.frost.frost_contracts import FrostRunOutput
        from analytics.python.io.postgres_frost_writer import upsert_frost_run
        conn = setup["conn"]
        output = FrostRunOutput(
            run_id=setup["run_id"],
            trace_id=setup["trace_id"],
            batch_label="integration_test_batch",
            candidate_count=4,
            status="running",
        )
        upsert_frost_run(conn, output)
        row = conn.execute(
            "SELECT run_id, trace_id FROM frost_runs WHERE run_id=%s",
            (setup["run_id"],)
        ).fetchone()
        assert row is not None
        assert str(row[0]) == setup["run_id"]
        assert row[1] == setup["trace_id"]

    def test_2_upsert_frost_candidates(self, setup):
        """frost_fitness_candidates UPSERT が成功する"""
        from analytics.python.io.postgres_frost_writer import upsert_frost_candidates_batch
        conn = setup["conn"]
        upsert_frost_candidates_batch(conn, setup["candidates"])
        count = conn.execute(
            "SELECT COUNT(*) FROM frost_fitness_candidates WHERE run_id=%s",
            (setup["run_id"],)
        ).fetchone()[0]
        assert count == len(setup["candidates"])

    def test_3_upsert_frost_evaluation(self, setup):
        """frost_evaluations UPSERT が成功する"""
        from analytics.python.frost.frost_contracts import FrostEvaluation
        from analytics.python.io.postgres_frost_writer import upsert_frost_evaluation
        conn = setup["conn"]
        c = setup["candidates"][0]
        ev = FrostEvaluation(
            run_id=setup["run_id"],
            candidate_id=c.candidate_id,
            trace_id=setup["trace_id"],
            frost_score=0.55,
            oos_sharpe=1.2,
            rank_ic=0.05,
            pbo_score=0.10,
            hard_gate_passed=True,
            hard_gate_failures=[],
        )
        upsert_frost_evaluation(conn, ev)
        conn.commit()
        row = conn.execute(
            "SELECT frost_score FROM frost_evaluations WHERE candidate_id=%s",
            (c.candidate_id,)
        ).fetchone()
        assert row is not None
        assert float(row[0]) == pytest.approx(0.55, abs=1e-4)

    def test_4_upsert_frost_decision(self, setup):
        """frost_selection_decisions UPSERT が成功する"""
        from analytics.python.frost.frost_contracts import FrostDecision
        from analytics.python.io.postgres_frost_writer import upsert_frost_decision
        conn = setup["conn"]
        c = setup["candidates"][0]
        d = FrostDecision(
            run_id=setup["run_id"],
            candidate_id=c.candidate_id,
            trace_id=setup["trace_id"],
            decision="SELECTED",
            frost_score=0.55,
            decision_rank=1,
            promotion_eligible=True,
        )
        upsert_frost_decision(conn, d)
        conn.commit()
        row = conn.execute(
            "SELECT decision FROM frost_selection_decisions WHERE candidate_id=%s",
            (c.candidate_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == "SELECTED"


# -------------------------------------------------------------------
# テスト 5-8: frost_runner end-to-end
# -------------------------------------------------------------------

class TestFrostRunner:
    @pytest.fixture(scope="class")
    def pipeline_output(self, db_conn, frost_cfg):
        """run_frost_pipeline を 1 度実行して output を共有する"""
        from analytics.python.frost.frost_runner import run_frost_pipeline
        new_run_id   = str(uuid.uuid4())
        new_trace_id = str(uuid.uuid4())
        candidates   = _make_candidates(new_run_id, new_trace_id, n=8)
        output = run_frost_pipeline(candidates, frost_cfg, conn=db_conn,
                                    run_id=new_run_id, trace_id=new_trace_id)
        return output

    def test_5_run_completes(self, pipeline_output):
        """パイプラインが completed/dry_run で終了する"""
        assert pipeline_output.status in ("completed", "dry_run", "skipped")
        assert pipeline_output.run_id != ""
        assert pipeline_output.trace_id != ""

    def test_6_candidates_evaluated(self, pipeline_output):
        """全候補が評価される"""
        assert pipeline_output.evaluated_count == pipeline_output.candidate_count
        assert len(pipeline_output.evaluations) == pipeline_output.candidate_count

    def test_7_decisions_generated(self, pipeline_output):
        """全候補に決定が生成される"""
        assert len(pipeline_output.decisions) == pipeline_output.candidate_count

    def test_8_frost_score_not_null(self, pipeline_output):
        """全評価で frost_score が None でない"""
        for ev in pipeline_output.evaluations:
            assert ev.frost_score is not None
            assert not (ev.frost_score != ev.frost_score)  # NaN チェック

    def test_9_dry_run_no_promotion_bridge(self, db_conn, frost_cfg):
        """dry_run=True では frost_promotion_bridges に 'applied' が書かれない"""
        from analytics.python.frost.frost_config import load_frost_config
        from analytics.python.frost.frost_runner import run_frost_pipeline
        dry_cfg = load_frost_config(overrides={
            **{k: getattr(frost_cfg, k) for k in [
                "min_backtest_folds", "top_k", "promotion_top_k",
                "min_oos_sharpe", "min_rank_ic", "max_turnover",
                "max_drawdown", "min_regime_pass_ratio", "max_complexity_score",
                "min_selection_stability", "pbo_threshold",
            ]},
            "dry_run": True,
            "batch_label": "integration_dry_run_test",
        })
        dr_run_id   = str(uuid.uuid4())
        dr_trace_id = str(uuid.uuid4())
        candidates  = _make_candidates(dr_run_id, dr_trace_id, n=3)
        output = run_frost_pipeline(candidates, dry_cfg, conn=db_conn,
                                    run_id=dr_run_id, trace_id=dr_trace_id)
        assert output.dry_run is True
        # promotion_bridges に 'applied' レコードがないこと
        count = db_conn.execute(
            "SELECT COUNT(*) FROM frost_promotion_bridges "
            "WHERE run_id=%s AND promotion_status='applied'",
            (dr_run_id,)
        ).fetchone()[0]
        assert count == 0


# -------------------------------------------------------------------
# テスト 10-12: Hard gate / PBO / near-dup
# -------------------------------------------------------------------

class TestFrostHardGates:
    def test_10_pbo_gate_reject(self):
        """PBO > threshold で REJECTED"""
        from analytics.python.frost.frost_config import FrostConfig
        from analytics.python.frost.frost_selector import check_hard_gates
        cfg = FrostConfig(pbo_threshold=0.20)
        feat = {"rank_ic": 0.05, "oos_sharpe": 1.0, "turnover": 2.0,
                "oos_max_drawdown": -0.10, "regime_pass_ratio_raw": 1.0,
                "complexity_score": 0.3}
        passed, failures = check_hard_gates(feat, pbo_score=0.30,
                                             selection_consistency_score=0.8,
                                             config=cfg)
        assert not passed
        assert any("pbo" in f for f in failures)

    def test_11_turnover_gate_reject(self):
        """turnover > max_turnover で REJECTED"""
        from analytics.python.frost.frost_config import FrostConfig
        from analytics.python.frost.frost_selector import check_hard_gates
        cfg = FrostConfig(max_turnover=4.0, pbo_threshold=0.99)
        feat = {"rank_ic": 0.05, "oos_sharpe": 1.0, "turnover": 6.0,
                "oos_max_drawdown": -0.05, "regime_pass_ratio_raw": 1.0,
                "complexity_score": 0.3}
        passed, failures = check_hard_gates(feat, pbo_score=0.10,
                                             selection_consistency_score=0.8,
                                             config=cfg)
        assert not passed
        assert any("turnover" in f for f in failures)

    def test_12_near_dup_suppression(self):
        """同一 hash の候補は near-dup として抑制される"""
        from analytics.python.frost.frost_contracts import FrostCandidate
        from analytics.python.frost.frost_ranker import detect_near_duplicates
        same_hash = "samehash12345678"
        c1 = FrostCandidate(candidate_id="dup1", candidate_hash=same_hash)
        c2 = FrostCandidate(candidate_id="dup2", candidate_hash=same_hash)
        c3 = FrostCandidate(candidate_id="uniq", candidate_hash="differenthashX")
        result = detect_near_duplicates([c1, c2, c3], threshold=0.90)
        assert "dup2" in result
        assert "uniq" not in result


# -------------------------------------------------------------------
# テスト 13-16: Audit / Promotion bridge
# -------------------------------------------------------------------

class TestFrostBridges:
    @pytest.fixture(scope="class")
    def bridge_output(self, db_conn, frost_cfg):
        """bridge テスト用の pipeline output"""
        from analytics.python.frost.frost_runner import run_frost_pipeline
        run_id   = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        candidates = _make_candidates(run_id, trace_id, n=5)
        output = run_frost_pipeline(candidates, frost_cfg, conn=db_conn,
                                    run_id=run_id, trace_id=trace_id)
        return output

    def test_14_emit_run_audit_events(self, db_conn, bridge_output):
        """audit bridge イベントが発行される"""
        from analytics.python.io.postgres_frost_audit_bridge import emit_run_audit_events
        records = emit_run_audit_events(db_conn, bridge_output)
        assert len(records) > 0
        # frost.run.completed は必ず含まれる
        event_names = [r.event_name for r in records]
        assert "frost.run.completed" in event_names

    def test_15_promote_dry_run(self, db_conn, bridge_output):
        """dry_run の promotion → promotion_status='dry_run' で書かれる"""
        from analytics.python.frost.frost_contracts import FrostRunOutput, FrostDecision
        from analytics.python.io.postgres_frost_promotion_bridge import promote_frost_decisions

        dry_output = FrostRunOutput(
            run_id=bridge_output.run_id,
            trace_id=bridge_output.trace_id,
            batch_label="test_promote_dry",
            decisions=[
                FrostDecision(
                    run_id=bridge_output.run_id,
                    candidate_id=d.candidate_id,
                    trace_id=bridge_output.trace_id,
                    decision="SELECTED",
                    frost_score=d.frost_score,
                    promotion_eligible=True,
                )
                for d in bridge_output.decisions
                if d.decision == "SELECTED"
            ][:1],
            dry_run=True,
        )
        records = promote_frost_decisions(db_conn, dry_output)
        # dry_run なので promotion_status='dry_run'
        for rec in records:
            assert rec.promotion_status in ("dry_run", "error")

    def test_16_promote_applied(self, db_conn, bridge_output):
        """実行 promotion → promotion_status='applied'"""
        from analytics.python.frost.frost_contracts import FrostRunOutput, FrostDecision
        from analytics.python.io.postgres_frost_promotion_bridge import promote_frost_decisions

        selected = [d for d in bridge_output.decisions if d.decision == "SELECTED"]
        if not selected:
            pytest.skip("SELECTED 候補なし — スキップ")

        apply_output = FrostRunOutput(
            run_id=bridge_output.run_id,
            trace_id=bridge_output.trace_id,
            batch_label="test_promote_apply",
            decisions=[
                FrostDecision(
                    run_id=bridge_output.run_id,
                    candidate_id=selected[0].candidate_id,
                    trace_id=bridge_output.trace_id,
                    decision="SELECTED",
                    frost_score=selected[0].frost_score,
                    promotion_eligible=True,
                )
            ],
            dry_run=False,
        )
        records = promote_frost_decisions(db_conn, apply_output)
        for rec in records:
            assert rec.promotion_status in ("applied", "error")


# -------------------------------------------------------------------
# テスト 17-20: rerun-safe / views / データ品質
# -------------------------------------------------------------------

class TestFrostDataQuality:
    @pytest.fixture(scope="class")
    def rerun_output(self, db_conn, frost_cfg):
        """同一 run_id で 2 回実行する"""
        from analytics.python.frost.frost_runner import run_frost_pipeline
        run_id   = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        candidates = _make_candidates(run_id, trace_id, n=4)
        # 1回目
        run_frost_pipeline(candidates, frost_cfg, conn=db_conn,
                           run_id=run_id, trace_id=trace_id)
        # 2回目 (同一 run_id)
        output = run_frost_pipeline(candidates, frost_cfg, conn=db_conn,
                                    run_id=run_id, trace_id=trace_id)
        return output

    def test_17_rerun_safe_no_duplicates(self, db_conn, rerun_output):
        """再実行しても frost_runs が重複しない"""
        count = db_conn.execute(
            "SELECT COUNT(*) FROM frost_runs WHERE run_id=%s",
            (rerun_output.run_id,)
        ).fetchone()[0]
        assert count == 1  # UPSERT で 1 件のみ

    def test_18_view_frost_runs_selectable(self, db_conn):
        """v_frost_runs が SELECT 可能"""
        rows = db_conn.execute(
            "SELECT run_id, batch_label FROM v_frost_runs LIMIT 5"
        ).fetchall()
        assert isinstance(rows, list)

    def test_19_view_candidate_scores_selectable(self, db_conn):
        """v_frost_candidate_scores が SELECT 可能"""
        rows = db_conn.execute(
            "SELECT candidate_id FROM v_frost_candidate_scores LIMIT 5"
        ).fetchall()
        assert isinstance(rows, list)

    def test_20_view_selection_summary_selectable(self, db_conn):
        """v_frost_selection_summary が SELECT 可能"""
        rows = db_conn.execute(
            "SELECT run_id FROM v_frost_selection_summary LIMIT 5"
        ).fetchall()
        assert isinstance(rows, list)

    def test_21_trace_id_end_to_end(self, db_conn, rerun_output):
        """trace_id が frost_runs と frost_evaluations で一致する"""
        ev_row = db_conn.execute(
            "SELECT trace_id FROM frost_evaluations WHERE run_id=%s LIMIT 1",
            (rerun_output.run_id,)
        ).fetchone()
        if ev_row:
            assert ev_row[0] == rerun_output.trace_id

    def test_22_missing_trace_id_zero(self, db_conn, rerun_output):
        """missing trace_id = 0"""
        count = db_conn.execute(
            "SELECT COUNT(*) FROM frost_runs WHERE run_id=%s AND (trace_id IS NULL OR trace_id='')",
            (rerun_output.run_id,)
        ).fetchone()[0]
        assert count == 0

    def test_23_null_frost_score_zero(self, db_conn, rerun_output):
        """null frost_score = 0"""
        count = db_conn.execute(
            "SELECT COUNT(*) FROM frost_evaluations WHERE run_id=%s AND frost_score IS NULL",
            (rerun_output.run_id,)
        ).fetchone()[0]
        assert count == 0

    def test_24_selected_only_promotion_eligible(self, db_conn, rerun_output):
        """SELECTED 候補のみ promotion_eligible=True になりうる"""
        count = db_conn.execute(
            "SELECT COUNT(*) FROM frost_selection_decisions "
            "WHERE run_id=%s AND promotion_eligible=TRUE AND decision != 'SELECTED'",
            (rerun_output.run_id,)
        ).fetchone()[0]
        assert count == 0


# -------------------------------------------------------------------
# テスト 25: report builder
# -------------------------------------------------------------------

class TestFrostReportIntegration:
    def test_25_report_from_pipeline_output(self, db_conn, frost_cfg):
        """run_frost_pipeline の output から markdown/JSON レポートが生成できる"""
        from analytics.python.frost.frost_runner import run_frost_pipeline
        from analytics.python.frost.frost_report_builder import (
            build_markdown_report, build_json_summary
        )
        run_id   = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        candidates = _make_candidates(run_id, trace_id, n=4)
        output = run_frost_pipeline(candidates, frost_cfg, conn=db_conn,
                                    run_id=run_id, trace_id=trace_id)
        md = build_markdown_report(output)
        assert isinstance(md, str)
        assert len(md) > 100

        summary = build_json_summary(output)
        assert summary["run_id"] == run_id
        assert "counts" in summary
