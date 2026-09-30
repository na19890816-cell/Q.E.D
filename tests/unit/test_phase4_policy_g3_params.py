"""
test_phase4_policy_g3_params.py
-------------------------------
Phase 4a: G3 (CUSUM / Detect→Kill) パラメータの PolicySpec / FrostConfig 集約テスト

HANDOVER_2026-09-30 §6「PolicySpec への G3 パラメータ追加（未着手）」の解消を検証する。

対象フィールド:
    cusum_k: float = 0.5
    cusum_h: float = 5.0
    cusum_mu0: float = 0.0
    lifecycle_min_ic_len: int = 10
"""
from __future__ import annotations

import pytest

from analytics.python.frost.frost_config import FrostConfig, load_frost_config
from analytics.python.frost.frost_cusum import CusumDetector, CusumParams
from analytics.python.frost.frost_lifecycle import AlphaLifecycleEngine, LifecycleRecord
from analytics.python.frost.policy_spec import (
    PolicySpec,
    _POLICY_ENV_VARS,
    load_policy_spec,
    policy_spec_from_frost_config,
    policy_spec_to_frost_config,
)

pytestmark = pytest.mark.phase4_policy_g3

G3_FIELDS = ("cusum_k", "cusum_h", "cusum_mu0", "lifecycle_min_ic_len")
G3_ENVS = (
    "FROST_CUSUM_K", "FROST_CUSUM_H", "FROST_CUSUM_MU0", "FROST_LIFECYCLE_MIN_IC_LEN",
)


# ===========================================================================
# PolicySpec
# ===========================================================================

class TestPolicySpecG3Defaults:
    def test_defaults(self):
        s = PolicySpec()
        assert s.cusum_k == pytest.approx(0.5)
        assert s.cusum_h == pytest.approx(5.0)
        assert s.cusum_mu0 == pytest.approx(0.0)
        assert s.lifecycle_min_ic_len == 10

    def test_defaults_match_cusum_params(self):
        """PolicySpec デフォルトが CusumParams デフォルトと一致する"""
        s, p = PolicySpec(), CusumParams()
        assert (s.cusum_k, s.cusum_h, s.cusum_mu0) == (p.k, p.h, p.mu0)

    def test_defaults_match_lifecycle_engine(self):
        assert PolicySpec().lifecycle_min_ic_len == AlphaLifecycleEngine().min_ic_len

    @pytest.mark.parametrize("name", G3_FIELDS)
    def test_in_hard_gates_section(self, name):
        assert name in PolicySpec().to_dict()["hard_gates"]


class TestPolicySpecG3Hash:
    @pytest.mark.parametrize("kw", [
        {"cusum_k": 0.6}, {"cusum_h": 4.0}, {"cusum_mu0": 0.01},
        {"lifecycle_min_ic_len": 20},
    ])
    def test_hash_changes(self, kw):
        assert PolicySpec(**kw).policy_hash != PolicySpec().policy_hash

    def test_hash_deterministic(self):
        a = PolicySpec(cusum_k=0.4, cusum_h=4.5)
        b = PolicySpec(cusum_k=0.4, cusum_h=4.5)
        assert a.policy_hash == b.policy_hash


class TestPolicySpecG3Roundtrip:
    def test_to_from_dict(self):
        s = PolicySpec(cusum_k=0.3, cusum_h=4.0, cusum_mu0=0.02, lifecycle_min_ic_len=15)
        r = PolicySpec.from_dict(s.to_dict())
        for f in G3_FIELDS:
            assert getattr(r, f) == getattr(s, f)
        assert r.policy_hash == s.policy_hash

    def test_from_dict_missing_keys_default(self):
        """G3 キーを持たない旧 dict (Phase 3 以前の qed_policies) でもデフォルト復元"""
        d = PolicySpec().to_dict()
        for f in G3_FIELDS:
            d["hard_gates"].pop(f)
        r = PolicySpec.from_dict(d)
        assert r.cusum_k == 0.5 and r.cusum_h == 5.0
        assert r.cusum_mu0 == 0.0 and r.lifecycle_min_ic_len == 10

    def test_min_ic_len_type_int(self):
        d = PolicySpec().to_dict()
        d["hard_gates"]["lifecycle_min_ic_len"] = 12.0
        assert isinstance(PolicySpec.from_dict(d).lifecycle_min_ic_len, int)

    def test_frost_config_bridge_roundtrip(self):
        s = PolicySpec(cusum_k=0.25, cusum_h=3.0, cusum_mu0=-0.1, lifecycle_min_ic_len=7)
        cfg = policy_spec_to_frost_config(s)
        for f in G3_FIELDS:
            assert getattr(cfg, f) == getattr(s, f)
        back = policy_spec_from_frost_config(cfg)
        for f in G3_FIELDS:
            assert getattr(back, f) == getattr(s, f)

    def test_from_legacy_frost_config_without_fields(self):
        """G3 属性を持たない旧 FrostConfig 風オブジェクトでもデフォルト"""
        cfg = FrostConfig()
        legacy = type("Legacy", (), {k: getattr(cfg, k) for k in vars(FrostConfig()) if k not in G3_FIELDS})()
        s = policy_spec_from_frost_config(legacy)
        assert s.cusum_k == 0.5 and s.lifecycle_min_ic_len == 10


class TestPolicySpecG3Validate:
    @pytest.mark.parametrize("kw", [
        {"cusum_k": -0.1}, {"cusum_h": 0.0}, {"cusum_h": -1.0},
        {"lifecycle_min_ic_len": 0},
    ])
    def test_invalid(self, kw):
        with pytest.raises(ValueError):
            PolicySpec(**kw).validate()

    def test_valid_defaults(self):
        PolicySpec().validate()

    def test_k_zero_allowed(self):
        PolicySpec(cusum_k=0.0).validate()


class TestPolicySpecG3Env:
    def test_env_vars_registered(self):
        for e in G3_ENVS:
            assert e in _POLICY_ENV_VARS

    def test_portfolio_corr_env_registered(self):
        """G2 実装時に漏れていた FROST_MAX_PORTFOLIO_CORR の登録"""
        assert "FROST_MAX_PORTFOLIO_CORR" in _POLICY_ENV_VARS

    def test_load_from_env(self, monkeypatch):
        monkeypatch.setenv("FROST_CUSUM_K", "0.7")
        monkeypatch.setenv("FROST_CUSUM_H", "6.5")
        monkeypatch.setenv("FROST_CUSUM_MU0", "0.05")
        monkeypatch.setenv("FROST_LIFECYCLE_MIN_IC_LEN", "30")
        s = load_policy_spec()
        assert s.cusum_k == pytest.approx(0.7)
        assert s.cusum_h == pytest.approx(6.5)
        assert s.cusum_mu0 == pytest.approx(0.05)
        assert s.lifecycle_min_ic_len == 30
        for e in G3_ENVS:
            assert e in s.source_env_vars

    def test_load_invalid_env_raises(self, monkeypatch):
        monkeypatch.setenv("FROST_CUSUM_H", "-1")
        with pytest.raises(ValueError):
            load_policy_spec()

    def test_load_overrides(self):
        s = load_policy_spec(overrides={"cusum_h": 8.0})
        assert s.cusum_h == 8.0


# ===========================================================================
# FrostConfig
# ===========================================================================

class TestFrostConfigG3:
    def test_defaults(self):
        c = FrostConfig()
        assert (c.cusum_k, c.cusum_h, c.cusum_mu0, c.lifecycle_min_ic_len) == (0.5, 5.0, 0.0, 10)

    def test_load_from_env(self, monkeypatch):
        monkeypatch.setenv("FROST_CUSUM_K", "0.2")
        monkeypatch.setenv("FROST_LIFECYCLE_MIN_IC_LEN", "12")
        c = load_frost_config()
        assert c.cusum_k == pytest.approx(0.2)
        assert c.lifecycle_min_ic_len == 12

    @pytest.mark.parametrize("kw", [
        {"cusum_k": -1.0}, {"cusum_h": 0.0}, {"lifecycle_min_ic_len": 0},
    ])
    def test_validate_invalid(self, kw):
        with pytest.raises(ValueError):
            FrostConfig(**kw).validate()


# ===========================================================================
# from_config 経由の end-to-end 連携
# ===========================================================================

class TestG3FromConfigIntegration:
    def test_cusum_params_from_policy_spec(self):
        p = CusumParams.from_config(PolicySpec(cusum_k=0.3, cusum_h=4.0, cusum_mu0=0.1))
        assert (p.k, p.h, p.mu0) == (0.3, 4.0, 0.1)

    def test_detector_from_frost_config(self):
        d = CusumDetector.from_config(FrostConfig(cusum_h=2.0))
        assert d.params.h == 2.0

    def test_engine_from_policy_spec(self):
        e = AlphaLifecycleEngine.from_config(PolicySpec(lifecycle_min_ic_len=25, cusum_h=3.0))
        assert e.min_ic_len == 25
        assert e.params.h == 3.0

    def test_policy_controls_detection(self):
        """h を下げると同じ IC 系列で劣化検知される (PolicySpec がふるまいを支配する)"""
        ic = [-0.8] * 12  # デフォルト h=5.0 では 17 ステップ必要 → 未検知
        strict = AlphaLifecycleEngine.from_config(PolicySpec(cusum_h=2.0))
        default = AlphaLifecycleEngine.from_config(PolicySpec())
        assert strict.check(LifecycleRecord("a", rolling_ic=ic)).degradation_detected
        assert not default.check(LifecycleRecord("a", rolling_ic=ic)).degradation_detected

    def test_min_ic_len_skip(self):
        e = AlphaLifecycleEngine.from_config(PolicySpec(lifecycle_min_ic_len=50))
        r = e.check(LifecycleRecord("a", rolling_ic=[-2.0] * 20))
        assert not r.degradation_detected
