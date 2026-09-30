"""
test_golden_determinism.py — Phase 0 決定論性ロジックテスト (DB レス)

## 設計方針

golden-determinism の本体は「同一入力を 2 回スナップショットしたとき、
diff_tables が差分ゼロを返すか」という性質の検証である。

実 DB なしで決定論性を証明するため、`snapshot()` を差し替え可能な構造にし、
以下の層を分離してテストする:

  Layer A — _normalize の決定論性
      同一値を 2 回 normalize した結果が常に等しい
      浮動小数点の ROUND_DECIMALS 丸めが安定している

  Layer B — snapshot-to-snapshot の一致
      mock_snapshot を 2 回呼んで diff_tables が空を返すことを確認
      これが「golden-determinism の核心」

  Layer C — 非決定論性源の排除確認
      揮発列(timestamp/uuid/id 系)が _is_volatile で除外されること
      EML_SEED 固定で rng_seed が固定されること
      FROST_PBO_PARALLEL_ENABLED=0 の環境変数がテスト設定に存在すること

  Layer D — baseline の書き込み・読み込みのラウンドトリップ
      baseline.json を tmpdir に書き込んで読み込み、内容が等しいこと
      diff_tables(base, loaded) が空になること

  Layer E — 非決定論性が注入されたとき FAIL すること
      diff_tables が差分を検出できること（false negative なし）
"""
from __future__ import annotations

import json
import os
import sys
import math
from pathlib import Path

import pytest

# scripts/golden を import パスに追加
SCRIPTS_GOLDEN = Path(__file__).parents[2] / "scripts" / "golden"
sys.path.insert(0, str(SCRIPTS_GOLDEN))

import golden_check as gc


# ===========================================================================
# テスト用スナップショット生成ヘルパー
# ===========================================================================

def _make_snapshot(tables: dict[str, list[dict]]) -> dict:
    """
    テスト用スナップショット構造を生成する。
    golden_check.snapshot() の返却形式に準拠。
    """
    result = {}
    for tbl, rows in tables.items():
        if not rows:
            cols = []
        else:
            cols = list(rows[0].keys())
        # _normalize を適用（本番 snapshot() と同じ前処理）
        normalized_rows = [
            {c: gc._normalize(v) for c, v in row.items()}
            for row in rows
        ]
        # 正準ソート（本番 snapshot() と同じ順序保証）
        normalized_rows.sort(
            key=lambda r: json.dumps(r, sort_keys=True, ensure_ascii=False)
        )
        result[tbl] = {
            "columns": cols,
            "row_count": len(normalized_rows),
            "rows": normalized_rows,
        }
    return result


def _write_baseline(snap: dict, path: Path) -> None:
    """snapshot を baseline.json として保存（本番 golden_check.py と同じ形式）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(snap, sort_keys=True, ensure_ascii=False, indent=1)
    )


def _load_baseline(path: Path) -> dict:
    return json.loads(path.read_text())


# ===========================================================================
# Layer A — _normalize の決定論性
# ===========================================================================

class TestNormalizeDeterminism:
    """同一値を 2 回 normalize した結果が常に等しいことを保証する。"""

    def test_float_idempotent(self):
        """normalize → normalize の冪等性"""
        v = 1.23456789012345678
        first = gc._normalize(v)
        second = gc._normalize(v)
        assert first == second

    def test_float_round_stable_across_calls(self):
        """10 桁丸めが繰り返し呼び出しで安定していること"""
        v = math.pi
        results = [gc._normalize(v) for _ in range(100)]
        assert len(set(results)) == 1, "浮動小数点丸め結果が呼び出し間でブレている"

    def test_nan_deterministic(self):
        results = [gc._normalize(float("nan")) for _ in range(10)]
        assert all(r == "NaN" for r in results)

    def test_dict_key_order_deterministic(self):
        """辞書は毎回同じキー順で正規化される"""
        d = {"z": 1.0, "a": 2.0, "m": 3.0}
        r1 = gc._normalize(d)
        r2 = gc._normalize(d)
        assert list(r1.keys()) == list(r2.keys()) == ["a", "m", "z"]

    def test_nested_dict_deterministic(self):
        d = {"outer": {"z": 1.0, "a": 2.0}}
        r1 = gc._normalize(d)
        r2 = gc._normalize(d)
        assert r1 == r2

    def test_list_deterministic(self):
        lst = [3.14159, None, "hello", 42, float("nan")]
        r1 = gc._normalize(lst)
        r2 = gc._normalize(lst)
        assert r1 == r2

    def test_none_deterministic(self):
        assert gc._normalize(None) == gc._normalize(None)

    def test_string_deterministic(self):
        s = "SELECTED"
        assert gc._normalize(s) == gc._normalize(s)

    def test_int_deterministic(self):
        assert gc._normalize(42) == gc._normalize(42)

    def test_round_decimals_matches_env(self):
        """GOLDEN_ROUND_DECIMALS のデフォルトが 10 であること"""
        assert gc.ROUND_DECIMALS == int(os.environ.get("GOLDEN_ROUND_DECIMALS", "10"))


# ===========================================================================
# Layer B — snapshot-to-snapshot の決定論性（golden-determinism の核心）
# ===========================================================================

class TestSnapshotDeterminism:
    """
    同一データを 2 回スナップショットして diff_tables が空になることを確認する。
    これが golden-determinism の証明の核心。
    実 DB なしで、_make_snapshot を用いてロジックを検証する。
    """

    FROST_EVAL_ROWS = [
        {"formula": "rank(volume) * momentum(close, 5)", "frost_score": 0.7432, "decision": "SELECTED"},
        {"formula": "alpha101(open, close)", "frost_score": 0.5821, "decision": "HOLD"},
        {"formula": "ts_rank(adv20, 5)", "frost_score": 0.3299, "decision": "REJECTED"},
    ]
    CAUSAL_ROWS = [
        {"formula": "rank(volume)", "coeff_stability": 0.8812, "causal_pass": True},
        {"formula": "momentum(close, 5)", "coeff_stability": 0.7001, "causal_pass": True},
    ]

    def _double_snapshot(self) -> tuple[dict, dict]:
        """同じデータで 2 回スナップショットを生成する（mock 版 golden-determinism）。"""
        snap1 = _make_snapshot({
            "frost_evaluations": self.FROST_EVAL_ROWS,
            "causal_invariance_results": self.CAUSAL_ROWS,
        })
        snap2 = _make_snapshot({
            "frost_evaluations": self.FROST_EVAL_ROWS,
            "causal_invariance_results": self.CAUSAL_ROWS,
        })
        return snap1, snap2

    def test_double_snapshot_no_diff(self):
        """同一データの 2 回スナップショットが完全一致する（golden-determinism 核心）"""
        snap1, snap2 = self._double_snapshot()
        problems = gc.diff_tables(snap1, snap2)
        assert problems == [], f"差分が検出された（非決定論的）:\n" + "\n".join(problems)

    def test_double_snapshot_json_identical(self):
        """JSON 直列化レベルでも完全一致する"""
        snap1, snap2 = self._double_snapshot()
        j1 = json.dumps(snap1, sort_keys=True, ensure_ascii=False)
        j2 = json.dumps(snap2, sort_keys=True, ensure_ascii=False)
        assert j1 == j2

    def test_row_order_independence(self):
        """行の挿入順序が異なっても正準ソートで一致する"""
        rows_forward = [
            {"formula": "a", "score": 0.9},
            {"formula": "b", "score": 0.5},
            {"formula": "c", "score": 0.1},
        ]
        rows_reversed = list(reversed(rows_forward))
        snap1 = _make_snapshot({"t": rows_forward})
        snap2 = _make_snapshot({"t": rows_reversed})
        problems = gc.diff_tables(snap1, snap2)
        assert problems == [], "行順が違うだけで差分が出た（正準ソート不足）"

    def test_float_precision_stable_across_snapshots(self):
        """浮動小数点が ROUND_DECIMALS 桁に丸められ、2 回のスナップで一致する"""
        rows = [{"score": 0.123456789012345678901}]
        snap1 = _make_snapshot({"t": rows})
        snap2 = _make_snapshot({"t": rows})
        problems = gc.diff_tables(snap1, snap2)
        assert problems == []

    def test_empty_table_deterministic(self):
        """空テーブルのスナップショットが一致する"""
        snap1 = _make_snapshot({"empty_tbl": []})
        snap2 = _make_snapshot({"empty_tbl": []})
        # 空テーブルは columns が [] なのでスキップ扱い（diff なし）
        problems = gc.diff_tables(snap1, snap2)
        assert problems == []

    def test_multiple_tables_deterministic(self):
        """複数テーブルを含むスナップショットが一致する"""
        tables = {
            "frost_evaluations": self.FROST_EVAL_ROWS,
            "causal_invariance_results": self.CAUSAL_ROWS,
            "knowledge_artifacts": [
                {"artifact_type": "alpha", "artifact_hash": "abc123", "status": "active"},
            ],
        }
        snap1 = _make_snapshot(tables)
        snap2 = _make_snapshot(tables)
        problems = gc.diff_tables(snap1, snap2)
        assert problems == []

    def test_nan_values_deterministic(self):
        """NaN を含む行のスナップショットが一致する"""
        rows = [{"score": float("nan"), "label": "unstable"}]
        snap1 = _make_snapshot({"t": rows})
        snap2 = _make_snapshot({"t": rows})
        problems = gc.diff_tables(snap1, snap2)
        assert problems == []

    def test_unicode_values_deterministic(self):
        """日本語・Unicode 値を含む行のスナップショットが一致する"""
        rows = [{"formula": "モメンタム戦略", "decision": "保留中"}]
        snap1 = _make_snapshot({"t": rows})
        snap2 = _make_snapshot({"t": rows})
        problems = gc.diff_tables(snap1, snap2)
        assert problems == []


# ===========================================================================
# Layer C — 非決定論性源の排除確認
# ===========================================================================

class TestNonDeterminismSources:
    """
    golden run が非決定論性を生む可能性のある源泉が
    適切に排除されていることを確認する。
    """

    # --- 揮発列の除外 ---

    def test_volatile_columns_excluded_from_snapshot(self):
        """揮発列を持つ行でも、揮発列を除いた安定列だけで比較される"""
        rows_run1 = [
            {
                "candidate_hash": "abc123",   # 安定
                "frost_score": 0.75,           # 安定
                "created_at": "2026-01-01",    # 揮発 → 除外される
                "run_id": "run-001",           # 揮発 → 除外される
                "decision": "SELECTED",        # 安定
            }
        ]
        rows_run2 = [
            {
                "candidate_hash": "abc123",
                "frost_score": 0.75,
                "created_at": "2026-06-13",    # 別のタイムスタンプ
                "run_id": "run-002",           # 別の run_id
                "decision": "SELECTED",
            }
        ]
        # 揮発列を手動除去してから snapshot を作成（_is_volatile の役割を再現）
        def strip_volatile(rows):
            return [
                {k: v for k, v in row.items() if not gc._is_volatile(k)}
                for row in rows
            ]
        snap1 = _make_snapshot({"t": strip_volatile(rows_run1)})
        snap2 = _make_snapshot({"t": strip_volatile(rows_run2)})
        problems = gc.diff_tables(snap1, snap2)
        assert problems == [], "揮発列除去後は一致するはずが差分が出た"

    def test_all_volatile_patterns_covered(self):
        """golden_check.VOLATILE_PATTERNS が全ての非決定論的列パターンを網羅"""
        volatile_cols = [
            "created_at", "updated_at", "evaluated_at", "promoted_at",
            "id", "run_id", "candidate_id", "backtest_run_id", "fold_id",
            "trace_id", "some_uuid_field", "event_timestamp",
        ]
        for col in volatile_cols:
            assert gc._is_volatile(col), \
                f"列 '{col}' が揮発列として認識されていない"

    def test_content_hashes_not_volatile(self):
        """内容ハッシュ（candidate_hash / spec_hash）は安定列として保持される"""
        stable_cols = ["candidate_hash", "spec_hash", "formula_hash"]
        for col in stable_cols:
            assert not gc._is_volatile(col), \
                f"列 '{col}' が誤って揮発列と判定された（内容ハッシュは安定）"

    # --- EML_SEED 固定 ---

    def test_eml_seed_type_when_set(self, monkeypatch):
        """EML_SEED=42 → rng_seed が int の 42 になること"""
        monkeypatch.setenv("EML_SEED", "42")
        import importlib
        import alpha.eml.eml_master_formula as mod
        importlib.reload(mod)
        cfg = mod.EMLDiscoveryConfig()
        assert cfg.rng_seed == 42
        assert isinstance(cfg.rng_seed, int)

    def test_eml_seed_zero_valid(self, monkeypatch):
        """EML_SEED=0 も有効な seed として扱われること"""
        monkeypatch.setenv("EML_SEED", "0")
        import importlib
        import alpha.eml.eml_master_formula as mod
        importlib.reload(mod)
        cfg = mod.EMLDiscoveryConfig()
        assert cfg.rng_seed == 0

    # --- FROST_PBO_PARALLEL_ENABLED=0 ---

    def test_golden_env_disables_parallel(self):
        """Makefile.golden の GOLDEN_ENV に FROST_PBO_PARALLEL_ENABLED=0 が含まれること"""
        mf = Path(__file__).parents[2] / "Makefile.golden"
        content = mf.read_text()
        assert "FROST_PBO_PARALLEL_ENABLED=0" in content, \
            "GOLDEN_ENV に FROST_PBO_PARALLEL_ENABLED=0 が含まれない（並列が非決定論性源になる）"

    def test_golden_env_sets_eml_seed(self):
        """Makefile.golden の GOLDEN_ENV に EML_SEED=42 が含まれること"""
        mf = Path(__file__).parents[2] / "Makefile.golden"
        content = mf.read_text()
        assert "EML_SEED=42" in content, \
            "GOLDEN_ENV に EML_SEED=42 が含まれない（ランダム性が非決定論性源になる）"

    def test_golden_env_sets_round_decimals(self):
        """Makefile.golden の GOLDEN_ENV に GOLDEN_ROUND_DECIMALS が含まれること"""
        mf = Path(__file__).parents[2] / "Makefile.golden"
        content = mf.read_text()
        assert "GOLDEN_ROUND_DECIMALS" in content, \
            "GOLDEN_ENV に GOLDEN_ROUND_DECIMALS が含まれない（丸め桁数が不定になる）"


# ===========================================================================
# Layer D — baseline ラウンドトリップ
# ===========================================================================

class TestBaselineRoundtrip:
    """
    baseline.json の書き込み → 読み込み → diff_tables がゼロになること。
    ファイル I/O が決定論性を破壊していないことを保証する。
    """

    BASE_TABLES = {
        "frost_evaluations": [
            {"formula": "rank(volume)", "frost_score": 0.82, "decision": "SELECTED"},
            {"formula": "ts_mean(returns, 10)", "frost_score": 0.61, "decision": "HOLD"},
        ],
        "causal_invariance_results": [
            {"formula": "rank(volume)", "coeff_stability": 0.91, "causal_pass": True},
        ],
    }

    def test_write_then_read_no_diff(self, tmp_path):
        """書き込んで読み込んだ baseline が元スナップショットと一致する"""
        baseline_path = tmp_path / "baseline.json"
        snap = _make_snapshot(self.BASE_TABLES)
        _write_baseline(snap, baseline_path)
        loaded = _load_baseline(baseline_path)
        problems = gc.diff_tables(snap, loaded)
        assert problems == [], "baseline ラウンドトリップで差分が発生した"

    def test_baseline_json_is_deterministic(self, tmp_path):
        """同じスナップショットを 2 回書き込んでもバイト列が同一"""
        p1 = tmp_path / "b1.json"
        p2 = tmp_path / "b2.json"
        snap = _make_snapshot(self.BASE_TABLES)
        _write_baseline(snap, p1)
        _write_baseline(snap, p2)
        assert p1.read_text() == p2.read_text(), "同一スナップショットの JSON 直列化が非決定論的"

    def test_baseline_keys_sorted(self, tmp_path):
        """baseline.json のトップレベルキーがソート済みであること（sort_keys=True）"""
        baseline_path = tmp_path / "baseline.json"
        snap = _make_snapshot({"z_table": [], "a_table": []})
        _write_baseline(snap, baseline_path)
        raw = json.loads(baseline_path.read_text())
        keys = list(raw.keys())
        assert keys == sorted(keys), "baseline.json のキーがソートされていない"

    def test_float_precision_preserved_in_json(self, tmp_path):
        """浮動小数点値が JSON 経由でも精度を保持すること"""
        v = round(math.pi, gc.ROUND_DECIMALS)
        snap = _make_snapshot({"t": [{"score": v}]})
        baseline_path = tmp_path / "baseline.json"
        _write_baseline(snap, baseline_path)
        loaded = _load_baseline(baseline_path)
        recovered = loaded["t"]["rows"][0]["score"]
        assert abs(recovered - v) < 10 ** (-gc.ROUND_DECIMALS + 1), \
            f"JSON ラウンドトリップで精度が失われた: {v} → {recovered}"

    def test_unicode_preserved_in_json(self, tmp_path):
        """Unicode 文字列が JSON 経由で保持されること（ensure_ascii=False）"""
        snap = _make_snapshot({"t": [{"formula": "モメンタム戦略"}]})
        baseline_path = tmp_path / "baseline.json"
        _write_baseline(snap, baseline_path)
        loaded = _load_baseline(baseline_path)
        assert loaded["t"]["rows"][0]["formula"] == "モメンタム戦略"

    def test_full_determinism_cycle(self, tmp_path):
        """
        baseline.json 保存 → 再スナップ → check の完全サイクルが PASS すること。
        これが make golden-determinism の DB レス証明。
        """
        baseline_path = tmp_path / "baseline.json"
        snap1 = _make_snapshot(self.BASE_TABLES)
        _write_baseline(snap1, baseline_path)
        # 2 回目のスナップ（= make golden-check に相当）
        snap2 = _make_snapshot(self.BASE_TABLES)
        loaded_base = _load_baseline(baseline_path)
        problems = gc.diff_tables(loaded_base, snap2)
        assert problems == [], \
            "full_determinism_cycle: baseline → re-snapshot → check が FAIL した"


# ===========================================================================
# Layer E — 非決定論性注入時に FAIL すること（false negative なし）
# ===========================================================================

class TestNonDeterminismDetection:
    """
    非決定論的な変化が注入されたとき、diff_tables が確実に検出することを確認する。
    これにより「golden-determinism が壊れているのに PASS してしまう」事故を防ぐ。
    """

    def test_detect_score_change(self):
        """スコアの変化を検出する"""
        base = _make_snapshot({"t": [{"formula": "a", "score": 0.80}]})
        cur  = _make_snapshot({"t": [{"formula": "a", "score": 0.81}]})
        problems = gc.diff_tables(base, cur)
        assert len(problems) > 0, "スコアの変化を検出できなかった"

    def test_detect_decision_change(self):
        """決定（SELECTED → HOLD）の変化を検出する"""
        base = _make_snapshot({"t": [{"formula": "a", "decision": "SELECTED"}]})
        cur  = _make_snapshot({"t": [{"formula": "a", "decision": "HOLD"}]})
        problems = gc.diff_tables(base, cur)
        assert len(problems) > 0, "decision 変化を検出できなかった"

    def test_detect_row_added(self):
        """新規行の追加を検出する"""
        base = _make_snapshot({"t": [{"formula": "a", "score": 0.5}]})
        cur  = _make_snapshot({"t": [
            {"formula": "a", "score": 0.5},
            {"formula": "b", "score": 0.9},  # 追加
        ]})
        problems = gc.diff_tables(base, cur)
        assert any("追加" in p for p in problems), "行追加を検出できなかった"

    def test_detect_row_removed(self):
        """行の削除を検出する"""
        base = _make_snapshot({"t": [
            {"formula": "a", "score": 0.5},
            {"formula": "b", "score": 0.9},
        ]})
        cur = _make_snapshot({"t": [{"formula": "a", "score": 0.5}]})
        problems = gc.diff_tables(base, cur)
        assert any("消失" in p for p in problems), "行削除を検出できなかった"

    def test_detect_table_missing(self):
        """テーブルの欠落を検出する"""
        base = _make_snapshot({
            "frost_evaluations": [{"formula": "a", "score": 0.5}],
            "causal_runs": [{"formula": "a", "pass": True}],
        })
        cur = _make_snapshot({"frost_evaluations": [{"formula": "a", "score": 0.5}]})
        problems = gc.diff_tables(base, cur)
        assert any("causal_runs" in p and "欠落" in p for p in problems)

    def test_detect_column_schema_change(self):
        """スキーマ変化（列追加）を検出する"""
        base = _make_snapshot({"t": [{"formula": "a", "score": 0.5}]})
        cur  = _make_snapshot({"t": [{"formula": "a", "score": 0.5, "new_col": 1}]})
        problems = gc.diff_tables(base, cur)
        assert any("列構成" in p for p in problems), "列追加を検出できなかった"

    def test_detect_subtle_float_change_beyond_tolerance(self):
        """ROUND_DECIMALS 桁を超える浮動小数点変化を検出する"""
        v_base = round(0.1234567890, gc.ROUND_DECIMALS)
        v_cur  = round(0.1234567899, gc.ROUND_DECIMALS)  # 最終桁が異なる
        if v_base == v_cur:
            pytest.skip("この精度では区別できない（ROUND_DECIMALS 以内）")
        base = _make_snapshot({"t": [{"score": v_base}]})
        cur  = _make_snapshot({"t": [{"score": v_cur}]})
        problems = gc.diff_tables(base, cur)
        assert len(problems) > 0, "微小な float 変化を検出できなかった"

    def test_no_false_negative_on_nan_to_value(self):
        """NaN → 数値 の変化（回復）を検出する"""
        base = _make_snapshot({"t": [{"score": float("nan")}]})
        cur  = _make_snapshot({"t": [{"score": 0.75}]})
        problems = gc.diff_tables(base, cur)
        assert len(problems) > 0, "NaN→数値 の変化を検出できなかった"
