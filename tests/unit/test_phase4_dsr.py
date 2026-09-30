"""
test_phase4_dsr.py
------------------
Phase 4b (G1): Deflated Sharpe Ratio (frost_dsr.py) の単体テスト

## 数値根拠

- Bailey & López de Prado (2014) §数値例:
    年率 SR = 2.5, T = 1250 (日次 5 年), N = 100, V[SR] (年率) = 0.5,
    歪度 = -3, 尖度 = 10  →  DSR ≈ 0.9004, 年率 SR0 ≈ 1.789
- 正規分布関数は scipy が利用可能ならその値と照合 (無ければ固定値のみ)
"""
from __future__ import annotations

import inspect
import math
import random

import pytest

from analytics.python.frost import frost_dsr
from analytics.python.frost.frost_dsr import (
    EULER_GAMMA,
    MIN_OBS,
    N_TRIALS_SOURCE_ASSUMED,
    N_TRIALS_SOURCE_PROVIDED,
    DsrGate,
    DsrGateResult,
    DsrParams,
    ReturnMoments,
    check_dsr_gate,
    cross_trial_sr_variance,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    norm_cdf,
    norm_pdf,
    norm_ppf,
    probabilistic_sharpe_ratio,
    sample_moments,
    sharpe_estimator_variance,
    sharpe_ratio,
)
from analytics.python.frost.frost_config import FrostConfig
from analytics.python.frost.policy_spec import PolicySpec

pytestmark = pytest.mark.phase4_dsr

# 論文数値例
PAPER_SR = 2.5 / math.sqrt(250)
PAPER_T = 1250
PAPER_N = 100
PAPER_V = 0.5 / 250
PAPER_SKEW = -3.0
PAPER_KURT = 10.0


def _normal_returns(n: int, mu: float, sigma: float, seed: int = 42):
    rng = random.Random(seed)
    return [rng.gauss(mu, sigma) for _ in range(n)]


# ===========================================================================
# 正規分布ユーティリティ
# ===========================================================================

class TestNormalFunctions:
    @pytest.mark.parametrize("x,expected", [
        (0.0, 0.5),
        (1.0, 0.8413447460685429),
        (-1.0, 0.15865525393145707),
        (1.959963984540054, 0.975),
        (-3.0, 0.0013498980316301),
    ])
    def test_cdf_values(self, x, expected):
        assert norm_cdf(x) == pytest.approx(expected, abs=1e-14)

    @pytest.mark.parametrize("p,expected", [
        (0.5, 0.0),
        (0.975, 1.959963984540054),
        (0.025, -1.959963984540054),
        (0.99, 2.326347874040841),
        (0.001, -3.090232306167813),
        (1e-10, -6.361340902404056),
    ])
    def test_ppf_values(self, p, expected):
        assert norm_ppf(p) == pytest.approx(expected, abs=1e-9)

    def test_ppf_bounds(self):
        assert norm_ppf(0.0) == float("-inf")
        assert norm_ppf(1.0) == float("inf")
        assert norm_ppf(-0.1) == float("-inf")
        assert math.isnan(norm_ppf(float("nan")))

    def test_cdf_nan(self):
        assert math.isnan(norm_cdf(float("nan")))

    def test_cdf_tails(self):
        assert norm_cdf(-40.0) == pytest.approx(0.0, abs=1e-300)
        assert norm_cdf(40.0) == 1.0

    @pytest.mark.parametrize("p", [1e-12, 1e-6, 0.01, 0.02425, 0.1, 0.3, 0.5, 0.7, 0.97575, 0.999, 1 - 1e-9])
    def test_ppf_cdf_inverse(self, p):
        assert norm_cdf(norm_ppf(p)) == pytest.approx(p, rel=1e-9)

    def test_ppf_symmetry(self):
        for p in (0.001, 0.05, 0.2, 0.4):
            assert norm_ppf(p) == pytest.approx(-norm_ppf(1 - p), abs=1e-9)

    def test_ppf_monotonic(self):
        ps = [i / 1000 for i in range(1, 1000)]
        vals = [norm_ppf(p) for p in ps]
        assert all(a < b for a, b in zip(vals, vals[1:]))

    def test_pdf(self):
        assert norm_pdf(0.0) == pytest.approx(1 / math.sqrt(2 * math.pi))

    def test_against_scipy(self):
        stats = pytest.importorskip("scipy.stats")
        for p in [1e-8, 1e-4, 0.01, 0.2, 0.5, 0.8, 0.99, 1 - 1e-6]:
            assert norm_ppf(p) == pytest.approx(stats.norm.ppf(p), abs=1e-9)
        for x in [-6, -2.5, -0.3, 0, 0.7, 3.1, 7]:
            assert norm_cdf(x) == pytest.approx(stats.norm.cdf(x), abs=1e-15)


# ===========================================================================
# 標本モーメント
# ===========================================================================

class TestSampleMoments:
    def test_empty(self):
        m = sample_moments([])
        assert m.n_obs == 0 and m.sharpe == 0.0 and m.kurt == 3.0

    def test_none_input(self):
        assert sample_moments(None).n_obs == 0

    def test_single(self):
        m = sample_moments([0.01])
        assert m.n_obs == 1 and m.std == 0.0 and m.sharpe == 0.0

    def test_constant(self):
        m = sample_moments([0.01] * 10)
        assert m.std == 0.0 and m.skew == 0.0 and m.kurt == 3.0 and m.sharpe == 0.0

    def test_filters_invalid(self):
        m = sample_moments([1.0, None, float("nan"), "x", float("inf"), 3.0])
        assert m.n_obs == 2 and m.mean == pytest.approx(2.0)

    def test_known_values(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        m = sample_moments(xs)
        assert m.mean == pytest.approx(3.0)
        assert m.std == pytest.approx(math.sqrt(2.5))
        assert m.skew == pytest.approx(0.0, abs=1e-15)
        assert m.kurt == pytest.approx(1.7)  # 一様離散: m4/m2² = 6.8/4

    def test_skew_sign(self):
        assert sample_moments([0, 0, 0, 0, 10]).skew > 0
        assert sample_moments([0, 0, 0, 0, -10]).skew < 0

    def test_sharpe_ratio_fn(self):
        xs = [0.01, 0.02, -0.005, 0.015]
        m = sample_moments(xs)
        assert sharpe_ratio(xs) == pytest.approx(m.mean / m.std)

    def test_frozen(self):
        m = sample_moments([1.0, 2.0])
        with pytest.raises(Exception):
            m.mean = 0.0  # type: ignore[misc]

    def test_against_scipy(self):
        stats = pytest.importorskip("scipy.stats")
        xs = _normal_returns(300, 0.001, 0.02, seed=7)
        xs = [x + (0.05 if i % 37 == 0 else 0.0) for i, x in enumerate(xs)]
        m = sample_moments(xs)
        assert m.skew == pytest.approx(stats.skew(xs), abs=1e-12)
        assert m.kurt == pytest.approx(stats.kurtosis(xs, fisher=False), abs=1e-12)

    def test_is_return_moments(self):
        assert isinstance(sample_moments([1, 2, 3]), ReturnMoments)


# ===========================================================================
# PSR
# ===========================================================================

class TestProbabilisticSharpeRatio:
    def test_sr_equal_benchmark_is_half(self):
        assert probabilistic_sharpe_ratio(0.1, 100, 0, 3, sr_benchmark=0.1) == pytest.approx(0.5)

    def test_zero_sr_is_half(self):
        assert probabilistic_sharpe_ratio(0.0, 100) == pytest.approx(0.5)

    def test_normal_formula(self):
        sr, t = 0.1, 252
        z = sr * math.sqrt(t - 1) / math.sqrt(1 + 0.5 * sr * sr)
        assert probabilistic_sharpe_ratio(sr, t) == pytest.approx(norm_cdf(z))

    def test_monotonic_in_sr(self):
        vals = [probabilistic_sharpe_ratio(s / 100, 250) for s in range(-10, 20)]
        assert all(a < b for a, b in zip(vals, vals[1:]))

    def test_monotonic_in_t(self):
        vals = [probabilistic_sharpe_ratio(0.05, t) for t in (10, 50, 100, 500, 2000)]
        assert all(a < b for a, b in zip(vals, vals[1:]))

    def test_negative_skew_lowers_psr(self):
        assert probabilistic_sharpe_ratio(0.1, 250, -2, 3) < probabilistic_sharpe_ratio(0.1, 250, 0, 3)

    def test_fat_tails_lower_psr(self):
        assert probabilistic_sharpe_ratio(0.1, 250, 0, 10) < probabilistic_sharpe_ratio(0.1, 250, 0, 3)

    def test_insufficient_obs(self):
        assert probabilistic_sharpe_ratio(0.5, 1) == 0.0

    def test_nonpositive_denominator(self):
        # 1 - g3*SR + (g4-1)/4*SR² <= 0 となる極端な正歪度
        assert probabilistic_sharpe_ratio(1.0, 100, skew=5.0, kurt=3.0) == 0.0

    def test_nan_sr(self):
        assert probabilistic_sharpe_ratio(float("nan"), 100) == 0.0

    def test_range(self):
        for s in (-1, -0.1, 0, 0.1, 1):
            v = probabilistic_sharpe_ratio(s, 100)
            assert 0.0 <= v <= 1.0


# ===========================================================================
# 期待最大 SR
# ===========================================================================

class TestExpectedMaxSharpe:
    def test_n1_zero(self):
        assert expected_max_sharpe(1, 0.01) == 0.0

    def test_n0_zero(self):
        assert expected_max_sharpe(0, 0.01) == 0.0

    def test_zero_variance(self):
        assert expected_max_sharpe(100, 0.0) == 0.0

    def test_nan_variance(self):
        assert expected_max_sharpe(100, float("nan")) == 0.0

    def test_formula(self):
        n, v = 50, 0.04
        expected = math.sqrt(v) * (
            (1 - EULER_GAMMA) * norm_ppf(1 - 1 / n) + EULER_GAMMA * norm_ppf(1 - 1 / (n * math.e))
        )
        assert expected_max_sharpe(n, v) == pytest.approx(expected)

    def test_monotonic_in_n(self):
        vals = [expected_max_sharpe(n, 0.01) for n in (2, 5, 10, 100, 1000, 10000)]
        assert all(a < b for a, b in zip(vals, vals[1:]))

    def test_scales_with_sqrt_variance(self):
        assert expected_max_sharpe(100, 0.04) == pytest.approx(2 * expected_max_sharpe(100, 0.01))

    def test_paper_sr0(self):
        """論文例: 年率 SR0 ≈ 1.789"""
        sr0_ann = expected_max_sharpe(PAPER_N, PAPER_V) * math.sqrt(250)
        assert sr0_ann == pytest.approx(1.7894, abs=1e-3)

    def test_monte_carlo_sanity(self):
        """N 個の独立 N(0,1) の最大値の期待値近似 (N=100 → ≈2.51)"""
        rng = random.Random(0)
        mc = sum(max(rng.gauss(0, 1) for _ in range(100)) for _ in range(3000)) / 3000
        assert expected_max_sharpe(100, 1.0) == pytest.approx(mc, abs=0.05)


# ===========================================================================
# DSR
# ===========================================================================

class TestDeflatedSharpeRatio:
    def test_paper_example(self):
        """Bailey & López de Prado (2014) の数値例: DSR ≈ 0.9004"""
        dsr = deflated_sharpe_ratio(
            PAPER_SR, PAPER_T, PAPER_N, PAPER_SKEW, PAPER_KURT, sr_variance=PAPER_V,
        )
        assert dsr == pytest.approx(0.9004, abs=1e-4)

    def test_n1_equals_psr_zero(self):
        sr, t = 0.08, 500
        assert deflated_sharpe_ratio(sr, t, 1) == pytest.approx(probabilistic_sharpe_ratio(sr, t))

    def test_decreasing_in_n(self):
        vals = [deflated_sharpe_ratio(0.1, 500, n) for n in (1, 2, 10, 100, 1000)]
        assert all(a > b for a, b in zip(vals, vals[1:]))

    def test_dsr_le_psr(self):
        for n in (2, 10, 100):
            assert deflated_sharpe_ratio(0.1, 500, n) <= probabilistic_sharpe_ratio(0.1, 500)

    def test_default_variance_is_estimator(self):
        sr, t, n = 0.1, 300, 20
        v = sharpe_estimator_variance(sr, t)
        assert deflated_sharpe_ratio(sr, t, n) == pytest.approx(
            deflated_sharpe_ratio(sr, t, n, sr_variance=v)
        )

    def test_range(self):
        for sr in (-0.2, 0.0, 0.05, 0.3):
            for n in (1, 10, 1000):
                assert 0.0 <= deflated_sharpe_ratio(sr, 250, n) <= 1.0

    def test_estimator_variance(self):
        assert sharpe_estimator_variance(0.0, 101) == pytest.approx(0.01)
        assert sharpe_estimator_variance(0.1, 1) == float("inf")


class TestCrossTrialVariance:
    def test_basic(self):
        assert cross_trial_sr_variance([1.0, 2.0, 3.0]) == pytest.approx(1.0)

    def test_too_few(self):
        assert cross_trial_sr_variance([1.0]) is None
        assert cross_trial_sr_variance([]) is None

    def test_filters_invalid(self):
        assert cross_trial_sr_variance([1.0, None, float("nan"), 3.0]) == pytest.approx(2.0)


# ===========================================================================
# DsrParams
# ===========================================================================

class TestDsrParams:
    def test_defaults(self):
        p = DsrParams()
        assert p.min_dsr == 0.95 and p.default_n_trials == 1

    def test_frozen(self):
        with pytest.raises(Exception):
            DsrParams().min_dsr = 0.5  # type: ignore[misc]

    @pytest.mark.parametrize("kw", [{"min_dsr": -0.1}, {"min_dsr": 1.1}, {"default_n_trials": 0}])
    def test_invalid(self, kw):
        with pytest.raises(ValueError):
            DsrParams(**kw)

    def test_from_config_empty(self):
        p = DsrParams.from_config(object())
        assert p == DsrParams()

    def test_from_policy_spec(self):
        p = DsrParams.from_config(PolicySpec(min_dsr=0.9, dsr_default_n_trials=5))
        assert p.min_dsr == 0.9 and p.default_n_trials == 5

    def test_from_frost_config(self):
        p = DsrParams.from_config(FrostConfig(min_dsr=0.8))
        assert p.min_dsr == 0.8


# ===========================================================================
# DsrGate
# ===========================================================================

class TestDsrGate:
    def test_strong_signal_passes(self):
        r = DsrGate().check(_normal_returns(1000, 0.002, 0.01), n_trials=10)
        assert r.passed and r.dsr >= 0.95
        assert r.failure_reason is None
        assert r.n_trials_source == N_TRIALS_SOURCE_PROVIDED
        assert r.review_required is False

    def test_noise_fails(self):
        r = DsrGate().check(_normal_returns(500, 0.0, 0.01), n_trials=10)
        assert not r.passed
        assert r.failure_reason.startswith("DSR_BELOW_THRESHOLD")
        assert r.review_required

    def test_many_trials_deflate(self):
        """同じリターンでも N を増やすと不合格になる (選択バイアス補正)"""
        rets = _normal_returns(500, 0.0015, 0.01, seed=3)  # DSR: N=1→0.981 / N=1e4→0.037
        few = DsrGate().check(rets, n_trials=1)
        many = DsrGate().check(rets, n_trials=10000)
        assert few.dsr > many.dsr
        assert few.passed and not many.passed

    def test_assumed_n_requires_review(self):
        r = DsrGate().check(_normal_returns(1000, 0.002, 0.01))
        assert r.passed
        assert r.n_trials == 1
        assert r.n_trials_source == N_TRIALS_SOURCE_ASSUMED
        assert r.review_required
        assert any("ADR-002" in n for n in r.notes)

    def test_default_n_from_params(self):
        r = DsrGate(DsrParams(default_n_trials=50)).check(_normal_returns(300, 0.001, 0.01))
        assert r.n_trials == 50 and r.n_trials_source == N_TRIALS_SOURCE_ASSUMED

    def test_insufficient_obs(self):
        r = DsrGate().check([0.01, 0.02])
        assert not r.passed and r.dsr == 0.0
        assert r.failure_reason.startswith("INSUFFICIENT_OBS")
        assert r.n_obs == 2 < MIN_OBS

    def test_empty(self):
        r = DsrGate().check([])
        assert not r.passed and r.review_required

    def test_invalid_n_trials(self):
        with pytest.raises(ValueError):
            DsrGate().check(_normal_returns(100, 0.001, 0.01), n_trials=0)

    def test_trial_sharpes_variance(self):
        rets = _normal_returns(500, 0.001, 0.01)
        r = DsrGate().check(rets, n_trials=20, trial_sharpes=[0.01, 0.05, -0.02, 0.03])
        assert r.sr_variance_source == "cross_trial"
        assert r.sr_variance == pytest.approx(cross_trial_sr_variance([0.01, 0.05, -0.02, 0.03]))
        assert r.n_trials == 20

    def test_trial_sharpes_lower_bound_n(self):
        r = DsrGate().check(_normal_returns(500, 0.001, 0.01), trial_sharpes=[0.01 * i for i in range(30)])
        assert r.n_trials == 30

    def test_trial_sharpes_do_not_override_provided_n(self):
        r = DsrGate().check(_normal_returns(500, 0.001, 0.01), n_trials=5, trial_sharpes=[0.1, 0.2, 0.3])
        assert r.n_trials == 5

    def test_trial_sharpes_too_few(self):
        r = DsrGate().check(_normal_returns(500, 0.001, 0.01), n_trials=5, trial_sharpes=[0.1])
        assert r.sr_variance_source == "estimator"

    def test_provided_variance(self):
        r = DsrGate().check(_normal_returns(500, 0.001, 0.01), n_trials=5, sr_variance=0.002)
        assert r.sr_variance_source == "provided" and r.sr_variance == 0.002

    def test_check_stats_paper(self):
        r = DsrGate(DsrParams(min_dsr=0.90)).check_stats(
            PAPER_SR, PAPER_T, PAPER_SKEW, PAPER_KURT, n_trials=PAPER_N, sr_variance=PAPER_V,
        )
        assert r.dsr == pytest.approx(0.9004, abs=1e-4)
        assert r.passed  # 0.9004 >= 0.90
        assert not DsrGate().check_stats(
            PAPER_SR, PAPER_T, PAPER_SKEW, PAPER_KURT, n_trials=PAPER_N, sr_variance=PAPER_V,
        ).passed  # 0.9004 < 0.95

    def test_psr_zero_reported(self):
        r = DsrGate().check(_normal_returns(500, 0.001, 0.01), n_trials=100)
        assert r.psr_zero >= r.dsr

    def test_extreme_skew_note(self):
        r = DsrGate().check_stats(1.0, 100, skew=5.0, kurt=3.0, n_trials=1)
        assert r.dsr == 0.0 and not r.passed
        assert any("非正" in n for n in r.notes)

    def test_threshold_boundary(self):
        rets = _normal_returns(500, 0.001, 0.01)
        dsr = DsrGate().check(rets, n_trials=3).dsr
        assert DsrGate(DsrParams(min_dsr=dsr)).check(rets, n_trials=3).passed

    def test_deterministic(self):
        rets = _normal_returns(300, 0.001, 0.01)
        a = DsrGate().check(rets, n_trials=7).to_dict()
        b = DsrGate().check(list(rets), n_trials=7).to_dict()
        assert a == b

    def test_from_config(self):
        g = DsrGate.from_config(PolicySpec(min_dsr=0.8))
        assert g.threshold == 0.8

    def test_result_type(self):
        assert isinstance(DsrGate().check([0.1, 0.2, 0.3]), DsrGateResult)


class TestDsrGateResultToDict:
    def test_keys(self):
        d = DsrGate().check(_normal_returns(100, 0.001, 0.01), n_trials=3).to_dict()
        assert d["gate"] == "dsr"
        for k in ("passed", "dsr", "psr_zero", "sharpe", "sr0", "n_obs", "n_trials",
                  "n_trials_source", "skew", "kurt", "sr_variance", "sr_variance_source",
                  "threshold", "review_required", "failure_reason", "notes"):
            assert k in d

    def test_json_serializable(self):
        import json
        json.dumps(DsrGate().check(_normal_returns(100, 0.001, 0.01)).to_dict())

    def test_inf_becomes_none(self):
        r = DsrGate().check_stats(0.1, 1, n_trials=3)
        assert r.to_dict()["sr_variance"] is None


class TestCheckDsrGateWrapper:
    def test_equivalent(self):
        rets = _normal_returns(400, 0.001, 0.01)
        assert check_dsr_gate(rets, n_trials=4, min_dsr=0.9).to_dict() == \
            DsrGate(DsrParams(min_dsr=0.9)).check(rets, n_trials=4).to_dict()

    def test_default_threshold(self):
        assert check_dsr_gate([0.1, 0.2, 0.3]).threshold == 0.95


# ===========================================================================
# PolicySpec / FrostConfig 統合
# ===========================================================================

class TestPolicyIntegration:
    def test_policy_defaults(self):
        s = PolicySpec()
        assert s.min_dsr == 0.95 and s.dsr_default_n_trials == 1

    def test_policy_hash_changes(self):
        assert PolicySpec(min_dsr=0.9).policy_hash != PolicySpec().policy_hash
        assert PolicySpec(dsr_default_n_trials=10).policy_hash != PolicySpec().policy_hash

    def test_policy_roundtrip(self):
        s = PolicySpec(min_dsr=0.85, dsr_default_n_trials=12)
        r = PolicySpec.from_dict(s.to_dict())
        assert r.min_dsr == 0.85 and r.dsr_default_n_trials == 12
        assert r.policy_hash == s.policy_hash

    def test_policy_in_hard_gates(self):
        hg = PolicySpec().to_dict()["hard_gates"]
        assert "min_dsr" in hg and "dsr_default_n_trials" in hg

    @pytest.mark.parametrize("kw", [{"min_dsr": 1.5}, {"min_dsr": -0.1}, {"dsr_default_n_trials": 0}])
    def test_policy_validate(self, kw):
        with pytest.raises(ValueError):
            PolicySpec(**kw).validate()

    def test_frost_config_validate(self):
        with pytest.raises(ValueError):
            FrostConfig(min_dsr=2.0).validate()

    def test_env(self, monkeypatch):
        from analytics.python.frost.policy_spec import load_policy_spec, _POLICY_ENV_VARS
        monkeypatch.setenv("FROST_MIN_DSR", "0.9")
        monkeypatch.setenv("FROST_DSR_DEFAULT_N_TRIALS", "25")
        s = load_policy_spec()
        assert s.min_dsr == 0.9 and s.dsr_default_n_trials == 25
        assert "FROST_MIN_DSR" in _POLICY_ENV_VARS

    def test_bridge_roundtrip(self):
        from analytics.python.frost.policy_spec import (
            policy_spec_from_frost_config, policy_spec_to_frost_config,
        )
        s = PolicySpec(min_dsr=0.9, dsr_default_n_trials=3)
        back = policy_spec_from_frost_config(policy_spec_to_frost_config(s))
        assert back.min_dsr == 0.9 and back.dsr_default_n_trials == 3


# ===========================================================================
# 設計原則 (純 Python / ADR-001)
# ===========================================================================

class TestDesignPrinciples:
    def test_no_numpy(self):
        assert "numpy" not in frost_dsr.__dict__ and "np" not in frost_dsr.__dict__

    def test_no_statistics(self):
        assert "statistics" not in frost_dsr.__dict__
        src = inspect.getsource(frost_dsr)
        assert "import statistics" not in src
        assert "import numpy" not in src

    def test_no_scipy(self):
        assert "import scipy" not in inspect.getsource(frost_dsr)
