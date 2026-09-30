"""
test_phase1_policy_bridge.py — Phase 1: postgres_policy_bridge.py の単体テスト (DB レス)

## テスト設計方針

postgres_policy_bridge.py は psycopg3 の conn オブジェクトに依存しているため、
unittest.mock.MagicMock でカーソルと接続をスタブ化し、DB 接続なしで全ロジックを検証する。

カバー範囲:
  TestUpsertPolicySpec      (10): upsert_policy_spec — dry_run / SQL / hash 返却
  TestLoadPolicySpecByHash  (8):  load_policy_spec_by_hash — ヒット / ミス / JSON/dict 型
  TestTouchPolicyUsedAt     (5):  touch_policy_used_at — dry_run / SQL 実行
  TestSetRunPolicyHash      (5):  set_run_policy_hash — dry_run / SQL 実行
  TestFetchPolicyForRun     (7):  fetch_policy_for_run — NULL / ヒット / 統合
  TestListPolicyHashes      (7):  list_policy_hashes — フィルタなし / engine / phase / limit
  TestBridgeIntegration     (6):  upsert → load ラウンドトリップ (mock)

合計: 48 テスト
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from analytics.python.frost.policy_spec import PolicySpec
from analytics.python.pg_io.postgres_policy_bridge import (
    fetch_policy_for_run,
    list_policy_hashes,
    load_policy_spec_by_hash,
    set_run_policy_hash,
    touch_policy_used_at,
    upsert_policy_spec,
)


# ===========================================================================
# ヘルパー: モック conn / cursor ファクトリ
# ===========================================================================

def _make_conn(fetchone_return=None, fetchall_return=None):
    """
    psycopg3 の Connection + cursor をモックする。
    cursor は context manager として __enter__ / __exit__ を実装する。
    """
    cur = MagicMock()
    cur.fetchone.return_value = fetchone_return
    cur.fetchall.return_value = fetchall_return if fetchall_return is not None else []

    # cursor() は context manager を返す
    ctx_cur = MagicMock()
    ctx_cur.__enter__ = MagicMock(return_value=cur)
    ctx_cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = ctx_cur

    return conn, cur


def _default_spec(**kwargs) -> PolicySpec:
    return PolicySpec(**kwargs)


def _spec_json(spec: PolicySpec) -> str:
    return json.dumps(spec.to_dict(), sort_keys=True, ensure_ascii=True)


# ===========================================================================
# TestUpsertPolicySpec
# ===========================================================================

@pytest.mark.phase1_bridge
class TestUpsertPolicySpec:
    """upsert_policy_spec() のロジックを検証する。"""

    def test_dry_run_returns_policy_hash_without_db(self):
        """dry_run=True のとき DB 操作なしで policy_hash を返す"""
        conn, cur = _make_conn()
        spec = _default_spec()
        result = upsert_policy_spec(conn, spec, dry_run=True)
        assert result == spec.policy_hash
        cur.execute.assert_not_called()

    def test_dry_run_true_no_cursor_used(self):
        """dry_run=True のとき cursor が一切開かれない"""
        conn, _ = _make_conn()
        spec = _default_spec()
        upsert_policy_spec(conn, spec, dry_run=True)
        conn.cursor.assert_not_called()

    def test_returns_policy_hash_on_success(self):
        """dry_run=False のとき policy_hash を返す"""
        conn, cur = _make_conn()
        spec = _default_spec()
        result = upsert_policy_spec(conn, spec, dry_run=False)
        assert result == spec.policy_hash

    def test_hash_format_sha256_prefix(self):
        """返却される policy_hash が sha256: プレフィックスを持つ"""
        conn, cur = _make_conn()
        spec = _default_spec()
        result = upsert_policy_spec(conn, spec, dry_run=False)
        assert result.startswith("sha256:")
        assert len(result) == 71

    def test_execute_called_once(self):
        """dry_run=False のとき execute が 1 回呼ばれる"""
        conn, cur = _make_conn()
        spec = _default_spec()
        upsert_policy_spec(conn, spec, dry_run=False)
        cur.execute.assert_called_once()

    def test_sql_contains_insert(self):
        """execute に渡される SQL に INSERT が含まれる"""
        conn, cur = _make_conn()
        spec = _default_spec()
        upsert_policy_spec(conn, spec, dry_run=False)
        sql_arg = cur.execute.call_args[0][0]
        assert "INSERT" in sql_arg.upper()

    def test_sql_contains_on_conflict(self):
        """ON CONFLICT (upsert) が SQL に含まれる"""
        conn, cur = _make_conn()
        spec = _default_spec()
        upsert_policy_spec(conn, spec, dry_run=False)
        sql_arg = cur.execute.call_args[0][0]
        assert "ON CONFLICT" in sql_arg.upper()

    def test_params_contain_policy_hash(self):
        """execute のパラメータに policy_hash が含まれる"""
        conn, cur = _make_conn()
        spec = _default_spec()
        upsert_policy_spec(conn, spec, dry_run=False)
        params = cur.execute.call_args[0][1]
        assert spec.policy_hash in params

    def test_params_contain_engine_version(self):
        """execute のパラメータに engine_version が含まれる"""
        conn, cur = _make_conn()
        spec = _default_spec(engine_version="frost_v2")
        upsert_policy_spec(conn, spec, dry_run=False)
        params = cur.execute.call_args[0][1]
        assert "frost_v2" in params

    def test_different_specs_return_different_hashes(self):
        """異なる PolicySpec が異なる policy_hash を返す"""
        conn1, _ = _make_conn()
        conn2, _ = _make_conn()
        spec1 = _default_spec(w_predictive=0.20)
        spec2 = _default_spec(w_predictive=0.30)
        h1 = upsert_policy_spec(conn1, spec1, dry_run=True)
        h2 = upsert_policy_spec(conn2, spec2, dry_run=True)
        assert h1 != h2


# ===========================================================================
# TestLoadPolicySpecByHash
# ===========================================================================

@pytest.mark.phase1_bridge
class TestLoadPolicySpecByHash:
    """load_policy_spec_by_hash() のロジックを検証する。"""

    def _make_conn_with_spec(self, spec: PolicySpec):
        """spec の spec_json を返す mock conn を作成する"""
        spec_json_str = _spec_json(spec)
        conn, cur = _make_conn(fetchone_return=(spec_json_str,))
        return conn, cur

    def test_returns_none_when_not_found(self):
        """DB にレコードがない場合 None を返す"""
        conn, cur = _make_conn(fetchone_return=None)
        result = load_policy_spec_by_hash(conn, "sha256:abc123")
        assert result is None

    def test_returns_policy_spec_on_hit(self):
        """DB にレコードがある場合 PolicySpec を返す"""
        spec = _default_spec()
        conn, _ = self._make_conn_with_spec(spec)
        result = load_policy_spec_by_hash(conn, spec.policy_hash)
        assert isinstance(result, PolicySpec)

    def test_hash_preserved_on_load(self):
        """ロードした PolicySpec のハッシュが元と一致する"""
        spec = _default_spec(w_predictive=0.30)
        conn, _ = self._make_conn_with_spec(spec)
        result = load_policy_spec_by_hash(conn, spec.policy_hash)
        assert result is not None
        assert result.policy_hash == spec.policy_hash

    def test_weights_preserved_on_load(self):
        """ロードした PolicySpec の重みが元と一致する"""
        spec = _default_spec(w_predictive=0.35, w_oos_sharpe=0.20)
        conn, _ = self._make_conn_with_spec(spec)
        result = load_policy_spec_by_hash(conn, spec.policy_hash)
        assert result is not None
        assert result.w_predictive == 0.35
        assert result.w_oos_sharpe == 0.20

    def test_accepts_dict_spec_json(self):
        """spec_json が dict 型（psycopg の jsonb 自動変換）でも動作する"""
        spec = _default_spec()
        spec_dict = spec.to_dict()
        conn, cur = _make_conn(fetchone_return=(spec_dict,))
        result = load_policy_spec_by_hash(conn, spec.policy_hash)
        assert isinstance(result, PolicySpec)
        assert result.policy_hash == spec.policy_hash

    def test_execute_called_with_hash(self):
        """execute に policy_hash がパラメータとして渡される"""
        hash_val = "sha256:0" * 7 + "a" * 10  # ダミー
        conn, cur = _make_conn(fetchone_return=None)
        load_policy_spec_by_hash(conn, hash_val)
        params = cur.execute.call_args[0][1]
        assert hash_val in params

    def test_sql_selects_spec_json(self):
        """execute に渡される SQL に spec_json が含まれる"""
        conn, cur = _make_conn(fetchone_return=None)
        load_policy_spec_by_hash(conn, "sha256:dummy")
        sql_arg = cur.execute.call_args[0][0]
        assert "spec_json" in sql_arg.lower()

    def test_gates_preserved_on_load(self):
        """ロードした PolicySpec の hard gates が元と一致する"""
        spec = _default_spec(pbo_threshold=0.15, max_drawdown=0.25)
        conn, _ = self._make_conn_with_spec(spec)
        result = load_policy_spec_by_hash(conn, spec.policy_hash)
        assert result is not None
        assert result.pbo_threshold == 0.15
        assert result.max_drawdown == 0.25


# ===========================================================================
# TestTouchPolicyUsedAt
# ===========================================================================

@pytest.mark.phase1_bridge
class TestTouchPolicyUsedAt:
    """touch_policy_used_at() のロジックを検証する。"""

    def test_dry_run_no_execute(self):
        """dry_run=True のとき execute が呼ばれない"""
        conn, cur = _make_conn()
        touch_policy_used_at(conn, "sha256:abc", dry_run=True)
        cur.execute.assert_not_called()

    def test_dry_run_no_cursor(self):
        """dry_run=True のとき cursor が開かれない"""
        conn, _ = _make_conn()
        touch_policy_used_at(conn, "sha256:abc", dry_run=True)
        conn.cursor.assert_not_called()

    def test_execute_called_once_when_not_dry_run(self):
        """dry_run=False のとき execute が 1 回呼ばれる"""
        conn, cur = _make_conn()
        touch_policy_used_at(conn, "sha256:abc", dry_run=False)
        cur.execute.assert_called_once()

    def test_sql_updates_used_at(self):
        """UPDATE used_at が SQL に含まれる"""
        conn, cur = _make_conn()
        touch_policy_used_at(conn, "sha256:abc", dry_run=False)
        sql_arg = cur.execute.call_args[0][0]
        assert "used_at" in sql_arg.lower()
        assert "UPDATE" in sql_arg.upper()

    def test_params_contain_policy_hash(self):
        """execute のパラメータに policy_hash が含まれる"""
        conn, cur = _make_conn()
        h = "sha256:test_hash"
        touch_policy_used_at(conn, h, dry_run=False)
        params = cur.execute.call_args[0][1]
        assert h in params


# ===========================================================================
# TestSetRunPolicyHash
# ===========================================================================

@pytest.mark.phase1_bridge
class TestSetRunPolicyHash:
    """set_run_policy_hash() のロジックを検証する。"""

    def test_dry_run_no_execute(self):
        """dry_run=True のとき execute が呼ばれない"""
        conn, cur = _make_conn()
        set_run_policy_hash(conn, "run-001", "sha256:abc", dry_run=True)
        cur.execute.assert_not_called()

    def test_dry_run_no_cursor(self):
        """dry_run=True のとき cursor が開かれない"""
        conn, _ = _make_conn()
        set_run_policy_hash(conn, "run-001", "sha256:abc", dry_run=True)
        conn.cursor.assert_not_called()

    def test_execute_called_once_when_not_dry_run(self):
        """dry_run=False のとき execute が 1 回呼ばれる"""
        conn, cur = _make_conn()
        set_run_policy_hash(conn, "run-001", "sha256:abc", dry_run=False)
        cur.execute.assert_called_once()

    def test_sql_updates_frost_runs(self):
        """frost_runs テーブルへの UPDATE が SQL に含まれる"""
        conn, cur = _make_conn()
        set_run_policy_hash(conn, "run-001", "sha256:abc", dry_run=False)
        sql_arg = cur.execute.call_args[0][0]
        assert "frost_runs" in sql_arg.lower()
        assert "UPDATE" in sql_arg.upper()
        assert "policy_hash" in sql_arg.lower()

    def test_params_contain_both_hash_and_run_id(self):
        """execute のパラメータに policy_hash と run_id が含まれる"""
        conn, cur = _make_conn()
        h = "sha256:myhash"
        run_id = "run-xyz-001"
        set_run_policy_hash(conn, run_id, h, dry_run=False)
        params = cur.execute.call_args[0][1]
        assert h in params
        assert run_id in params


# ===========================================================================
# TestFetchPolicyForRun
# ===========================================================================

@pytest.mark.phase1_bridge
class TestFetchPolicyForRun:
    """fetch_policy_for_run() のロジックを検証する。"""

    def test_returns_none_when_run_not_found(self):
        """run_id が存在しない場合 None を返す"""
        conn, cur = _make_conn(fetchone_return=None)
        result = fetch_policy_for_run(conn, "nonexistent-run")
        assert result is None

    def test_returns_none_when_policy_hash_is_null(self):
        """frost_runs.policy_hash が NULL の場合 None を返す"""
        conn, cur = _make_conn(fetchone_return=(None,))
        result = fetch_policy_for_run(conn, "run-001")
        assert result is None

    def test_executes_query_with_run_id(self):
        """execute に run_id がパラメータとして渡される"""
        conn, cur = _make_conn(fetchone_return=None)
        fetch_policy_for_run(conn, "run-001")
        params = cur.execute.call_args[0][1]
        assert "run-001" in params

    def test_sql_queries_frost_runs(self):
        """frost_runs テーブルへのクエリが SQL に含まれる"""
        conn, cur = _make_conn(fetchone_return=None)
        fetch_policy_for_run(conn, "run-001")
        sql_arg = cur.execute.call_args[0][0]
        assert "frost_runs" in sql_arg.lower()

    def test_sql_selects_policy_hash(self):
        """SELECT に policy_hash が含まれる"""
        conn, cur = _make_conn(fetchone_return=None)
        fetch_policy_for_run(conn, "run-001")
        sql_arg = cur.execute.call_args[0][0]
        assert "policy_hash" in sql_arg.lower()

    def test_returns_policy_spec_when_found(self):
        """run が見つかり policy_hash も存在する場合 PolicySpec を返す"""
        spec = _default_spec(w_predictive=0.30)
        spec_json_str = _spec_json(spec)

        # 1 回目 (frost_runs から policy_hash 取得) → hash を返す
        # 2 回目 (qed_policies から spec_json 取得)  → spec_json を返す
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (spec.policy_hash,),  # frost_runs クエリ
            (spec_json_str,),     # qed_policies クエリ
        ]
        ctx_cur = MagicMock()
        ctx_cur.__enter__ = MagicMock(return_value=cur)
        ctx_cur.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor.return_value = ctx_cur

        result = fetch_policy_for_run(conn, "run-001")
        assert isinstance(result, PolicySpec)
        assert result.policy_hash == spec.policy_hash

    def test_hash_preserved_through_run_fetch(self):
        """fetch_policy_for_run で取得した PolicySpec のハッシュが元と一致する"""
        spec = _default_spec(pbo_threshold=0.15)
        spec_json_str = _spec_json(spec)

        cur = MagicMock()
        cur.fetchone.side_effect = [
            (spec.policy_hash,),
            (spec_json_str,),
        ]
        ctx_cur = MagicMock()
        ctx_cur.__enter__ = MagicMock(return_value=cur)
        ctx_cur.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor.return_value = ctx_cur

        result = fetch_policy_for_run(conn, "run-gate-check")
        assert result is not None
        assert result.pbo_threshold == 0.15


# ===========================================================================
# TestListPolicyHashes
# ===========================================================================

@pytest.mark.phase1_bridge
class TestListPolicyHashes:
    """list_policy_hashes() のロジックを検証する。"""

    def _rows(self, n: int = 2):
        """ダミーの fetchall 結果"""
        return [
            (f"sha256:{'a' * 64}", "frost_v1", "phase1", f"desc_{i}", None, None)
            for i in range(n)
        ]

    def test_returns_list(self):
        """結果がリストである"""
        conn, cur = _make_conn(fetchall_return=self._rows(2))
        result = list_policy_hashes(conn)
        assert isinstance(result, list)

    def test_returns_correct_count(self):
        """DB の返却行数と結果の件数が一致する"""
        conn, cur = _make_conn(fetchall_return=self._rows(3))
        result = list_policy_hashes(conn)
        assert len(result) == 3

    def test_result_keys(self):
        """各要素が必要なキーを持つ"""
        conn, cur = _make_conn(fetchall_return=self._rows(1))
        result = list_policy_hashes(conn)
        keys = set(result[0].keys())
        assert {"policy_hash", "engine_version", "phase_tag",
                "description", "first_seen_at", "used_at"} <= keys

    def test_empty_result(self):
        """結果が 0 件でも空リストを返す"""
        conn, cur = _make_conn(fetchall_return=[])
        result = list_policy_hashes(conn)
        assert result == []

    def test_engine_version_filter_adds_where(self):
        """engine_version フィルタを渡すと WHERE 句が SQL に含まれる"""
        conn, cur = _make_conn(fetchall_return=[])
        list_policy_hashes(conn, engine_version="frost_v2")
        sql_arg = cur.execute.call_args[0][0]
        assert "engine_version" in sql_arg.lower()
        assert "WHERE" in sql_arg.upper()

    def test_phase_tag_filter_adds_where(self):
        """phase_tag フィルタを渡すと WHERE 句が SQL に含まれる"""
        conn, cur = _make_conn(fetchall_return=[])
        list_policy_hashes(conn, phase_tag="phase2")
        sql_arg = cur.execute.call_args[0][0]
        assert "phase_tag" in sql_arg.lower()

    def test_limit_included_in_params(self):
        """limit 値が execute のパラメータに含まれる"""
        conn, cur = _make_conn(fetchall_return=[])
        list_policy_hashes(conn, limit=50)
        params = cur.execute.call_args[0][1]
        assert 50 in params


# ===========================================================================
# TestBridgeIntegration
# ===========================================================================

@pytest.mark.phase1_bridge
class TestBridgeIntegration:
    """
    upsert → load → touch のシーケンスを mock でシミュレートする。
    実 DB なしで「upsert して load したら元の PolicySpec に戻る」ことを証明する。
    """

    def test_upsert_then_load_roundtrip(self):
        """upsert の後に load すると同一 PolicySpec を取得できる"""
        spec = _default_spec(w_predictive=0.35, pbo_threshold=0.15)

        # upsert: write
        conn_w, _ = _make_conn()
        returned_hash = upsert_policy_spec(conn_w, spec, dry_run=False)
        assert returned_hash == spec.policy_hash

        # load: read
        spec_json_str = _spec_json(spec)
        conn_r, _ = _make_conn(fetchone_return=(spec_json_str,))
        loaded = load_policy_spec_by_hash(conn_r, returned_hash)

        assert loaded is not None
        assert loaded.policy_hash == spec.policy_hash
        assert loaded.w_predictive == 0.35
        assert loaded.pbo_threshold == 0.15

    def test_dry_run_upsert_then_load(self):
        """dry_run=True の upsert で得た hash でも load が成功する"""
        spec = _default_spec()
        conn_w, _ = _make_conn()
        h = upsert_policy_spec(conn_w, spec, dry_run=True)
        # h は有効な policy_hash
        assert h == spec.policy_hash

        spec_json_str = _spec_json(spec)
        conn_r, _ = _make_conn(fetchone_return=(spec_json_str,))
        loaded = load_policy_spec_by_hash(conn_r, h)
        assert loaded is not None
        assert loaded.policy_hash == h

    def test_set_run_then_fetch_roundtrip(self):
        """set_run_policy_hash → fetch_policy_for_run のシーケンスが正しく動く"""
        spec = _default_spec(w_oos_sharpe=0.20)
        spec_json_str = _spec_json(spec)
        run_id = "run-integration-001"

        # set_run_policy_hash
        conn_set, _ = _make_conn()
        set_run_policy_hash(conn_set, run_id, spec.policy_hash, dry_run=False)

        # fetch_policy_for_run (2 クエリ)
        cur = MagicMock()
        cur.fetchone.side_effect = [
            (spec.policy_hash,),
            (spec_json_str,),
        ]
        ctx_cur = MagicMock()
        ctx_cur.__enter__ = MagicMock(return_value=cur)
        ctx_cur.__exit__ = MagicMock(return_value=False)
        conn_fetch = MagicMock()
        conn_fetch.cursor.return_value = ctx_cur

        fetched = fetch_policy_for_run(conn_fetch, run_id)
        assert fetched is not None
        assert fetched.w_oos_sharpe == 0.20

    def test_multiple_specs_independent_hashes(self):
        """異なる PolicySpec は独立した policy_hash を持ち、混在しない"""
        specs = [
            _default_spec(w_predictive=0.20 + i * 0.05)
            for i in range(4)
        ]
        hashes = [upsert_policy_spec(_make_conn()[0], s, dry_run=True) for s in specs]
        assert len(set(hashes)) == 4, "異なる PolicySpec が同一ハッシュになった"

    def test_same_spec_always_same_hash(self):
        """同一パラメータの PolicySpec は何度 upsert しても同一 hash"""
        spec = _default_spec(w_predictive=0.25)
        hashes = [
            upsert_policy_spec(_make_conn()[0], spec, dry_run=True)
            for _ in range(10)
        ]
        assert len(set(hashes)) == 1, "同一 PolicySpec のハッシュが不安定"

    def test_touch_after_load_no_error(self):
        """load した PolicySpec の hash で touch を呼んでも例外が起きない"""
        spec = _default_spec()
        spec_json_str = _spec_json(spec)
        conn_r, _ = _make_conn(fetchone_return=(spec_json_str,))
        loaded = load_policy_spec_by_hash(conn_r, spec.policy_hash)
        assert loaded is not None

        conn_t, cur_t = _make_conn()
        touch_policy_used_at(conn_t, loaded.policy_hash, dry_run=False)
        cur_t.execute.assert_called_once()
