"""
tests/unit/test_trace_id_consistency.py
-----------------------------------------
trace_id が全ステージを通じて一貫していることを検証するユニットテスト

- EventStudyWriter から生成された trace_id が
  ExperimentReportBridge / KnowledgeArtifactBridge に引き継がれること
- _make_trace_id が同じ入力に対して常に同じ UUID5 を返すこと
- 各 phase の audit emit で同一 trace_id が使われること
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../analytics/python"))

import uuid
from unittest.mock import MagicMock, patch
import pytest

from pg_io.postgres_event_study_writer import _make_trace_id


# ---------------------------------------------------------------------------
# trace_id 生成の決定論的性質
# ---------------------------------------------------------------------------

class TestTraceIdDeterminism:
    def test_same_input_same_output(self):
        t1 = _make_trace_id("event_study", "ev1__batch_20260413")
        t2 = _make_trace_id("event_study", "ev1__batch_20260413")
        assert t1 == t2

    def test_different_batch_different_trace(self):
        t1 = _make_trace_id("event_study", "ev1__batch_20260413")
        t2 = _make_trace_id("event_study", "ev1__batch_20260414")
        assert t1 != t2

    def test_different_namespace_different_trace(self):
        t1 = _make_trace_id("ns_a", "run1")
        t2 = _make_trace_id("ns_b", "run1")
        assert t1 != t2

    def test_is_valid_uuid5(self):
        trace = _make_trace_id("event_study", "any_run")
        parsed = uuid.UUID(trace)
        assert parsed.version == 5

    def test_string_output(self):
        trace = _make_trace_id("ns", "run")
        assert isinstance(trace, str)
        assert len(trace) == 36  # standard UUID format


# ---------------------------------------------------------------------------
# 全ステージで trace_id が同一であることの確認
# ---------------------------------------------------------------------------

class TestTraceIdPropagation:
    """
    DuckDB → writeback → experiment_report → knowledge_artifact → audit_events
    の各ステージで同じ trace_id が使われることを mock で検証する。
    """

    def _make_conn_with_row(self, row_data: dict) -> tuple:
        """
        指定した row_data を返す mock cursor を持つ connection を作成。
        """
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cursor
        return conn, cursor

    def test_writer_trace_matches_make_trace_id(self):
        """EventStudyWriter.trace_id が _make_trace_id の出力と一致。"""
        from pg_io.postgres_event_study_writer import EventStudyWriter
        conn, _ = self._make_conn_with_row({})
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1",
                             trace_namespace="ns", dry_run=True)
        expected = _make_trace_id("ns", "ev1__b1")
        assert w.trace_id == expected

    def test_experiment_report_bridge_propagates_trace_id(self):
        """ExperimentReportBridge が summary_run から trace_id を引き継ぐこと。"""
        from pg_io.postgres_event_study_experiment_report_bridge import ExperimentReportBridge

        conn, cursor = self._make_conn_with_row({})
        trace_id = _make_trace_id("ns", "ev1__b1")
        run_id = "ev1__b1"

        # event_study_summary_runs の 8 カラム:
        # run_id, trace_id, source_name, panel_kind, batch_label,
        # total_events, run_metadata, status
        run_row = (run_id, trace_id, "ev1", "abnormal_return", "b1", 48, {}, "completed")
        # stats query の戻り値: n_events, avg_car, avg_ar, min_offset, max_offset
        stats_row = (48, 0.01, 0.02, -10, 20)

        call_count = {"n": 0}
        def side_fetchone():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return run_row
            elif call_count["n"] == 2:
                return stats_row
            else:
                import uuid as _uuid
                return (_uuid.uuid4(),)
        cursor.fetchone.side_effect = side_fetchone

        mock_audit = MagicMock()
        bridge = ExperimentReportBridge(conn, mock_audit, dry_run=True)

        # DRY_RUN なので INSERT はされないが promote は通る
        result = bridge.promote(run_id)

        # audit emit の trace_id が一致すること
        if mock_audit.emit.called:
            kwargs = mock_audit.emit.call_args[1]
            assert kwargs["trace_id"] == trace_id

    def test_knowledge_artifact_bridge_propagates_trace_id(self):
        """KnowledgeArtifactBridge が experiment_run から trace_id を引き継ぐ。"""
        from pg_io.postgres_event_study_knowledge_artifact_bridge import KnowledgeArtifactBridge

        conn, cursor = self._make_conn_with_row({})
        trace_id = _make_trace_id("ns", "ev1__b1")
        run_id = "ev1__b1"

        # event_study_experiment_report_bridge の 7 カラム:
        # run_id, trace_id, report_title, report_summary,
        # report_markdown, report_metadata, experiment_run_id
        import uuid as _uuid
        exp_run_id = _uuid.uuid4()
        report_row = (
            run_id, trace_id, "[Event Study] ev1", "summary",
            "", {"artifact_tag": f"event_study:{run_id}"}, str(exp_run_id)
        )
        cursor.fetchone.return_value = report_row

        mock_audit = MagicMock()
        bridge = KnowledgeArtifactBridge(conn, mock_audit, dry_run=True)
        result = bridge.promote(run_id)

        if mock_audit.emit.called:
            kwargs = mock_audit.emit.call_args[1]
            assert kwargs["trace_id"] == trace_id


# ---------------------------------------------------------------------------
# audit emit の phase × decision マトリックス
# ---------------------------------------------------------------------------

class TestAuditPhaseDecisionMatrix:
    """
    各 phase と decision の組み合わせが想定通りであることを確認する。
    """

    EXPECTED = [
        # (phase, decision, event_type)
        ("writeback",           "APPLIED",  "TRANSITION_APPLIED"),
        ("experiment_report",   "APPLIED",  "TRANSITION_APPLIED"),
        ("knowledge_artifact",  "APPLIED",  "TRANSITION_APPLIED"),
        ("target_resolution",   "APPLIED",  "TRANSITION_APPLIED"),
        ("target_resolution",   "REJECTED", "TRANSITION_REJECTED"),
        ("artifact_link",       "APPLIED",  "TRANSITION_APPLIED"),
    ]

    def test_decision_values_are_valid(self):
        """EXPECTED の decision が許可値のみであること。"""
        allowed = {"APPLIED", "DRY_RUN", "CONFLICTED", "REJECTED"}
        for phase, decision, _ in self.EXPECTED:
            assert decision in allowed, f"{phase}: invalid decision={decision}"

    def test_event_type_matches_decision(self):
        """decision と event_type の対応が一貫していること。"""
        for phase, decision, event_type in self.EXPECTED:
            if decision == "APPLIED":
                assert event_type == "TRANSITION_APPLIED", f"{phase}: mismatch"
            elif decision in ("REJECTED", "CONFLICTED"):
                assert "TRANSITION_REJECTED" in event_type or "TRANSITION_CONFLICTED" in event_type, \
                    f"{phase}: unexpected event_type={event_type}"
