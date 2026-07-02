"""
tests/unit/test_phase1_policy_spec.py
--------------------------------------
Phase 1: PolicySpec 統合の単体テスト (DB 不要)

カバー範囲:
  TestPolicySpecDefaults         (6)  デフォルト値・フィールド構成
  TestPolicySpecHash             (8)  ハッシュ決定論性・変更検知・canonical除外
  TestPolicySpecSerialization    (8)  to_dict / from_dict ラウンドトリップ
  TestPolicySpecValidation       (7)  validate() 正常・異常系
  TestPolicySpecWeightHelpers    (6)  重み集計ヘルパー
  TestPolicySpecBridge           (8)  from_frost_config / to_frost_config
  TestLoadPolicySpec             (5)  load_policy_spec() 環境変数・overrides
  TestFrostRunOutputPolicyHash   (4)  FrostRunOutput.policy_hash フィールド
  TestMigrationFiles             (4)  migration SQL ファイル存在確認

合計: 56 テスト
"""
from __future__ import annotations

import os
from dataclasses import fields
from pathlib import Path
from typing import Any, Dict

import pytest

from analytics.python.frost.frost_config import FrostConfig, load_frost_config
from analytics.python.frost.frost_contracts import FrostRunOutput
from analytics.python.frost.policy_spec import (
    PolicySpec,
    _POLICY_ENV_VARS,
    load_policy_spec,
    policy_spec_from_frost_config,
    policy_spec_to_frost_config,
)

# プロジェクトルート
ROOT = Path(__file__).parents[2]
MIGRATIONS_DIR = ROOT / "qedschema" / "migrations"


# ===========================================================================
# TestPolicySpecDefaults
# ===========================================================================

class TestPolicySpecDefaults:
    def test_default_instantiation(self):
        spec = PolicySpec()
        assert spec is not None

    def test_frozen_immutability(self):
        spec = PolicySpec()
        with pytest.raises((AttributeError, TypeError)):
            spec.w_predictive = 0.99  # type: ignore[misc]

    def test_default_engine_version(self):
        spec = PolicySpec()
        assert spec.engine_version == "frost_v1"

    def test_default_phase_tag(self):
        spec = PolicySpec()
        assert spec.phase_tag == "phase1"

    def test_default_use_v2_score_false(self):
        spec = PolicySpec()
        assert spec.use_v2_score is False

    def test_all_weight_fields_present(self):
        spec = PolicySpec()
        # 正方向 9 軸 + ペナルティ 8 軸 = 17 フィールド
        weight_fields = [f.name for f in fields(spec) if f.name.startswith("w_")]
        assert len(weight_fields) == 17, f"期待 17 個, 実際 {len(weight_fields)} 個: {weight_fields}"


# ===========================================================================
# TestPolicySpecHash
# ===========================================================================

class TestPolicySpecHash:
    def test_hash_prefix(self):
        spec = PolicySpec()
        assert spec.policy_hash.startswith("sha256:")

    def test_hash_length(self):
        spec = PolicySpec()
        # "sha256:" (7) + 64 hex = 71 chars
        assert len(spec.policy_hash) == 7 + 64

    def test_hash_determinism(self):
        """同一パラメータのインスタンスは同一ハッシュ"""
        spec1 = PolicySpec()
        spec2 = PolicySpec()
        assert spec1.policy_hash == spec2.policy_hash

    def test_hash_changes_on_weight_change(self):
        """重みを変えるとハッシュが変わる"""
        spec1 = PolicySpec(w_predictive=0.20)
        spec2 = PolicySpec(w_predictive=0.30)
        assert spec1.policy_hash != spec2.policy_hash

    def test_hash_changes_on_gate_change(self):
        """Hard gate を変えるとハッシュが変わる"""
        spec1 = PolicySpec(pbo_threshold=0.20)
        spec2 = PolicySpec(pbo_threshold=0.25)
        assert spec1.policy_hash != spec2.policy_hash

    def test_description_excluded_from_hash(self):
        """description はハッシュに含まれない"""
        spec1 = PolicySpec(description="実験 A")
        spec2 = PolicySpec(description="実験 B")
        assert spec1.policy_hash == spec2.policy_hash

    def test_source_env_vars_excluded_from_hash(self):
        """source_env_vars はハッシュに含まれない"""
        spec1 = PolicySpec(source_env_vars=("FROST_W_PREDICTIVE",))
        spec2 = PolicySpec(source_env_vars=())
        assert spec1.policy_hash == spec2.policy_hash

    def test_property_alias(self):
        """policy_hash プロパティと compute_hash() が一致"""
        spec = PolicySpec()
        assert spec.policy_hash == spec.compute_hash()


# ===========================================================================
# TestPolicySpecSerialization
# ===========================================================================

class TestPolicySpecSerialization:
    def test_to_dict_top_level_keys(self):
        spec = PolicySpec()
        d = spec.to_dict()
        assert set(d.keys()) == {"weights", "hard_gates", "selection", "backtest", "meta"}

    def test_to_dict_weights_count(self):
        spec = PolicySpec()
        assert len(spec.to_dict()["weights"]) == 17

    def test_to_dict_hard_gates_count(self):
        spec = PolicySpec()
        # v1: 8 個 + v2: 7 個 = 15 個
        assert len(spec.to_dict()["hard_gates"]) == 15

    def test_roundtrip_hash_preservation(self):
        """to_dict → from_dict でハッシュが変わらない"""
        spec = PolicySpec(w_predictive=0.25, description="テスト")
        restored = PolicySpec.from_dict(spec.to_dict())
        assert spec.policy_hash == restored.policy_hash

    def test_roundtrip_weight_values(self):
        spec = PolicySpec(w_predictive=0.25, w_oos_sharpe=0.20)
        restored = PolicySpec.from_dict(spec.to_dict())
        assert restored.w_predictive == 0.25
        assert restored.w_oos_sharpe == 0.20

    def test_roundtrip_gate_values(self):
        spec = PolicySpec(pbo_threshold=0.15, max_drawdown=0.25)
        restored = PolicySpec.from_dict(spec.to_dict())
        assert restored.pbo_threshold == 0.15
        assert restored.max_drawdown == 0.25

    def test_roundtrip_meta_values(self):
        spec = PolicySpec(engine_version="frost_v2", phase_tag="phase3")
        restored = PolicySpec.from_dict(spec.to_dict())
        assert restored.engine_version == "frost_v2"
        assert restored.phase_tag == "phase3"

    def test_from_dict_missing_keys_use_defaults(self):
        """不完全な辞書でも from_dict はデフォルト値で補完する"""
        spec = PolicySpec.from_dict({"weights": {}, "hard_gates": {}, "selection": {}, "backtest": {}, "meta": {}})
        assert spec.w_predictive == 0.20  # デフォルト値
        assert spec.pbo_threshold == 0.20


# ===========================================================================
# TestPolicySpecValidation
# ===========================================================================

class TestPolicySpecValidation:
    def test_default_spec_is_valid(self):
        PolicySpec().validate()  # 例外なし

    def test_negative_weight_raises(self):
        with pytest.raises(ValueError, match="0 以上"):
            PolicySpec(w_predictive=-0.01).validate()

    def test_pbo_threshold_out_of_range(self):
        with pytest.raises(ValueError, match="pbo_threshold"):
            PolicySpec(pbo_threshold=1.5).validate()

    def test_max_drawdown_out_of_range(self):
        with pytest.raises(ValueError, match="max_drawdown"):
            PolicySpec(max_drawdown=-0.1).validate()

    def test_top_k_zero_raises(self):
        with pytest.raises(ValueError, match="top_k"):
            PolicySpec(top_k=0).validate()

    def test_promotion_top_k_exceeds_top_k(self):
        with pytest.raises(ValueError, match="promotion_top_k"):
            PolicySpec(top_k=5, promotion_top_k=10).validate()

    def test_promotion_top_k_equals_top_k_is_valid(self):
        PolicySpec(top_k=5, promotion_top_k=5).validate()  # 例外なし


# ===========================================================================
# TestPolicySpecWeightHelpers
# ===========================================================================

class TestPolicySpecWeightHelpers:
    def test_positive_weight_sum_default(self):
        spec = PolicySpec()
        # 0.20 + 0.15 + 0.15 + 0.10 + 0.10 + 0.05 = 0.75
        assert abs(spec.positive_weight_sum() - 0.75) < 1e-9

    def test_positive_weight_sum_v2_default(self):
        spec = PolicySpec()
        # 0.75 + 0.05 + 0.05 + 0.05 = 0.90
        assert abs(spec.positive_weight_sum_v2() - 0.90) < 1e-9

    def test_penalty_weight_sum_default(self):
        spec = PolicySpec()
        # 0.02 + 0.10 + 0.05 + 0.05 + 0.03 = 0.25
        assert abs(spec.penalty_weight_sum() - 0.25) < 1e-9

    def test_penalty_weight_sum_v2_default(self):
        spec = PolicySpec()
        # 0.25 + 0.05 + 0.03 + 0.02 = 0.35
        assert abs(spec.penalty_weight_sum_v2() - 0.35) < 1e-9

    def test_weight_sum_after_custom_weights(self):
        spec = PolicySpec(w_predictive=0.30, w_oos_sharpe=0.10)
        # 0.30 + 0.10 + 0.15 + 0.10 + 0.10 + 0.05 = 0.80
        assert abs(spec.positive_weight_sum() - 0.80) < 1e-9

    def test_short_repr(self):
        spec = PolicySpec()
        r = spec.short_repr()
        assert "sha256:" in r
        assert "frost_v1" in r


# ===========================================================================
# TestPolicySpecBridge
# ===========================================================================

class TestPolicySpecBridge:
    def _default_cfg(self) -> FrostConfig:
        return FrostConfig()

    def test_from_frost_config_returns_policy_spec(self):
        cfg = self._default_cfg()
        spec = policy_spec_from_frost_config(cfg)
        assert isinstance(spec, PolicySpec)

    def test_from_frost_config_weights_preserved(self):
        cfg = FrostConfig(w_predictive=0.30, w_oos_sharpe=0.20)
        spec = policy_spec_from_frost_config(cfg)
        assert spec.w_predictive == 0.30
        assert spec.w_oos_sharpe == 0.20

    def test_from_frost_config_gates_preserved(self):
        cfg = FrostConfig(pbo_threshold=0.15, max_drawdown=0.25)
        spec = policy_spec_from_frost_config(cfg)
        assert spec.pbo_threshold == 0.15
        assert spec.max_drawdown == 0.25

    def test_from_frost_config_hash_determinism(self):
        cfg = self._default_cfg()
        spec1 = policy_spec_from_frost_config(cfg)
        spec2 = policy_spec_from_frost_config(cfg)
        assert spec1.policy_hash == spec2.policy_hash

    def test_to_frost_config_returns_frost_config(self):
        spec = PolicySpec()
        cfg = policy_spec_to_frost_config(spec)
        assert isinstance(cfg, FrostConfig)

    def test_to_frost_config_weights_preserved(self):
        spec = PolicySpec(w_predictive=0.30, w_oos_sharpe=0.20)
        cfg = policy_spec_to_frost_config(spec)
        assert cfg.w_predictive == 0.30
        assert cfg.w_oos_sharpe == 0.20

    def test_roundtrip_frost_config_policy_spec(self):
        """FrostConfig → PolicySpec → FrostConfig で重みが保持される"""
        original = FrostConfig(w_predictive=0.25)
        spec = policy_spec_from_frost_config(original)
        restored = policy_spec_to_frost_config(spec)
        assert restored.w_predictive == 0.25

    def test_default_config_policy_spec_hash_consistent(self):
        """デフォルト FrostConfig と デフォルト PolicySpec のハッシュが一致"""
        cfg = FrostConfig()
        spec_from_cfg = policy_spec_from_frost_config(cfg)
        spec_direct = PolicySpec()
        assert spec_from_cfg.policy_hash == spec_direct.policy_hash


# ===========================================================================
# TestLoadPolicySpec
# ===========================================================================

class TestLoadPolicySpec:
    def test_load_policy_spec_returns_policy_spec(self):
        spec = load_policy_spec()
        assert isinstance(spec, PolicySpec)

    def test_load_policy_spec_validates(self):
        """load_policy_spec は validate() 済みを返す (例外なし)"""
        spec = load_policy_spec()
        spec.validate()  # 二重呼び出しも例外なし

    def test_load_policy_spec_overrides(self):
        spec = load_policy_spec(overrides={"w_predictive": 0.35})
        assert spec.w_predictive == 0.35

    def test_load_policy_spec_phase_tag(self):
        spec = load_policy_spec(phase_tag="phase_test")
        assert spec.phase_tag == "phase_test"

    def test_load_policy_spec_env_var(self, monkeypatch):
        """環境変数 FROST_W_PREDICTIVE が反映される"""
        monkeypatch.setenv("FROST_W_PREDICTIVE", "0.40")
        spec = load_policy_spec()
        assert spec.w_predictive == pytest.approx(0.40)


# ===========================================================================
# TestFrostRunOutputPolicyHash
# ===========================================================================

class TestFrostRunOutputPolicyHash:
    def test_policy_hash_field_exists(self):
        out = FrostRunOutput()
        assert hasattr(out, "policy_hash")

    def test_policy_hash_default_is_none(self):
        out = FrostRunOutput()
        assert out.policy_hash is None

    def test_policy_hash_can_be_set(self):
        spec = PolicySpec()
        out = FrostRunOutput(policy_hash=spec.policy_hash)
        assert out.policy_hash == spec.policy_hash

    def test_policy_hash_format(self):
        spec = PolicySpec()
        out = FrostRunOutput(policy_hash=spec.policy_hash)
        assert out.policy_hash is not None
        assert out.policy_hash.startswith("sha256:")
        assert len(out.policy_hash) == 71


# ===========================================================================
# TestMigrationFiles
# ===========================================================================

class TestMigrationFiles:
    def test_migration_082_exists(self):
        path = MIGRATIONS_DIR / "082_qed_policies.sql"
        assert path.exists(), f"migration 082 が見つかりません: {path}"

    def test_migration_083_exists(self):
        path = MIGRATIONS_DIR / "083_frost_runs_policy_hash.sql"
        assert path.exists(), f"migration 083 が見つかりません: {path}"

    def test_migration_082_contains_qed_policies(self):
        content = (MIGRATIONS_DIR / "082_qed_policies.sql").read_text()
        assert "CREATE TABLE qed_policies" in content
        assert "policy_hash" in content
        assert "spec_json" in content

    def test_migration_083_adds_policy_hash_to_frost_runs(self):
        content = (MIGRATIONS_DIR / "083_frost_runs_policy_hash.sql").read_text()
        assert "policy_hash" in content
        assert "frost_runs" in content
        assert "qed_policies" in content
