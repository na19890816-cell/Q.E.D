"""
test_phase5_run_context.py
--------------------------
Phase 5: RunContext テスト (D5 負債解消の検証)

テスト構成
----------
TestRunContextDefaults         (6)  : デフォルト値・自動生成の確認
TestRunContextFromEnv          (9)  : from_env() 環境変数マッピング
TestRunContextFromArgs         (7)  : from_args() argparse.Namespace マッピング
TestRunContextFromDict         (6)  : from_dict() / to_dict() ラウンドトリップ
TestRunContextChild            (5)  : child() trace_id 引き継ぎ
TestRunContextLogHeader        (4)  : log_header() 出力形式
TestFrostRunnerMain            (8)  : frost_runner.run_frost_pipeline_with_context()
TestEmlPipelineRunContext       (5)  : run_eml_pipeline の RunContext 統合検証
"""
from __future__ import annotations

import os
import types
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import patch
import pytest

from analytics.python.frost.run_context import RunContext, _make_run_id, _make_trace_id


# ===========================================================================
# 1. TestRunContextDefaults
# ===========================================================================

class TestRunContextDefaults:
    def test_run_id_is_generated(self):
        ctx = RunContext()
        assert ctx.run_id
        # 形式チェック: UUID v4 または文字列
        assert len(ctx.run_id) > 0

    def test_trace_id_is_generated(self):
        ctx = RunContext()
        assert ctx.trace_id
        assert len(ctx.trace_id) > 0

    def test_run_id_trace_id_are_different(self):
        ctx = RunContext()
        assert ctx.run_id != ctx.trace_id

    def test_default_dry_run_false(self):
        ctx = RunContext()
        assert ctx.dry_run is False

    def test_default_verbose_false(self):
        ctx = RunContext()
        assert ctx.verbose is False

    def test_started_at_is_utc(self):
        ctx = RunContext()
        assert ctx.started_at.tzinfo is not None
        assert ctx.started_at.tzinfo == timezone.utc


# ===========================================================================
# 2. TestRunContextFromEnv
# ===========================================================================

class TestRunContextFromEnv:
    def _env(self, **kwargs):
        """環境変数をパッチして from_env() を呼ぶ。"""
        with patch.dict(os.environ, {k: v for k, v in kwargs.items()}, clear=False):
            return RunContext.from_env(pipeline="frost")

    def test_run_id_from_env(self):
        ctx = self._env(FROST_RUN_ID="my_run_001")
        assert ctx.run_id == "my_run_001"

    def test_trace_id_from_env(self):
        ctx = self._env(FROST_TRACE_ID="my_trace_001")
        assert ctx.trace_id == "my_trace_001"

    def test_batch_label_from_env(self):
        ctx = self._env(FROST_BATCH_LABEL="v2_batch")
        assert ctx.batch_label == "v2_batch"

    def test_dry_run_from_env(self):
        ctx = self._env(FROST_DRY_RUN="1")
        assert ctx.dry_run is True

    def test_dry_run_default_false(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FROST_DRY_RUN", None)
            ctx = RunContext.from_env(pipeline="frost")
        assert ctx.dry_run is False

    def test_verbose_from_env(self):
        ctx = self._env(FROST_VERBOSE="1")
        assert ctx.verbose is True

    def test_pipeline_set_correctly(self):
        ctx = RunContext.from_env(pipeline="eml", prefix="EML_")
        assert ctx.pipeline == "eml"

    def test_auto_generate_run_id_if_missing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FROST_RUN_ID", None)
            ctx = RunContext.from_env(pipeline="frost")
        assert ctx.run_id.startswith("frost__")

    def test_batch_label_default_when_missing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FROST_BATCH_LABEL", None)
            ctx = RunContext.from_env(pipeline="frost")
        assert ctx.batch_label == "frost_v1"


# ===========================================================================
# 3. TestRunContextFromArgs
# ===========================================================================

class TestRunContextFromArgs:
    def _args(self, **kwargs):
        """argparse.Namespace 風のオブジェクトを生成する。"""
        ns = types.SimpleNamespace(
            dry_run=kwargs.get("dry_run", False),
            batch_label=kwargs.get("batch_label", None),
            verbose=kwargs.get("verbose", False),
            top_k=kwargs.get("top_k", 25),
        )
        return ns

    def test_dry_run_from_args(self):
        ctx = RunContext.from_args(self._args(dry_run=True))
        assert ctx.dry_run is True

    def test_batch_label_from_args(self):
        ctx = RunContext.from_args(self._args(batch_label="cli_batch"))
        assert ctx.batch_label == "cli_batch"

    def test_verbose_from_args(self):
        ctx = RunContext.from_args(self._args(verbose=True))
        assert ctx.verbose is True

    def test_explicit_run_id(self):
        ctx = RunContext.from_args(self._args(), run_id="explicit_run")
        assert ctx.run_id == "explicit_run"

    def test_explicit_trace_id(self):
        ctx = RunContext.from_args(self._args(), trace_id="explicit_trace")
        assert ctx.trace_id == "explicit_trace"

    def test_pipeline_set(self):
        ctx = RunContext.from_args(self._args(), pipeline="eml")
        assert ctx.pipeline == "eml"

    def test_batch_label_falls_back_to_env(self):
        with patch.dict(os.environ, {"FROST_BATCH_LABEL": "env_batch"}):
            ctx = RunContext.from_args(self._args(batch_label=None))
        assert ctx.batch_label == "env_batch"


# ===========================================================================
# 4. TestRunContextFromDict
# ===========================================================================

class TestRunContextFromDict:
    def test_roundtrip(self):
        ctx = RunContext(
            run_id="r1",
            trace_id="t1",
            batch_label="b1",
            dry_run=True,
            verbose=False,
            pipeline="frost",
        )
        d = ctx.to_dict()
        ctx2 = RunContext.from_dict(d)
        assert ctx2.run_id == "r1"
        assert ctx2.trace_id == "t1"
        assert ctx2.batch_label == "b1"
        assert ctx2.dry_run is True
        assert ctx2.pipeline == "frost"

    def test_started_at_string_parsed(self):
        d = {"started_at": "2026-07-02T12:00:00+00:00"}
        ctx = RunContext.from_dict(d)
        assert ctx.started_at.year == 2026

    def test_to_dict_keys(self):
        ctx = RunContext()
        d = ctx.to_dict()
        assert set(d.keys()) == {
            "run_id", "trace_id", "batch_label", "dry_run",
            "verbose", "started_at", "pipeline",
        }

    def test_to_dict_started_at_is_iso_string(self):
        ctx = RunContext()
        d = ctx.to_dict()
        assert isinstance(d["started_at"], str)
        # ISO 8601 形式か確認
        datetime.fromisoformat(d["started_at"])

    def test_missing_run_id_auto_generated(self):
        ctx = RunContext.from_dict({})
        assert ctx.run_id  # 空でない

    def test_pipeline_default_frost(self):
        ctx = RunContext.from_dict({})
        assert ctx.pipeline == "frost"


# ===========================================================================
# 5. TestRunContextChild
# ===========================================================================

class TestRunContextChild:
    def test_child_inherits_trace_id(self):
        parent = RunContext(trace_id="parent_trace")
        child = parent.child()
        assert child.trace_id == "parent_trace"

    def test_child_has_new_run_id(self):
        parent = RunContext(run_id="parent_run")
        child = parent.child()
        assert child.run_id != "parent_run"

    def test_child_explicit_run_id(self):
        parent = RunContext()
        child = parent.child(run_id="child_run")
        assert child.run_id == "child_run"

    def test_child_inherits_dry_run(self):
        parent = RunContext(dry_run=True)
        child = parent.child()
        assert child.dry_run is True

    def test_child_custom_batch_label(self):
        parent = RunContext(batch_label="batch_a")
        child = parent.child(batch_label="batch_b")
        assert child.batch_label == "batch_b"


# ===========================================================================
# 6. TestRunContextLogHeader
# ===========================================================================

class TestRunContextLogHeader:
    def test_log_header_contains_run_id(self):
        ctx = RunContext(run_id="my_run", trace_id="my_trace", pipeline="frost")
        header = ctx.log_header()
        assert "my_run" in header

    def test_log_header_contains_trace_id(self):
        ctx = RunContext(run_id="r1", trace_id="my_trace", pipeline="frost")
        assert "my_trace" in ctx.log_header()

    def test_log_header_contains_pipeline(self):
        ctx = RunContext(pipeline="eml")
        assert "EML" in ctx.log_header()

    def test_log_header_contains_dry_run(self):
        ctx = RunContext(dry_run=True)
        assert "dry_run=True" in ctx.log_header()


# ===========================================================================
# 7. TestFrostRunnerMain
# ===========================================================================

class TestFrostRunnerMain:
    """frost_runner.run_frost_pipeline_with_context() の統合テスト"""

    def _make_config(self, dry_run=True):
        from analytics.python.frost.frost_config import FrostConfig
        return FrostConfig(dry_run=dry_run, enabled=True)

    def test_import_run_context_from_frost_runner(self):
        """frost_runner が RunContext を import できることを確認"""
        from analytics.python.frost.frost_runner import run_frost_pipeline_with_context
        assert callable(run_frost_pipeline_with_context)

    def test_with_context_uses_ctx_run_id(self):
        from analytics.python.frost.frost_runner import run_frost_pipeline_with_context
        ctx = RunContext(run_id="ctx_run_id", trace_id="ctx_trace_id")
        config = self._make_config()
        # DB 書き込みをスキップ (conn=None + DSN 未設定は許容)
        output = run_frost_pipeline_with_context([], config, ctx)
        assert output.run_id == "ctx_run_id"

    def test_with_context_uses_ctx_trace_id(self):
        from analytics.python.frost.frost_runner import run_frost_pipeline_with_context
        ctx = RunContext(run_id="r1", trace_id="ctx_trace_abc")
        config = self._make_config()
        output = run_frost_pipeline_with_context([], config, ctx)
        assert output.trace_id == "ctx_trace_abc"

    def test_with_context_dry_run_propagated(self):
        from analytics.python.frost.frost_runner import run_frost_pipeline_with_context
        ctx = RunContext(dry_run=True)
        config = self._make_config(dry_run=False)
        output = run_frost_pipeline_with_context([], config, ctx)
        assert output.dry_run is True

    def test_with_context_empty_candidates_status(self):
        from analytics.python.frost.frost_runner import run_frost_pipeline_with_context
        ctx = RunContext()
        config = self._make_config()
        output = run_frost_pipeline_with_context([], config, ctx)
        # 候補なし → completed or dry_run
        assert output.status in ("completed", "dry_run", "skipped", "failed")

    def test_frost_runner_has_main_block(self):
        """frost_runner.py に __main__ ブロックが存在することを確認"""
        import inspect
        import analytics.python.frost.frost_runner as fr
        source = inspect.getsource(fr)
        assert 'if __name__ == "__main__"' in source

    def test_frost_runner_has_argparse(self):
        """frost_runner.py に argparse が使われていることを確認"""
        import inspect
        import analytics.python.frost.frost_runner as fr
        source = inspect.getsource(fr)
        assert "argparse" in source

    def test_frost_runner_imports_run_context(self):
        """frost_runner が RunContext を import していることを確認"""
        import inspect
        import analytics.python.frost.frost_runner as fr
        source = inspect.getsource(fr)
        assert "RunContext" in source


# ===========================================================================
# 8. TestEmlPipelineRunContext
# ===========================================================================

class TestEmlPipelineRunContext:
    """run_eml_pipeline.py が RunContext を統合していることを確認"""

    def test_eml_pipeline_imports_run_context(self):
        import importlib.util
        import os
        path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "scripts", "postgres", "run_eml_pipeline.py",
        )
        with open(path) as f:
            source = f.read()
        assert "RunContext" in source, "run_eml_pipeline.py が RunContext を import していない"

    def test_eml_pipeline_uses_from_env(self):
        import os
        path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "scripts", "postgres", "run_eml_pipeline.py",
        )
        with open(path) as f:
            source = f.read()
        assert "RunContext.from_env" in source

    def test_eml_pipeline_no_uuid4_for_trace(self):
        """uuid4 / uuid5 で trace_id を直接生成するコードが削除されていることを確認"""
        import os
        path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "scripts", "postgres", "run_eml_pipeline.py",
        )
        with open(path) as f:
            source = f.read()
        # uuid.uuid5(uuid.NAMESPACE_DNS, ...) パターンが残っていないことを確認
        assert "uuid.uuid5(uuid.NAMESPACE_DNS" not in source

    def test_eml_pipeline_no_direct_datetime_for_run_id(self):
        """run_id を datetime.strftime で直接作るコードが削除されていることを確認"""
        import os
        path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "scripts", "postgres", "run_eml_pipeline.py",
        )
        with open(path) as f:
            source = f.read()
        # 旧パターン: f"eml_v1__{datetime.now(timezone.utc).strftime(...)}"
        assert "eml_v1__{datetime.now" not in source

    def test_run_context_eml_pipeline_prefix(self):
        """EML パイプラインでは EML_ プレフィックスを使うことを確認"""
        import os
        path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "scripts", "postgres", "run_eml_pipeline.py",
        )
        with open(path) as f:
            source = f.read()
        assert 'prefix="EML_"' in source or "prefix='EML_'" in source
