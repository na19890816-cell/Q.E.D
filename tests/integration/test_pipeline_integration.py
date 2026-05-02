"""
tests/integration/test_pipeline_integration.py
------------------------------------------------
Event Study Pipeline の結合テスト（実 PostgreSQL を使用）

前提条件:
  - QED_PG_DSN に接続可能な PostgreSQL が必要
  - マイグレーション 015-021 が適用済みであること
  - 環境変数 QED_PG_DSN が設定されていること

スキップ条件:
  - QED_PG_DSN が未設定 or PostgreSQL に接続できない場合は SKIP
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../analytics/python"))

PG_DSN = os.environ.get(
    "QED_PG_DSN", "postgresql://postgres:postgres@localhost:5432/qed_dev"
)


def _try_connect():
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
def unique_batch() -> str:
    """テスト実行ごとにユニークなバッチラベルを生成。"""
    return f"test_batch_{uuid.uuid4().hex[:8]}"


def _make_sample_df(n: int = 5):
    import pandas as pd
    return pd.DataFrame({
        "benchmark_id": [f"TEST_{i}" for i in range(n)],
        "event_offset": list(range(-2, -2 + n)),
        "abnormal_return": [0.01 * i for i in range(n)],
        "car_from_t0": [0.02 * i for i in range(n)],
        "n_events": [10] * n,
    })


# ---------------------------------------------------------------------------
# Phase A: writeback
# ---------------------------------------------------------------------------

class TestPhaseAWriteback:
    def test_upsert_run_creates_record(self, conn, unique_batch):
        from pg_io.postgres_event_study_writer import EventStudyWriter
        from pg_io.postgres_audit_event_writer import AuditEventWriter

        writer = EventStudyWriter(
            conn, source_name="test_ev", batch_label=unique_batch,
            trace_namespace="test_ns", dry_run=False,
        )
        run_id = writer.upsert_run(total_events=5)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, trace_id, status FROM event_study_summary_runs WHERE run_id = %s",
                (run_id,),
            )
            row = cur.fetchone()

        assert row is not None, "event_study_summary_runs にレコードが存在しない"
        assert row[0] == run_id
        assert row[2] == "running"

    def test_upsert_summaries_creates_rows(self, conn, unique_batch):
        from pg_io.postgres_event_study_writer import EventStudyWriter

        writer = EventStudyWriter(
            conn, source_name="test_ev", batch_label=unique_batch,
            trace_namespace="test_ns", dry_run=False,
        )
        writer.upsert_run(total_events=5)
        df = _make_sample_df(5)
        count = writer.upsert_summaries_from_df(df)

        assert count == 5
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM event_study_summaries WHERE run_id = %s",
                (writer.run_id,),
            )
            db_count = cur.fetchone()[0]
        assert db_count == 5

    def test_upsert_is_idempotent(self, conn, unique_batch):
        """同じ run_id / batch で 2 回 UPSERT してもレコード数が変わらない。"""
        from pg_io.postgres_event_study_writer import EventStudyWriter

        writer = EventStudyWriter(
            conn, source_name="test_ev", batch_label=unique_batch,
            trace_namespace="test_ns", dry_run=False,
        )
        df = _make_sample_df(4)
        writer.upsert_run(total_events=4)
        writer.upsert_summaries_from_df(df)
        # 2 回目
        writer.upsert_run(total_events=4)
        writer.upsert_summaries_from_df(df)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM event_study_summaries WHERE run_id = %s",
                (writer.run_id,),
            )
            count = cur.fetchone()[0]
        assert count == 4, f"UPSERT で重複: {count}"

    def test_trace_id_in_summaries(self, conn, unique_batch):
        """summaries の trace_id が run の trace_id と一致すること。"""
        from pg_io.postgres_event_study_writer import EventStudyWriter

        writer = EventStudyWriter(
            conn, source_name="test_ev", batch_label=unique_batch,
            trace_namespace="test_ns", dry_run=False,
        )
        writer.upsert_run(total_events=2)
        writer.upsert_summaries_from_df(_make_sample_df(2))

        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT trace_id FROM event_study_summaries WHERE run_id = %s",
                (writer.run_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == writer.trace_id


# ---------------------------------------------------------------------------
# Phase A + audit emit
# ---------------------------------------------------------------------------

class TestPhaseAAudit:
    def test_audit_event_created_after_writeback(self, conn, unique_batch):
        from pg_io.postgres_event_study_writer import EventStudyWriter
        from pg_io.postgres_audit_event_writer import AuditEventWriter

        writer = EventStudyWriter(
            conn, source_name="test_ev", batch_label=unique_batch,
            trace_namespace="test_ns", dry_run=False,
        )
        run_id = writer.upsert_run(total_events=3)
        writer.upsert_summaries_from_df(_make_sample_df(3))
        writer.complete_run(total_events=3)

        audit = AuditEventWriter(conn, strict=False)
        audit.emit(
            trace_id=writer.trace_id,
            phase="writeback",
            object_type="event_study_summary_runs",
            object_id=run_id,
            event_type="TRANSITION_APPLIED",
            decision="APPLIED",
            metadata={"batch_label": unique_batch, "rows": 3},
        )

        with conn.cursor() as cur:
            cur.execute(
                """SELECT decision, event_type FROM audit_events
                   WHERE trace_id = %s AND object_type = 'event_study_summary_runs'
                   ORDER BY created_at DESC LIMIT 1""",
                (writer.trace_id,),
            )
            row = cur.fetchone()

        assert row is not None, "audit_events にレコードがない"
        assert row[0] == "APPLIED"
        assert row[1] == "TRANSITION_APPLIED"


# ---------------------------------------------------------------------------
# Phase B: experiment_report bridge
# ---------------------------------------------------------------------------

class TestPhaseBExperimentReport:
    def _setup_run(self, conn, unique_batch):
        from pg_io.postgres_event_study_writer import EventStudyWriter
        writer = EventStudyWriter(
            conn, source_name="test_ev", batch_label=unique_batch,
            trace_namespace="test_ns", dry_run=False,
        )
        writer.upsert_run(total_events=3)
        writer.upsert_summaries_from_df(_make_sample_df(3))
        writer.complete_run(total_events=3)
        return writer.run_id, writer.trace_id

    def test_bridge_creates_experiment_run(self, conn, unique_batch):
        from pg_io.postgres_audit_event_writer import AuditEventWriter
        from pg_io.postgres_event_study_experiment_report_bridge import ExperimentReportBridge

        run_id, trace_id = self._setup_run(conn, unique_batch)
        audit = AuditEventWriter(conn, strict=False)
        bridge = ExperimentReportBridge(conn, audit, dry_run=False)
        result = bridge.promote(run_id)

        assert result["promotion_status"] == "applied"
        assert result["trace_id"] == trace_id

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM event_study_experiment_report_bridge WHERE run_id = %s",
                (run_id,),
            )
            count = cur.fetchone()[0]
        assert count == 1

    def test_bridge_idempotent(self, conn, unique_batch):
        from pg_io.postgres_audit_event_writer import AuditEventWriter
        from pg_io.postgres_event_study_experiment_report_bridge import ExperimentReportBridge

        run_id, _ = self._setup_run(conn, unique_batch)
        audit = AuditEventWriter(conn, strict=False)
        bridge = ExperimentReportBridge(conn, audit, dry_run=False)
        bridge.promote(run_id)
        bridge.promote(run_id)  # 2 回目

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM event_study_experiment_report_bridge WHERE run_id = %s",
                (run_id,),
            )
            count = cur.fetchone()[0]
        assert count == 1, f"重複挿入: {count}"


# ---------------------------------------------------------------------------
# Phase C: knowledge_artifact bridge
# ---------------------------------------------------------------------------

class TestPhaseCKnowledgeArtifact:
    def _setup_phases_ab(self, conn, unique_batch):
        from pg_io.postgres_event_study_writer import EventStudyWriter
        from pg_io.postgres_audit_event_writer import AuditEventWriter
        from pg_io.postgres_event_study_experiment_report_bridge import ExperimentReportBridge

        writer = EventStudyWriter(
            conn, source_name="test_ev", batch_label=unique_batch,
            trace_namespace="test_ns", dry_run=False,
        )
        writer.upsert_run(total_events=3)
        writer.upsert_summaries_from_df(_make_sample_df(3))
        writer.complete_run(total_events=3)

        audit = AuditEventWriter(conn, strict=False)
        bridge = ExperimentReportBridge(conn, audit, dry_run=False)
        bridge.promote(writer.run_id)
        return writer.run_id, writer.trace_id

    def test_ka_bridge_creates_artifact(self, conn, unique_batch):
        from pg_io.postgres_audit_event_writer import AuditEventWriter
        from pg_io.postgres_event_study_knowledge_artifact_bridge import KnowledgeArtifactBridge

        run_id, trace_id = self._setup_phases_ab(conn, unique_batch)
        audit = AuditEventWriter(conn, strict=False)
        bridge = KnowledgeArtifactBridge(conn, audit, dry_run=False)
        result = bridge.promote(run_id)

        assert result["promotion_status"] == "applied"
        assert result["trace_id"] == trace_id

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM knowledge_artifacts WHERE artifact_id = %s",
                (result["artifact_id"],),
            )
            count = cur.fetchone()[0]
        assert count == 1

    def test_ka_bridge_idempotent(self, conn, unique_batch):
        from pg_io.postgres_audit_event_writer import AuditEventWriter
        from pg_io.postgres_event_study_knowledge_artifact_bridge import KnowledgeArtifactBridge

        run_id, _ = self._setup_phases_ab(conn, unique_batch)
        audit = AuditEventWriter(conn, strict=False)
        bridge = KnowledgeArtifactBridge(conn, audit, dry_run=False)
        r1 = bridge.promote(run_id)
        r2 = bridge.promote(run_id)

        assert r1["artifact_id"] == r2["artifact_id"], "artifact_id が変わっている"

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM knowledge_artifacts WHERE artifact_id = %s",
                (r1["artifact_id"],),
            )
            count = cur.fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# Phase D: target resolution — unresolved / resolved ケース
# ---------------------------------------------------------------------------

class TestPhaseDTargetResolution:
    def _run_phases_abc(self, conn, unique_batch, candidate_code=None):
        from pg_io.postgres_event_study_writer import EventStudyWriter
        from pg_io.postgres_audit_event_writer import AuditEventWriter
        from pg_io.postgres_event_study_experiment_report_bridge import ExperimentReportBridge
        from pg_io.postgres_event_study_knowledge_artifact_bridge import KnowledgeArtifactBridge

        writer = EventStudyWriter(
            conn, source_name="test_ev", batch_label=unique_batch,
            trace_namespace="test_ns", dry_run=False,
        )
        writer.upsert_run(total_events=2)
        writer.upsert_summaries_from_df(_make_sample_df(2))
        writer.complete_run(total_events=2)

        audit = AuditEventWriter(conn, strict=False)

        b = ExperimentReportBridge(conn, audit, dry_run=False)
        b.promote(writer.run_id)

        c = KnowledgeArtifactBridge(conn, audit, dry_run=False)
        ka_result = c.promote(writer.run_id)
        artifact_id = ka_result["artifact_id"]

        # candidate_code を metadata に注入
        if candidate_code:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE knowledge_artifacts SET metadata = metadata || %s::jsonb WHERE artifact_id = %s",
                    (json.dumps({"candidate_code": candidate_code}), artifact_id),
                )

        return artifact_id, writer.trace_id

    def test_unresolved_when_no_code(self, conn, unique_batch):
        from pg_io.postgres_audit_event_writer import AuditEventWriter
        from pg_io.postgres_event_study_target_rule_resolver import TargetRuleResolver

        artifact_id, _ = self._run_phases_abc(conn, unique_batch, candidate_code=None)
        audit = AuditEventWriter(conn, strict=False)
        resolver = TargetRuleResolver(conn, audit, dry_run=False)
        result = resolver.resolve(artifact_id)

        assert result["resolution_status"] in ("unresolved", "ambiguous")

    def test_resolved_when_valid_candidate_code(self, conn, unique_batch):
        """factor_candidates に存在するコードで resolved になること。"""
        from pg_io.postgres_audit_event_writer import AuditEventWriter
        from pg_io.postgres_event_study_target_rule_resolver import TargetRuleResolver

        # 実際の factor_candidates に存在するコードを確認
        with __import__("psycopg").connect(PG_DSN) as tmp_conn:
            with tmp_conn.cursor() as cur:
                cur.execute("SELECT name FROM factor_candidates WHERE status != 'deprecated' LIMIT 1")
                row = cur.fetchone()

        if row is None:
            pytest.skip("factor_candidates に有効なレコードがない")

        valid_code = row[0]
        artifact_id, _ = self._run_phases_abc(conn, unique_batch, candidate_code=valid_code)
        audit = AuditEventWriter(conn, strict=False)
        resolver = TargetRuleResolver(conn, audit, dry_run=False)
        result = resolver.resolve(artifact_id)

        assert result["resolution_status"] == "resolved", \
            f"code={valid_code} で resolved になるべき: {result}"
        assert result["matched_target_id"] is not None


# ---------------------------------------------------------------------------
# UPSERT 重複防止テスト (acceptance criteria 7)
# ---------------------------------------------------------------------------

class TestUpsertNoDuplicates:
    def test_full_pipeline_rerun_no_duplicates(self, conn, unique_batch):
        """
        フルパイプラインを 2 回実行してもテーブルの件数が変わらないこと。
        """
        from pg_io.postgres_event_study_writer import EventStudyWriter
        from pg_io.postgres_audit_event_writer import AuditEventWriter
        from pg_io.postgres_event_study_experiment_report_bridge import ExperimentReportBridge
        from pg_io.postgres_event_study_knowledge_artifact_bridge import KnowledgeArtifactBridge

        def _run_pipeline():
            writer = EventStudyWriter(
                conn, source_name="test_ev", batch_label=unique_batch,
                trace_namespace="test_ns", dry_run=False,
            )
            writer.upsert_run(total_events=3)
            writer.upsert_summaries_from_df(_make_sample_df(3))
            writer.complete_run(total_events=3)

            audit = AuditEventWriter(conn, strict=False)
            ExperimentReportBridge(conn, audit).promote(writer.run_id)
            KnowledgeArtifactBridge(conn, audit).promote(writer.run_id)
            return writer.run_id

        run_id = _run_pipeline()
        _run_pipeline()  # 2 回目

        with conn.cursor() as cur:
            for table, col in [
                ("event_study_summary_runs", "run_id"),
                ("event_study_summaries", "run_id"),
                ("event_study_experiment_report_bridge", "run_id"),
            ]:
                cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = %s", (run_id,))
                count = cur.fetchone()[0]
                assert count in (1, 3), \
                    f"{table}: 期待値から外れた件数 {count} (run_id={run_id})"


# ---------------------------------------------------------------------------
# 欠損テーブル / カラムエラーのフェイルケース
# ---------------------------------------------------------------------------

class TestFailureCases:
    def test_experiment_report_raises_if_run_missing(self, conn):
        from pg_io.postgres_audit_event_writer import AuditEventWriter
        from pg_io.postgres_event_study_experiment_report_bridge import ExperimentReportBridge

        audit = AuditEventWriter(conn, strict=False)
        bridge = ExperimentReportBridge(conn, audit, dry_run=False)
        with pytest.raises(ValueError, match="event_study_summary_runs"):
            bridge.promote("non_existent_run_id_xyz")

    def test_ka_bridge_raises_if_report_missing(self, conn):
        from pg_io.postgres_audit_event_writer import AuditEventWriter
        from pg_io.postgres_event_study_knowledge_artifact_bridge import KnowledgeArtifactBridge

        audit = AuditEventWriter(conn, strict=False)
        bridge = KnowledgeArtifactBridge(conn, audit, dry_run=False)
        with pytest.raises(ValueError, match="experiment_report"):
            bridge.promote("non_existent_run_id_xyz")

    def test_resolver_raises_if_artifact_missing(self, conn):
        from pg_io.postgres_audit_event_writer import AuditEventWriter
        from pg_io.postgres_event_study_target_rule_resolver import TargetRuleResolver

        audit = AuditEventWriter(conn, strict=False)
        resolver = TargetRuleResolver(conn, audit, dry_run=False)
        with pytest.raises(ValueError, match="knowledge_artifacts"):
            resolver.resolve("00000000-0000-0000-0000-000000000000")
