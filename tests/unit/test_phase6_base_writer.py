"""
test_phase6_base_writer.py
--------------------------
Phase 6: base_writer.py 共通骨格 + 既存 writer 委譲 の単体テスト。

テストクラス:
  TestSafeFloat           (8) — safe_float() 境界値・型バリエーション
  TestSafeFloatOpt        (7) — safe_float_opt() 境界値・None ハンドリング
  TestSafeJson            (9) — safe_json() NaN/Inf/datetime/ネスト/default
  TestNowUtc              (3) — now_utc() UTC タイムゾーン確認
  TestBaseWriterInit      (5) — BaseWriter.__init__ / writer_name 設定
  TestBaseWriterGuardDryRun (7) — _guard_dry_run() True/False 分岐・ログ
  TestBaseWriterCommit    (5) — _commit() dry_run ガード / conn.commit() 呼び出し
  TestBaseWriterHelpers   (7) — _sf / _sfo / _sj / _now ショートカット
  TestBaseWriterExecute   (5) — _execute() 単体 / executemany 呼び出し検証
  TestDryRunSkipped       (3) — DryRunSkipped 例外クラスの振る舞い
  TestSafeJsonBackward    (6) — 既存 writer の _safe_json と同等出力の確認
  TestFrostWriterImport   (5) — postgres_frost_writer が base_writer の関数を使用
  TestEmlAlphaWriterImport(4) — postgres_eml_alpha_writer が base_writer の関数を使用
  TestEmlBacktestWriterImport(4) — postgres_eml_backtest_writer が base_writer の関数を使用

合計: 78 テスト
"""
from __future__ import annotations

import json
import logging
import math
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

from analytics.python.pg_io.base_writer import (
    BaseWriter,
    DryRunSkipped,
    now_utc,
    safe_float,
    safe_float_opt,
    safe_json,
)


# ---------------------------------------------------------------------------
# TestSafeFloat (8)
# ---------------------------------------------------------------------------

class TestSafeFloat(unittest.TestCase):
    """safe_float() の境界値テスト。"""

    def test_normal_float(self):
        self.assertEqual(safe_float(1.5), 1.5)

    def test_zero(self):
        self.assertEqual(safe_float(0.0), 0.0)

    def test_nan_returns_zero(self):
        self.assertEqual(safe_float(float("nan")), 0.0)

    def test_inf_returns_zero(self):
        self.assertEqual(safe_float(float("inf")), 0.0)

    def test_neg_inf_returns_zero(self):
        self.assertEqual(safe_float(float("-inf")), 0.0)

    def test_none_returns_zero(self):
        self.assertEqual(safe_float(None), 0.0)

    def test_string_int(self):
        self.assertEqual(safe_float("3"), 3.0)

    def test_invalid_string(self):
        self.assertEqual(safe_float("abc"), 0.0)


# ---------------------------------------------------------------------------
# TestSafeFloatOpt (7)
# ---------------------------------------------------------------------------

class TestSafeFloatOpt(unittest.TestCase):
    """safe_float_opt() の境界値テスト。"""

    def test_normal_float(self):
        self.assertEqual(safe_float_opt(1.5), 1.5)

    def test_none_stays_none(self):
        self.assertIsNone(safe_float_opt(None))

    def test_nan_returns_none(self):
        self.assertIsNone(safe_float_opt(float("nan")))

    def test_inf_returns_none(self):
        self.assertIsNone(safe_float_opt(float("inf")))

    def test_neg_inf_returns_none(self):
        self.assertIsNone(safe_float_opt(float("-inf")))

    def test_zero(self):
        self.assertEqual(safe_float_opt(0.0), 0.0)

    def test_invalid_string_returns_none(self):
        self.assertIsNone(safe_float_opt("bad"))


# ---------------------------------------------------------------------------
# TestSafeJson (9)
# ---------------------------------------------------------------------------

class TestSafeJson(unittest.TestCase):
    """safe_json() のサニタイズ / シリアライズテスト。"""

    def test_simple_dict(self):
        result = json.loads(safe_json({"a": 1, "b": 2.5}))
        self.assertEqual(result, {"a": 1, "b": 2.5})

    def test_nan_in_dict(self):
        result = json.loads(safe_json({"x": float("nan"), "y": 1.0}))
        self.assertIsNone(result["x"])
        self.assertEqual(result["y"], 1.0)

    def test_inf_in_list(self):
        result = json.loads(safe_json([float("inf"), 2.0, float("-inf")]))
        self.assertIsNone(result[0])
        self.assertEqual(result[1], 2.0)
        self.assertIsNone(result[2])

    def test_nested_dict(self):
        data = {"outer": {"inner": float("nan")}}
        result = json.loads(safe_json(data))
        self.assertIsNone(result["outer"]["inner"])

    def test_datetime_serialized_as_iso(self):
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = json.loads(safe_json({"ts": dt}))
        self.assertIn("2024-01-15", result["ts"])

    def test_none_value_preserved(self):
        result = json.loads(safe_json({"x": None}))
        self.assertIsNone(result["x"])

    def test_string_value(self):
        result = json.loads(safe_json("hello"))
        self.assertEqual(result, "hello")

    def test_returns_string(self):
        self.assertIsInstance(safe_json({"a": 1}), str)

    def test_default_str_fallback(self):
        # str() にフォールバックする未知の型
        class Custom:
            def __str__(self):
                return "custom_str"
        result = safe_json({"obj": Custom()})
        self.assertIn("custom_str", result)


# ---------------------------------------------------------------------------
# TestNowUtc (3)
# ---------------------------------------------------------------------------

class TestNowUtc(unittest.TestCase):
    """now_utc() のタイムゾーンテスト。"""

    def test_returns_datetime(self):
        result = now_utc()
        self.assertIsInstance(result, datetime)

    def test_is_utc(self):
        result = now_utc()
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_is_recent(self):
        before = datetime.now(timezone.utc)
        result = now_utc()
        after = datetime.now(timezone.utc)
        self.assertGreaterEqual(result, before)
        self.assertLessEqual(result, after)


# ---------------------------------------------------------------------------
# ConcreteWriter — テスト用具体クラス
# ---------------------------------------------------------------------------

class ConcreteWriter(BaseWriter):
    """テスト用 BaseWriter 具体実装。"""
    pass


# ---------------------------------------------------------------------------
# TestBaseWriterInit (5)
# ---------------------------------------------------------------------------

class TestBaseWriterInit(unittest.TestCase):
    """BaseWriter.__init__ のテスト。"""

    def _make_conn(self):
        return MagicMock(spec=["execute", "commit", "cursor", "rollback"])

    def test_conn_stored(self):
        conn = self._make_conn()
        w = ConcreteWriter(conn)
        self.assertIs(w.conn, conn)

    def test_dry_run_default_false(self):
        conn = self._make_conn()
        w = ConcreteWriter(conn)
        self.assertFalse(w.dry_run)

    def test_dry_run_true(self):
        conn = self._make_conn()
        w = ConcreteWriter(conn, dry_run=True)
        self.assertTrue(w.dry_run)

    def test_writer_name_default(self):
        conn = self._make_conn()
        w = ConcreteWriter(conn)
        self.assertEqual(w._writer_name, "ConcreteWriter")

    def test_writer_name_custom(self):
        conn = self._make_conn()
        w = ConcreteWriter(conn, writer_name="MyWriter")
        self.assertEqual(w._writer_name, "MyWriter")


# ---------------------------------------------------------------------------
# TestBaseWriterGuardDryRun (7)
# ---------------------------------------------------------------------------

class TestBaseWriterGuardDryRun(unittest.TestCase):
    """_guard_dry_run() の分岐テスト。"""

    def _make_conn(self):
        return MagicMock(spec=["execute", "commit", "cursor"])

    def test_returns_false_when_not_dry_run(self):
        w = ConcreteWriter(self._make_conn(), dry_run=False)
        self.assertFalse(w._guard_dry_run("op"))

    def test_returns_true_when_dry_run(self):
        w = ConcreteWriter(self._make_conn(), dry_run=True)
        self.assertTrue(w._guard_dry_run("op"))

    def test_logs_skip_message_when_dry_run(self):
        w = ConcreteWriter(self._make_conn(), dry_run=True, writer_name="TW")
        with self.assertLogs("analytics.python.pg_io.base_writer", level="INFO") as cm:
            w._guard_dry_run("test_op")
        self.assertTrue(any("[DRY_RUN]" in line for line in cm.output))

    def test_no_log_when_not_dry_run(self):
        w = ConcreteWriter(self._make_conn(), dry_run=False)
        # INFO ログが出ないことを確認 (assertLogs は1件以上必要なので警告レベルで確認)
        with self.assertRaises(AssertionError):
            with self.assertLogs("analytics.python.pg_io.base_writer", level="INFO") as cm:
                w._guard_dry_run("test_op")
                # 何もログが出なければ AssertionError が発生する

    def test_label_appears_in_log(self):
        w = ConcreteWriter(self._make_conn(), dry_run=True)
        with self.assertLogs("analytics.python.pg_io.base_writer", level="INFO") as cm:
            w._guard_dry_run("my_label_xyz")
        self.assertTrue(any("my_label_xyz" in line for line in cm.output))

    def test_writer_name_in_log(self):
        w = ConcreteWriter(self._make_conn(), dry_run=True, writer_name="SpecialWriter")
        with self.assertLogs("analytics.python.pg_io.base_writer", level="INFO") as cm:
            w._guard_dry_run("some_op")
        self.assertTrue(any("SpecialWriter" in line for line in cm.output))

    def test_guard_called_twice(self):
        """複数回呼び出しても問題なく動作する。"""
        w = ConcreteWriter(self._make_conn(), dry_run=True)
        with self.assertLogs("analytics.python.pg_io.base_writer", level="INFO"):
            r1 = w._guard_dry_run("op1")
            r2 = w._guard_dry_run("op2")
        self.assertTrue(r1)
        self.assertTrue(r2)


# ---------------------------------------------------------------------------
# TestBaseWriterCommit (5)
# ---------------------------------------------------------------------------

class TestBaseWriterCommit(unittest.TestCase):
    """_commit() のテスト。"""

    def _make_conn(self):
        return MagicMock(spec=["execute", "commit", "cursor"])

    def test_commit_called_when_not_dry_run(self):
        conn = self._make_conn()
        w = ConcreteWriter(conn, dry_run=False)
        w._commit()
        conn.commit.assert_called_once()

    def test_commit_not_called_when_dry_run(self):
        conn = self._make_conn()
        w = ConcreteWriter(conn, dry_run=True)
        w._commit()
        conn.commit.assert_not_called()

    def test_commit_called_multiple_times(self):
        conn = self._make_conn()
        w = ConcreteWriter(conn, dry_run=False)
        w._commit()
        w._commit()
        self.assertEqual(conn.commit.call_count, 2)

    def test_commit_propagates_exception(self):
        conn = self._make_conn()
        conn.commit.side_effect = RuntimeError("DB error")
        w = ConcreteWriter(conn, dry_run=False)
        with self.assertRaises(RuntimeError):
            w._commit()

    def test_dry_run_commit_skipped_silently(self):
        conn = self._make_conn()
        w = ConcreteWriter(conn, dry_run=True)
        # 例外が出ないことを確認
        w._commit()
        w._commit()
        conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# TestBaseWriterHelpers (7)
# ---------------------------------------------------------------------------

class TestBaseWriterHelpers(unittest.TestCase):
    """静的ヘルパー _sf / _sfo / _sj / _now のテスト。"""

    def setUp(self):
        conn = MagicMock(spec=["execute", "commit", "cursor"])
        self.w = ConcreteWriter(conn)

    def test_sf_delegates_to_safe_float(self):
        self.assertEqual(self.w._sf(float("nan")), 0.0)
        self.assertEqual(self.w._sf(1.5), 1.5)

    def test_sf_nan_returns_zero(self):
        self.assertEqual(self.w._sf(float("nan")), 0.0)

    def test_sfo_delegates_to_safe_float_opt(self):
        self.assertIsNone(self.w._sfo(float("inf")))
        self.assertEqual(self.w._sfo(2.0), 2.0)

    def test_sfo_none_stays_none(self):
        self.assertIsNone(self.w._sfo(None))

    def test_sj_delegates_to_safe_json(self):
        result = json.loads(self.w._sj({"x": float("nan")}))
        self.assertIsNone(result["x"])

    def test_now_returns_utc(self):
        dt = self.w._now()
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_helpers_are_static(self):
        # インスタンスなしでも呼び出せる (staticmethod 確認)
        self.assertEqual(ConcreteWriter._sf(float("nan")), 0.0)
        self.assertIsNone(ConcreteWriter._sfo(None))


# ---------------------------------------------------------------------------
# TestBaseWriterExecute (5)
# ---------------------------------------------------------------------------

class TestBaseWriterExecute(unittest.TestCase):
    """_execute() のテスト。"""

    def _make_conn(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=MagicMock())
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn

    def test_single_execute_calls_conn_execute(self):
        conn = self._make_conn()
        w = ConcreteWriter(conn)
        w._execute("SELECT 1", (1,))
        conn.execute.assert_called_once_with("SELECT 1", (1,))

    def test_single_execute_no_params(self):
        conn = self._make_conn()
        w = ConcreteWriter(conn)
        w._execute("SELECT 1")
        conn.execute.assert_called_once_with("SELECT 1", None)

    def test_many_calls_executemany(self):
        conn = self._make_conn()
        mock_cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = mock_cursor
        w = ConcreteWriter(conn)
        rows = [(1, "a"), (2, "b")]
        w._execute("INSERT INTO t VALUES (%s, %s)", rows, many=True)
        mock_cursor.executemany.assert_called_once_with(
            "INSERT INTO t VALUES (%s, %s)", rows
        )

    def test_many_empty_rows(self):
        conn = self._make_conn()
        mock_cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = mock_cursor
        w = ConcreteWriter(conn)
        w._execute("INSERT INTO t VALUES (%s)", [], many=True)
        mock_cursor.executemany.assert_called_once()

    def test_single_execute_propagates_exception(self):
        conn = self._make_conn()
        conn.execute.side_effect = RuntimeError("SQL error")
        w = ConcreteWriter(conn)
        with self.assertRaises(RuntimeError):
            w._execute("BAD SQL")


# ---------------------------------------------------------------------------
# TestDryRunSkipped (3)
# ---------------------------------------------------------------------------

class TestDryRunSkipped(unittest.TestCase):
    """DryRunSkipped 例外クラスのテスト。"""

    def test_is_exception(self):
        self.assertTrue(issubclass(DryRunSkipped, Exception))

    def test_can_be_raised(self):
        with self.assertRaises(DryRunSkipped):
            raise DryRunSkipped("skipped because dry_run")

    def test_message_preserved(self):
        try:
            raise DryRunSkipped("test_msg")
        except DryRunSkipped as e:
            self.assertEqual(str(e), "test_msg")


# ---------------------------------------------------------------------------
# TestSafeJsonBackward (6)
# — 既存 writer の _safe_json と base_writer.safe_json の出力が一致する
# ---------------------------------------------------------------------------

class TestSafeJsonBackward(unittest.TestCase):
    """
    Phase 6 移行後の後方互換テスト。
    既存の各 writer が持っていた独自 _safe_json と base_writer.safe_json の
    出力結果が一致することを確認する。
    """

    def _old_safe_json_frost(self, obj):
        """frost_writer 旧実装の複製。"""
        import json, math
        def _sanitize(v):
            if isinstance(v, float):
                return None if (math.isnan(v) or math.isinf(v)) else v
            if isinstance(v, dict):
                return {k: _sanitize(vv) for k, vv in v.items()}
            if isinstance(v, list):
                return [_sanitize(x) for x in v]
            return v
        return json.dumps(_sanitize(obj), default=str)

    def test_plain_dict_matches(self):
        data = {"a": 1.0, "b": "text", "c": None}
        self.assertEqual(safe_json(data), self._old_safe_json_frost(data))

    def test_nan_dict_matches(self):
        data = {"score": float("nan"), "val": 3.14}
        self.assertEqual(
            json.loads(safe_json(data)),
            json.loads(self._old_safe_json_frost(data)),
        )

    def test_nested_nan_matches(self):
        data = {"outer": {"inner": float("nan"), "ok": 1.0}}
        self.assertEqual(
            json.loads(safe_json(data)),
            json.loads(self._old_safe_json_frost(data)),
        )

    def test_list_with_inf_matches(self):
        data = [1.0, float("inf"), 3.0, float("-inf")]
        self.assertEqual(
            json.loads(safe_json(data)),
            json.loads(self._old_safe_json_frost(data)),
        )

    def test_empty_dict_matches(self):
        self.assertEqual(safe_json({}), self._old_safe_json_frost({}))

    def test_empty_list_matches(self):
        self.assertEqual(safe_json([]), self._old_safe_json_frost([]))


# ---------------------------------------------------------------------------
# TestFrostWriterImport (5)
# ---------------------------------------------------------------------------

class TestFrostWriterImport(unittest.TestCase):
    """
    postgres_frost_writer が base_writer のユーティリティを参照していることを確認。

    Note: frost/__init__.py → frost_runner → postgres_frost_writer の循環 import
    を回避するため、importlib でモジュール単体をロードして確認する。
    """

    def _load_frost_writer(self):
        """循環 import を回避して frost_writer モジュールを取得する。"""
        import importlib.util, sys
        mod_name = "analytics.python.io.postgres_frost_writer"
        if mod_name in sys.modules:
            return sys.modules[mod_name]
        spec = importlib.util.spec_from_file_location(
            mod_name,
            "/home/user/prostock/analytics/python/io/postgres_frost_writer.py",
        )
        mod = importlib.util.module_from_spec(spec)
        # frost_contracts の循環を避けるため sys.modules に先行登録してからロード
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
        except Exception:
            del sys.modules[mod_name]
            raise
        return mod

    def test_module_imports_safe_float(self):
        """_safe_float が base_writer.safe_float と同一オブジェクトである。"""
        # ソースファイルを直接解析して import 文を確認
        import ast
        src = open("/home/user/prostock/analytics/python/io/postgres_frost_writer.py").read()
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.ImportFrom, ast.Import)):
                s = ast.unparse(node)
                if "safe_float" in s and "base_writer" in s:
                    found = True
                    break
        self.assertTrue(found, "postgres_frost_writer が base_writer から safe_float を import していない")

    def test_module_imports_safe_float_opt(self):
        import ast
        src = open("/home/user/prostock/analytics/python/io/postgres_frost_writer.py").read()
        tree = ast.parse(src)
        found = any(
            "safe_float_opt" in ast.unparse(n) and "base_writer" in ast.unparse(n)
            for n in ast.walk(tree)
            if isinstance(n, (ast.ImportFrom, ast.Import))
        )
        self.assertTrue(found)

    def test_module_imports_safe_json(self):
        import ast
        src = open("/home/user/prostock/analytics/python/io/postgres_frost_writer.py").read()
        tree = ast.parse(src)
        found = any(
            "safe_json" in ast.unparse(n) and "base_writer" in ast.unparse(n)
            for n in ast.walk(tree)
            if isinstance(n, (ast.ImportFrom, ast.Import))
        )
        self.assertTrue(found)

    def test_safe_float_nan_zero(self):
        """base_writer.safe_float 自体で NaN→0.0 を確認 (frost_writer 移行後も同じ動作)。"""
        self.assertEqual(safe_float(float("nan")), 0.0)

    def test_safe_json_nan_none(self):
        """base_writer.safe_json で NaN→None を確認。"""
        result = json.loads(safe_json({"x": float("nan")}))
        self.assertIsNone(result["x"])


# ---------------------------------------------------------------------------
# TestEmlAlphaWriterImport (4)
# ---------------------------------------------------------------------------

class TestEmlAlphaWriterImport(unittest.TestCase):
    """
    postgres_eml_alpha_writer が base_writer のユーティリティを参照していることを確認。
    """

    def test_module_imports_safe_float(self):
        import analytics.python.io.postgres_eml_alpha_writer as m
        self.assertIs(m._safe_float, safe_float)

    def test_module_imports_safe_json(self):
        import analytics.python.io.postgres_eml_alpha_writer as m
        self.assertIs(m._safe_json, safe_json)

    def test_safe_float_inf_zero(self):
        import analytics.python.io.postgres_eml_alpha_writer as m
        self.assertEqual(m._safe_float(float("inf")), 0.0)

    def test_safe_json_inf_none(self):
        import analytics.python.io.postgres_eml_alpha_writer as m
        result = json.loads(m._safe_json([float("inf"), 1.0]))
        self.assertIsNone(result[0])


# ---------------------------------------------------------------------------
# TestEmlBacktestWriterImport (4)
# ---------------------------------------------------------------------------

class TestEmlBacktestWriterImport(unittest.TestCase):
    """
    postgres_eml_backtest_writer が base_writer のユーティリティを参照していることを確認。
    eml_backtest の _sf は safe_float_opt (NaN/Inf→None) のエイリアス。
    """

    def test_module_imports_sf_as_safe_float_opt(self):
        import analytics.python.io.postgres_eml_backtest_writer as m
        self.assertIs(m._sf, safe_float_opt)

    def test_module_imports_safe_json(self):
        import analytics.python.io.postgres_eml_backtest_writer as m
        self.assertIs(m._safe_json, safe_json)

    def test_sf_nan_returns_none(self):
        """eml_backtest の旧 _sf は NaN/Inf → None だったことを確認。"""
        import analytics.python.io.postgres_eml_backtest_writer as m
        self.assertIsNone(m._sf(float("nan")))

    def test_sf_inf_returns_none(self):
        import analytics.python.io.postgres_eml_backtest_writer as m
        self.assertIsNone(m._sf(float("inf")))


if __name__ == "__main__":
    unittest.main()
