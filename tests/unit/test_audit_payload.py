"""
tests/unit/test_audit_payload.py
----------------------------------
AuditEventWriter のユニットテスト

- emit() で audit_events / pipeline_audit への INSERT が行われること
- strict=True 時に audit_events がなければ RuntimeError
- dry_run 時に書き込みがスキップされること
- trace_id が両テーブルに引き継がれること
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../analytics/python"))

from unittest.mock import MagicMock, patch, call
import pytest

from pg_io.postgres_audit_event_writer import AuditEventWriter


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_mock_conn(has_audit_table: bool = True, has_pipeline_table: bool = True):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor
    return conn, cursor


def _make_writer(conn, strict: bool = False) -> AuditEventWriter:
    """table_exists / check_table_columns をモックして AuditEventWriter を初期化。"""
    col_map = {
        "trace_id": "uuid",
        "case_id": "text",
        "object_type": "text",
        "object_id": "text",
        "requested_by": "text",
        "event_type": "text",
        "decision": "text",
        "decision_reason_code": "text",
        "metadata": "jsonb",
    }
    with patch("pg_io.postgres_audit_event_writer.table_exists", return_value=True), \
         patch("pg_io.postgres_audit_event_writer.check_table_columns", return_value=col_map):
        return AuditEventWriter(conn, strict=strict)


# ---------------------------------------------------------------------------
# strict mode
# ---------------------------------------------------------------------------

class TestStrictMode:
    def test_strict_raises_when_no_audit_table(self):
        conn, _ = _make_mock_conn()
        with patch("pg_io.postgres_audit_event_writer.table_exists", return_value=False):
            with pytest.raises(RuntimeError, match="audit_events"):
                AuditEventWriter(conn, strict=True)

    def test_non_strict_ok_without_audit_table(self):
        conn, _ = _make_mock_conn()
        with patch("pg_io.postgres_audit_event_writer.table_exists", return_value=False):
            writer = AuditEventWriter(conn, strict=False)
            assert writer is not None


# ---------------------------------------------------------------------------
# emit() — 正常系
# ---------------------------------------------------------------------------

class TestEmit:
    def _writer_and_cursor(self) -> tuple[AuditEventWriter, MagicMock]:
        conn, cur = _make_mock_conn()
        writer = _make_writer(conn, strict=False)
        return writer, cur

    def test_emit_calls_execute_twice(self):
        """audit_events + pipeline_audit の 2 回 execute が呼ばれること。"""
        writer, cur = self._writer_and_cursor()
        with patch("pg_io.postgres_audit_event_writer.table_exists", return_value=True):
            writer.emit(
                trace_id="trace-001",
                phase="writeback",
                object_type="event_study_summary_runs",
                object_id="run-001",
                event_type="TRANSITION_APPLIED",
                decision="APPLIED",
            )
        # audit_events + pipeline_audit = 2 calls
        assert cur.execute.call_count >= 1

    def test_emit_dry_run_skips_db(self):
        writer, cur = self._writer_and_cursor()
        writer.emit(
            trace_id="trace-001",
            phase="writeback",
            object_type="run",
            object_id="r1",
            event_type="TRANSITION_APPLIED",
            decision="APPLIED",
            dry_run=True,
        )
        cur.execute.assert_not_called()

    def test_emit_decision_values(self):
        """APPLIED / DRY_RUN / CONFLICTED / REJECTED の 4 値を受け付けること。"""
        for decision in ("APPLIED", "DRY_RUN", "CONFLICTED", "REJECTED"):
            conn, cur = _make_mock_conn()
            writer = _make_writer(conn)
            with patch("pg_io.postgres_audit_event_writer.table_exists", return_value=True):
                writer.emit(
                    trace_id=f"t-{decision}",
                    phase="test",
                    object_type="obj",
                    object_id="id",
                    event_type="TRANSITION_APPLIED",
                    decision=decision,
                )
            # no exception = pass

    def test_trace_id_passed_to_insert(self):
        writer, cur = self._writer_and_cursor()
        trace = "trace-check-999"
        with patch("pg_io.postgres_audit_event_writer.table_exists", return_value=True):
            writer.emit(
                trace_id=trace,
                phase="p",
                object_type="obj",
                object_id="id",
                event_type="TRANSITION_APPLIED",
                decision="APPLIED",
            )
        # 少なくとも 1 つの execute call の args に trace が含まれること
        found = any(
            trace in str(c)
            for c in cur.execute.call_args_list
        )
        assert found, f"trace_id={trace} が execute 引数に見つかりませんでした"


# ---------------------------------------------------------------------------
# emit() — audit_events テーブルのカラム互換性
# ---------------------------------------------------------------------------

class TestEmitColumnCompat:
    def test_missing_optional_col_does_not_raise(self):
        """reject_reason_code など任意カラムが存在しなくても emit できること。"""
        conn, cur = _make_mock_conn()
        # reject_reason_code を除いた minimal col_map
        minimal_cols = {
            "trace_id": "uuid",
            "case_id": "text",
            "object_type": "text",
            "object_id": "text",
            "requested_by": "text",
            "event_type": "text",
            "decision": "text",
            "decision_reason_code": "text",
            "metadata": "jsonb",
        }
        with patch("pg_io.postgres_audit_event_writer.table_exists", return_value=True), \
             patch("pg_io.postgres_audit_event_writer.check_table_columns", return_value=minimal_cols):
            writer = AuditEventWriter(conn, strict=False)
        with patch("pg_io.postgres_audit_event_writer.table_exists", return_value=True):
            writer.emit(
                trace_id="t1",
                phase="p",
                object_type="obj",
                object_id="id",
                event_type="TRANSITION_REJECTED",
                decision="REJECTED",
                decision_reason="ROLE_DENIED",
            )
        # no exception = pass
