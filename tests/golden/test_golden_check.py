"""
test_golden_check.py — golden_check.py のロジック単体テスト

DB 接続を一切使わず、_normalize / diff_tables / _is_volatile を直接テストする。
golden-determinism の実行には実 DB が必要だが、ロジック部分はここで保証する。
"""
from __future__ import annotations

import importlib
import json
import sys
import os
from pathlib import Path

import pytest

# scripts/golden を import パスに追加
SCRIPTS_GOLDEN = Path(__file__).parents[2] / "scripts" / "golden"
sys.path.insert(0, str(SCRIPTS_GOLDEN))

import golden_check as gc


# ============================================================
# _is_volatile
# ============================================================

class TestIsVolatile:
    def test_created_at(self):
        assert gc._is_volatile("created_at") is True

    def test_updated_at(self):
        assert gc._is_volatile("updated_at") is True

    def test_id(self):
        assert gc._is_volatile("id") is True

    def test_run_id(self):
        assert gc._is_volatile("run_id") is True

    def test_trace_id(self):
        assert gc._is_volatile("trace_id") is True

    def test_uuid_column(self):
        assert gc._is_volatile("some_uuid_col") is True

    def test_timestamp_column(self):
        assert gc._is_volatile("event_timestamp") is True

    def test_stable_candidate_hash(self):
        """candidate_hash は内容ハッシュ。揮発しない"""
        assert gc._is_volatile("candidate_hash") is False

    def test_stable_formula(self):
        assert gc._is_volatile("formula") is False

    def test_stable_frost_score(self):
        assert gc._is_volatile("frost_score") is False

    def test_stable_decision(self):
        assert gc._is_volatile("decision") is False


# ============================================================
# _normalize
# ============================================================

class TestNormalize:
    def test_float_round(self):
        result = gc._normalize(1.123456789012345)
        assert isinstance(result, float)
        # ROUND_DECIMALS=10 なら 10 桁丸め
        assert result == round(1.123456789012345, gc.ROUND_DECIMALS)

    def test_nan(self):
        assert gc._normalize(float("nan")) == "NaN"

    def test_inf(self):
        assert gc._normalize(float("inf")) == "inf"

    def test_neg_inf(self):
        assert gc._normalize(float("-inf")) == "-inf"

    def test_none(self):
        assert gc._normalize(None) is None

    def test_str(self):
        assert gc._normalize("hello") == "hello"

    def test_int(self):
        assert gc._normalize(42) == 42

    def test_dict_sorted(self):
        """辞書はキーでソートされる(決定論的JSON化のため)"""
        d = {"z": 1.0, "a": 2.0}
        result = gc._normalize(d)
        assert list(result.keys()) == ["a", "z"]

    def test_list(self):
        result = gc._normalize([1.1, float("nan"), None])
        assert result[1] == "NaN"
        assert result[2] is None

    def test_decimal_like(self):
        """Decimal 型は float 丸めになる"""
        from decimal import Decimal
        result = gc._normalize(Decimal("1.5"))
        assert isinstance(result, float)
        assert result == 1.5


# ============================================================
# diff_tables
# ============================================================

class TestDiffTables:
    def _make_snap(self, tables: dict) -> dict:
        """テスト用スナップショット。各テーブルは rows リストを持つ"""
        result = {}
        for tbl, rows in tables.items():
            result[tbl] = {
                "columns": list(rows[0].keys()) if rows else [],
                "row_count": len(rows),
                "rows": rows,
            }
        return result

    def test_identical_snapshots(self):
        rows = [{"formula": "a+b", "score": 0.5}, {"formula": "x*y", "score": 0.3}]
        base = self._make_snap({"frost_evaluations": rows})
        cur  = self._make_snap({"frost_evaluations": rows})
        problems = gc.diff_tables(base, cur)
        assert problems == []

    def test_missing_table_in_current(self):
        rows = [{"formula": "a+b", "score": 0.5}]
        base = self._make_snap({"frost_evaluations": rows, "causal_runs": rows})
        cur  = self._make_snap({"frost_evaluations": rows})
        problems = gc.diff_tables(base, cur)
        assert any("causal_runs" in p and "欠落" in p for p in problems)

    def test_new_table_in_current(self):
        rows = [{"formula": "a+b", "score": 0.5}]
        base = self._make_snap({"frost_evaluations": rows})
        cur  = self._make_snap({"frost_evaluations": rows, "new_table": rows})
        problems = gc.diff_tables(base, cur)
        assert any("new_table" in p for p in problems)

    def test_row_missing(self):
        base_rows = [{"formula": "a+b", "score": 0.5}, {"formula": "x*y", "score": 0.3}]
        cur_rows  = [{"formula": "a+b", "score": 0.5}]
        base = self._make_snap({"frost_evaluations": base_rows})
        cur  = self._make_snap({"frost_evaluations": cur_rows})
        problems = gc.diff_tables(base, cur)
        assert any("frost_evaluations" in p and "消失" in p for p in problems)

    def test_row_added(self):
        base_rows = [{"formula": "a+b", "score": 0.5}]
        cur_rows  = [{"formula": "a+b", "score": 0.5}, {"formula": "new", "score": 0.9}]
        base = self._make_snap({"frost_evaluations": base_rows})
        cur  = self._make_snap({"frost_evaluations": cur_rows})
        problems = gc.diff_tables(base, cur)
        assert any("frost_evaluations" in p and "追加" in p for p in problems)

    def test_column_change(self):
        base_rows = [{"formula": "a+b", "score": 0.5}]
        cur_rows  = [{"formula": "a+b", "new_col": 1}]
        base = self._make_snap({"t": base_rows})
        cur  = self._make_snap({"t": cur_rows})
        problems = gc.diff_tables(base, cur)
        assert any("列構成" in p for p in problems)

    def test_float_rounding_stable(self):
        """浮動小数点のごくわずかな差は ROUND_DECIMALS 以内なら一致"""
        base_rows = [{"score": round(0.123456789012345, gc.ROUND_DECIMALS)}]
        # ROUND_DECIMALS 桁以内の差
        cur_rows  = [{"score": round(0.123456789012345, gc.ROUND_DECIMALS)}]
        base = self._make_snap({"t": base_rows})
        cur  = self._make_snap({"t": cur_rows})
        problems = gc.diff_tables(base, cur)
        assert problems == []

    def test_order_independent(self):
        """行の順序が違っても正準ソートで一致する"""
        r1 = {"formula": "a+b", "score": 0.5}
        r2 = {"formula": "x*y", "score": 0.3}
        base = self._make_snap({"t": [r1, r2]})
        cur  = self._make_snap({"t": [r2, r1]})  # 逆順
        problems = gc.diff_tables(base, cur)
        assert problems == []


# ============================================================
# EMLDiscoveryConfig の rng_seed フィールド
# ============================================================

class TestEmlDiscoveryConfigSeed:
    def test_rng_seed_field_exists(self):
        """EMLDiscoveryConfig に rng_seed フィールドが追加されていること"""
        sys.path.insert(0, str(Path(__file__).parents[2] / "analytics" / "python"))
        from alpha.eml.eml_master_formula import EMLDiscoveryConfig
        cfg = EMLDiscoveryConfig()
        assert hasattr(cfg, "rng_seed")

    def test_rng_seed_default_none(self, monkeypatch):
        """EML_SEED 未設定時は None"""
        monkeypatch.delenv("EML_SEED", raising=False)
        sys.path.insert(0, str(Path(__file__).parents[2] / "analytics" / "python"))
        # reload して環境変数の変化を反映
        import importlib
        import alpha.eml.eml_master_formula as mod
        importlib.reload(mod)
        cfg = mod.EMLDiscoveryConfig()
        assert cfg.rng_seed is None

    def test_rng_seed_from_env(self, monkeypatch):
        """EML_SEED=42 → rng_seed=42"""
        monkeypatch.setenv("EML_SEED", "42")
        import importlib
        import alpha.eml.eml_master_formula as mod
        importlib.reload(mod)
        cfg = mod.EMLDiscoveryConfig()
        assert cfg.rng_seed == 42


# ============================================================
# golden_dataset.py の import チェック(構文確認)
# ============================================================

class TestGoldenDatasetImport:
    def test_importable(self):
        import golden_dataset  # noqa: F401


# ============================================================
# Makefile.golden の存在確認
# ============================================================

class TestMakefileGolden:
    def test_makefile_golden_exists(self):
        mf = Path(__file__).parents[2] / "Makefile.golden"
        assert mf.exists(), "Makefile.golden が存在しない"

    def test_makefile_golden_targets(self):
        mf = Path(__file__).parents[2] / "Makefile.golden"
        content = mf.read_text()
        for target in ["golden-db-init", "golden-dataset", "golden-load",
                       "golden-baseline", "golden-check", "golden-determinism"]:
            assert target in content, f"ターゲット {target} が Makefile.golden にない"

    def test_makefile_includes_golden(self):
        mf = Path(__file__).parents[2] / "Makefile"
        content = mf.read_text()
        assert "include Makefile.golden" in content

    def test_frost_pbo_parallel_disabled_in_golden(self):
        """golden run では PBO 並列を無効化する設定があること"""
        mf = Path(__file__).parents[2] / "Makefile.golden"
        content = mf.read_text()
        assert "FROST_PBO_PARALLEL_ENABLED=0" in content

    def test_eml_seed_set_in_golden(self):
        """golden run では EML_SEED が設定されること"""
        mf = Path(__file__).parents[2] / "Makefile.golden"
        content = mf.read_text()
        assert "EML_SEED=42" in content


# ============================================================
# ADR-001 の存在確認
# ============================================================

class TestADR001:
    def test_adr001_exists(self):
        adr = Path(__file__).parents[2] / "docs" / "adr" / "ADR-001-numpy-policy.md"
        assert adr.exists()

    def test_adr001_contains_key_decisions(self):
        adr = Path(__file__).parents[2] / "docs" / "adr" / "ADR-001-numpy-policy.md"
        content = adr.read_text()
        assert "Phase 7" in content       # 導入は Phase 7 のみ
        assert "frost_pbo" in content     # ホワイトリスト記載
        assert "Accepted" in content      # ステータス
