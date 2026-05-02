"""
tests/unit/test_event_study_writer.py
--------------------------------------
EventStudyWriter のユニットテスト

- run_id / trace_id 生成ロジック
- dry_run 時に DB 書き込みが行われないこと
- 必須列チェック (ValueError)
- UPSERT 呼び出し確認 (mock cursor)
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../analytics/python"))

from pg_io.postgres_event_study_writer import (
    EventStudyWriter,
    _build_run_id,
    _make_trace_id,
    REQUIRED_PANEL_COLS,
)


# ---------------------------------------------------------------------------
# Helper: mock connection
# ---------------------------------------------------------------------------

def _make_mock_conn():
    """psycopg Connection のモックを返す。"""
    conn = MagicMock()
    cursor = MagicMock()
    # context manager サポート
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor
    return conn, cursor


# ---------------------------------------------------------------------------
# _build_run_id
# ---------------------------------------------------------------------------

class TestBuildRunId:
    def test_basic(self):
        assert _build_run_id("ev1", "batch_20260101") == "ev1__batch_20260101"

    def test_with_special_chars(self):
        result = _build_run_id("source-a", "label_v2")
        assert result == "source-a__label_v2"


# ---------------------------------------------------------------------------
# _make_trace_id
# ---------------------------------------------------------------------------

class TestMakeTraceId:
    def test_returns_valid_uuid(self):
        tid = _make_trace_id("event_study", "ev1__batch_20260101")
        parsed = uuid.UUID(tid)
        assert parsed.version == 5

    def test_deterministic(self):
        t1 = _make_trace_id("ns", "run1")
        t2 = _make_trace_id("ns", "run1")
        assert t1 == t2

    def test_different_namespace(self):
        t1 = _make_trace_id("ns_a", "run1")
        t2 = _make_trace_id("ns_b", "run1")
        assert t1 != t2

    def test_different_run_id(self):
        t1 = _make_trace_id("ns", "run1")
        t2 = _make_trace_id("ns", "run2")
        assert t1 != t2


# ---------------------------------------------------------------------------
# EventStudyWriter.__init__
# ---------------------------------------------------------------------------

class TestEventStudyWriterInit:
    def test_run_id_composed(self):
        conn, _ = _make_mock_conn()
        w = EventStudyWriter(
            conn, source_name="ev1", batch_label="b1",
            panel_kind="abnormal_return", trace_namespace="ns",
        )
        assert w.run_id == "ev1__b1"

    def test_trace_id_is_uuid5(self):
        conn, _ = _make_mock_conn()
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1",
                             trace_namespace="ns")
        uuid.UUID(w.trace_id)  # raises if invalid

    def test_dry_run_default_false(self):
        conn, _ = _make_mock_conn()
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1")
        assert w.dry_run is False

    def test_dry_run_explicit(self):
        conn, _ = _make_mock_conn()
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1", dry_run=True)
        assert w.dry_run is True

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("EVENT_STUDY_SOURCE_NAME", "env_src")
        monkeypatch.setenv("EVENT_STUDY_BATCH_LABEL", "env_batch")
        monkeypatch.setenv("EVENT_STUDY_WRITEBACK_DRY_RUN", "true")
        conn, _ = _make_mock_conn()
        w = EventStudyWriter(conn)
        assert w.source_name == "env_src"
        assert w.batch_label == "env_batch"
        assert w.dry_run is True


# ---------------------------------------------------------------------------
# upsert_run
# ---------------------------------------------------------------------------

class TestUpsertRun:
    def test_dry_run_skips_execute(self):
        conn, cur = _make_mock_conn()
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1", dry_run=True)
        result = w.upsert_run(total_events=10)
        cur.execute.assert_not_called()
        assert result == "ev1__b1"

    def test_normal_calls_execute(self):
        conn, cur = _make_mock_conn()
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1", dry_run=False)
        result = w.upsert_run(total_events=5)
        cur.execute.assert_called_once()
        assert result == "ev1__b1"

    def test_sql_contains_on_conflict(self):
        conn, cur = _make_mock_conn()
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1")
        w.upsert_run()
        sql_arg = cur.execute.call_args[0][0]
        assert "ON CONFLICT" in sql_arg
        assert "DO UPDATE" in sql_arg


# ---------------------------------------------------------------------------
# upsert_summaries_from_df
# ---------------------------------------------------------------------------

def _make_panel_df(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame({
        "benchmark_id": [f"TKR_{i}" for i in range(n)],
        "event_offset": list(range(n)),
        "abnormal_return": [0.01 * i for i in range(n)],
        "car_from_t0":    [0.02 * i for i in range(n)],
        "n_events":       [100] * n,
    })


class TestUpsertSummariesFromDf:
    def test_missing_required_cols_raises(self):
        conn, _ = _make_mock_conn()
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1")
        bad_df = pd.DataFrame({"only_col": [1, 2]})
        with pytest.raises(ValueError, match="必須列"):
            w.upsert_summaries_from_df(bad_df)

    def test_dry_run_returns_zero(self):
        conn, cur = _make_mock_conn()
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1", dry_run=True)
        df = _make_panel_df(5)
        count = w.upsert_summaries_from_df(df)
        assert count == 0
        cur.execute.assert_not_called()

    def test_normal_upserts_all_rows(self):
        conn, cur = _make_mock_conn()
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1")
        df = _make_panel_df(4)
        count = w.upsert_summaries_from_df(df)
        assert count == 4
        assert cur.execute.call_count == 4

    def test_upsert_sql_has_on_conflict(self):
        conn, cur = _make_mock_conn()
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1")
        df = _make_panel_df(1)
        w.upsert_summaries_from_df(df)
        sql_arg = cur.execute.call_args[0][0]
        assert "ON CONFLICT" in sql_arg
        assert "DO UPDATE" in sql_arg

    def test_extra_columns_go_to_extra_metrics(self):
        conn, cur = _make_mock_conn()
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1")
        df = _make_panel_df(1)
        df["custom_col"] = [42.0]
        w.upsert_summaries_from_df(df)
        # execute params の最後 (extra_metrics JSON) に custom_col が含まれること
        params = cur.execute.call_args[0][1]
        extra = json.loads(params[-1])
        assert "custom_col" in extra

    def test_nan_values_become_none(self):
        """NaN は None に変換されて INSERT される。"""
        import numpy as np
        conn, cur = _make_mock_conn()
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1")
        df = _make_panel_df(1)
        df["abnormal_return"] = [float("nan")]
        w.upsert_summaries_from_df(df)
        params = cur.execute.call_args[0][1]
        # params[5] = abnormal_return
        assert params[5] is None


# ---------------------------------------------------------------------------
# complete_run / fail_run
# ---------------------------------------------------------------------------

class TestRunLifecycle:
    def test_complete_run_updates_status(self):
        conn, cur = _make_mock_conn()
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1")
        w.complete_run(total_events=48)
        cur.execute.assert_called_once()
        sql = cur.execute.call_args[0][0]
        assert "completed" in sql.lower()

    def test_complete_run_dry_run_skips(self):
        conn, cur = _make_mock_conn()
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1", dry_run=True)
        w.complete_run(total_events=10)
        cur.execute.assert_not_called()

    def test_fail_run_updates_status(self):
        conn, cur = _make_mock_conn()
        w = EventStudyWriter(conn, source_name="ev1", batch_label="b1")
        w.fail_run("some error")
        cur.execute.assert_called_once()
        sql = cur.execute.call_args[0][0]
        assert "failed" in sql.lower()
