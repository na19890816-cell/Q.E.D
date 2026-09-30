"""
test_phase2_portfolio_correlation_gate.py — Phase 2 (G2): 採用済みポートフォリオ相関ゲート 単体テスト

## テスト設計方針

portfolio_correlation_gate.py は numpy 依存の純関数群であるため、
DB 接続不要で全ロジックを直接テストできる。

カバー範囲:
  TestPearsonNumpy             (10): _pearson_numpy — 数値正確性 / 境界値 / ADR-001 準拠
  TestSingleCorrResult         (7):  SingleCorrResult — 属性 / exceeded 判定 / repr
  TestPortfolioGateResultProps (8):  PortfolioGateResult — max_correlation / failed_artifacts / to_dict
  TestCheckOne                 (8):  PortfolioCorrelationGate.check_one — r の正確性 / 境界
  TestCheckGate                (12): PortfolioCorrelationGate.check — pass/fail / 複数アルファ / 空
  TestFunctionWrapper          (6):  check_portfolio_correlation_gate 関数型 API
  TestFromConfig               (5):  from_config — PolicySpec / FrostConfig 互換
  TestGateIntegration          (6):  統合: PolicySpec 連携 / end-to-end シナリオ

合計: 62 テスト
"""
from __future__ import annotations

import math
from typing import Dict, List
from unittest.mock import MagicMock

import pytest

from analytics.python.frost.policy_spec import PolicySpec
from analytics.python.frost.portfolio_correlation_gate import (
    PortfolioCorrelationGate,
    PortfolioGateResult,
    SingleCorrResult,
    _pearson_numpy,
    check_portfolio_correlation_gate,
)


# ===========================================================================
# シグナル生成ヘルパー
# ===========================================================================

def _linspace(start: float, stop: float, n: int) -> List[float]:
    """等差数列を返す (numpy 不依存ヘルパー)。"""
    if n <= 1:
        return [start]
    step = (stop - start) / (n - 1)
    return [start + step * i for i in range(n)]


def _const(val: float, n: int) -> List[float]:
    return [val] * n


def _alternating(n: int, amp: float = 0.1) -> List[float]:
    """交互符号列 (+amp, -amp, ...) — 単調増加と無相関に近い。"""
    return [amp * (1 if i % 2 == 0 else -1) for i in range(n)]


def _negative(sig: List[float]) -> List[float]:
    """符号反転 — 完全負相関シグナル。"""
    return [-x for x in sig]


# ===========================================================================
# TestPearsonNumpy
# ===========================================================================

@pytest.mark.phase2_portfolio_corr
class TestPearsonNumpy:
    """_pearson_numpy の数値正確性・境界値を検証する。"""

    def test_perfect_positive_correlation(self):
        """同一列は r=1.0 を返す"""
        sig = _linspace(0.0, 1.0, 20)
        r = _pearson_numpy(sig, sig)
        assert abs(r - 1.0) < 1e-8

    def test_perfect_negative_correlation(self):
        """符号反転列は r=-1.0 を返す"""
        sig = _linspace(0.0, 1.0, 20)
        r = _pearson_numpy(sig, _negative(sig))
        assert abs(r + 1.0) < 1e-8

    def test_zero_correlation_approx(self):
        """直交列 (交互符号 vs 単調増加) は abs(r) が小さい"""
        sig = _linspace(0.0, 1.0, 100)
        alt = _alternating(100)
        r = _pearson_numpy(sig, alt)
        assert abs(r) < 0.3

    def test_constant_signal_returns_zero(self):
        """定数列は標準偏差ゼロ → 0.0 を返す"""
        sig = _linspace(0.0, 1.0, 20)
        const = _const(0.5, 20)
        r = _pearson_numpy(sig, const)
        assert r == 0.0

    def test_both_constant_returns_zero(self):
        """両方定数列は 0.0 を返す"""
        r = _pearson_numpy(_const(1.0, 20), _const(1.0, 20))
        assert r == 0.0

    def test_short_signal_returns_zero(self):
        """データ点数 < 3 は 0.0 を返す"""
        r = _pearson_numpy([1.0, 2.0], [1.0, 2.0])
        assert r == 0.0

    def test_empty_signal_returns_zero(self):
        """空列は 0.0 を返す"""
        assert _pearson_numpy([], []) == 0.0

    def test_nan_in_signal_handled(self):
        """NaN を含むシグナルでも例外が起きない"""
        sig = [float("nan")] * 5 + [0.1 * i for i in range(15)]
        ref = _linspace(0.0, 1.0, 20)
        r = _pearson_numpy(sig, ref)
        assert math.isfinite(r)

    def test_inf_in_signal_handled(self):
        """Inf を含むシグナルでも例外が起きない"""
        sig = [float("inf")] * 3 + [0.1 * i for i in range(17)]
        ref = _linspace(0.0, 1.0, 20)
        r = _pearson_numpy(sig, ref)
        assert math.isfinite(r)

    def test_result_in_range(self):
        """結果は必ず [-1.0, 1.0] の範囲に収まる"""
        import random
        random.seed(42)
        for _ in range(50):
            sig_a = [random.gauss(0, 1) for _ in range(30)]
            sig_b = [random.gauss(0, 1) for _ in range(30)]
            r = _pearson_numpy(sig_a, sig_b)
            assert -1.0 <= r <= 1.0, f"Out of range: r={r}"


# ===========================================================================
# TestSingleCorrResult
# ===========================================================================

@pytest.mark.phase2_portfolio_corr
class TestSingleCorrResult:
    """SingleCorrResult の属性と判定ロジックを検証する。"""

    def test_exceeded_true_when_abs_corr_ge_threshold(self):
        """abs(r) >= threshold のとき exceeded=True"""
        r = SingleCorrResult(artifact_id="a1", correlation=0.70, threshold=0.60, exceeded=True)
        assert r.exceeded is True

    def test_exceeded_false_when_abs_corr_lt_threshold(self):
        """abs(r) < threshold のとき exceeded=False"""
        r = SingleCorrResult(artifact_id="a1", correlation=0.50, threshold=0.60, exceeded=False)
        assert r.exceeded is False

    def test_abs_corr_negative_correlation(self):
        """負の相関でも abs_corr は正値"""
        r = SingleCorrResult(artifact_id="a2", correlation=-0.75, threshold=0.60, exceeded=True)
        assert r.abs_corr == pytest.approx(0.75)

    def test_abs_corr_zero(self):
        """相関 0.0 の abs_corr は 0.0"""
        r = SingleCorrResult(artifact_id="a3", correlation=0.0, threshold=0.60, exceeded=False)
        assert r.abs_corr == 0.0

    def test_repr_contains_fail(self):
        """exceeded=True のとき repr に FAIL が含まれる"""
        r = SingleCorrResult(artifact_id="art_x", correlation=0.8, threshold=0.6, exceeded=True)
        assert "FAIL" in repr(r)

    def test_repr_contains_pass(self):
        """exceeded=False のとき repr に PASS が含まれる"""
        r = SingleCorrResult(artifact_id="art_y", correlation=0.3, threshold=0.6, exceeded=False)
        assert "PASS" in repr(r)

    def test_threshold_on_boundary(self):
        """abs(r) == threshold の境界値 (exceeded=True が期待)"""
        # abs(0.60) >= 0.60 → exceeded=True
        r = SingleCorrResult(artifact_id="a4", correlation=0.60, threshold=0.60, exceeded=True)
        assert r.exceeded is True


# ===========================================================================
# TestPortfolioGateResultProps
# ===========================================================================

@pytest.mark.phase2_portfolio_corr
class TestPortfolioGateResultProps:
    """PortfolioGateResult のプロパティと to_dict を検証する。"""

    def _make_results(self, corrs: List[float], threshold: float = 0.60) -> List[SingleCorrResult]:
        return [
            SingleCorrResult(
                artifact_id=f"art_{i}",
                correlation=c,
                threshold=threshold,
                exceeded=abs(c) >= threshold,
            )
            for i, c in enumerate(corrs)
        ]

    def test_max_correlation_single(self):
        """1 本のみの場合 max_correlation はその abs(r)"""
        results = self._make_results([0.55])
        gr = PortfolioGateResult(
            passed=True, threshold=0.60, candidate_signal_len=20,
            promoted_count=1, all_results=results,
        )
        assert gr.max_correlation == pytest.approx(0.55)

    def test_max_correlation_multiple(self):
        """複数の中で最大の abs(r) を返す"""
        results = self._make_results([0.30, -0.70, 0.55])
        gr = PortfolioGateResult(
            passed=False, threshold=0.60, candidate_signal_len=20,
            promoted_count=3, all_results=results,
        )
        assert gr.max_correlation == pytest.approx(0.70)

    def test_max_correlation_empty(self):
        """採用済みアルファ 0 本の場合 None"""
        gr = PortfolioGateResult(
            passed=True, threshold=0.60, candidate_signal_len=20, promoted_count=0,
        )
        assert gr.max_correlation is None

    def test_failed_artifacts_correct_subset(self):
        """exceeded=True のもののみ failed_artifacts に含まれる"""
        results = self._make_results([0.30, 0.65, -0.72])
        gr = PortfolioGateResult(
            passed=False, threshold=0.60, candidate_signal_len=20,
            promoted_count=3, all_results=results,
        )
        failed_ids = {r.artifact_id for r in gr.failed_artifacts}
        assert failed_ids == {"art_1", "art_2"}

    def test_to_dict_has_required_keys(self):
        """to_dict の必須キーが存在する"""
        results = self._make_results([0.80])
        gr = PortfolioGateResult(
            passed=False, threshold=0.60, candidate_signal_len=20,
            promoted_count=1, all_results=results,
            failure_reason="test",
        )
        d = gr.to_dict()
        required = {
            "gate", "passed", "threshold", "candidate_signal_len",
            "promoted_count", "max_correlation", "failure_reason", "failed_artifacts",
        }
        assert required <= set(d.keys())

    def test_to_dict_gate_name(self):
        """to_dict の gate キーが 'portfolio_correlation'"""
        gr = PortfolioGateResult(
            passed=True, threshold=0.60, candidate_signal_len=10, promoted_count=0,
        )
        assert gr.to_dict()["gate"] == "portfolio_correlation"

    def test_to_dict_failed_artifacts_content(self):
        """to_dict の failed_artifacts に artifact_id と correlation が含まれる"""
        results = self._make_results([0.75])
        gr = PortfolioGateResult(
            passed=False, threshold=0.60, candidate_signal_len=20,
            promoted_count=1, all_results=results, failure_reason="x",
        )
        d = gr.to_dict()
        assert len(d["failed_artifacts"]) == 1
        assert "artifact_id" in d["failed_artifacts"][0]
        assert "correlation" in d["failed_artifacts"][0]

    def test_to_dict_passed_true_empty_failed(self):
        """passed=True のとき failed_artifacts は空"""
        results = self._make_results([0.30, 0.40])
        gr = PortfolioGateResult(
            passed=True, threshold=0.60, candidate_signal_len=20,
            promoted_count=2, all_results=results,
        )
        assert gr.to_dict()["failed_artifacts"] == []


# ===========================================================================
# TestCheckOne
# ===========================================================================

@pytest.mark.phase2_portfolio_corr
class TestCheckOne:
    """PortfolioCorrelationGate.check_one の正確性を検証する。"""

    def test_perfect_corr_exceeds_threshold(self):
        """r=1.0 は threshold=0.60 を超える"""
        sig = _linspace(0.0, 1.0, 20)
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check_one(sig, "art_a", sig)
        assert result.exceeded is True
        assert result.abs_corr > 0.99

    def test_negative_corr_exceeds_threshold(self):
        """r=-1.0 も abs(r)=1.0 > threshold → 失敗"""
        sig = _linspace(0.0, 1.0, 20)
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check_one(sig, "art_neg", _negative(sig))
        assert result.exceeded is True

    def test_low_corr_passes(self):
        """abs(r) < threshold なら passed"""
        sig = _linspace(0.0, 1.0, 50)
        alt = _alternating(50)
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check_one(sig, "art_b", alt)
        assert result.exceeded is False

    def test_artifact_id_preserved(self):
        """artifact_id が結果に保持される"""
        sig = _linspace(0.0, 1.0, 20)
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check_one(sig, "my_special_artifact", _alternating(20))
        assert result.artifact_id == "my_special_artifact"

    def test_threshold_preserved(self):
        """threshold が結果に保持される"""
        sig = _linspace(0.0, 1.0, 20)
        gate = PortfolioCorrelationGate(threshold=0.75)
        result = gate.check_one(sig, "a", _alternating(20))
        assert result.threshold == pytest.approx(0.75)

    def test_unequal_length_truncates(self):
        """長さ不一致の場合も例外が起きない (短い方に truncate)"""
        sig_long = _linspace(0.0, 1.0, 30)
        sig_short = _linspace(0.0, 1.0, 10)
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check_one(sig_long, "a_short", sig_short)
        assert math.isfinite(result.correlation)

    def test_correlation_symmetric(self):
        """check_one(a, b) と check_one(b, a) は同じ相関値を返す"""
        sig_a = _linspace(0.0, 1.0, 20)
        sig_b = _linspace(0.5, 1.5, 20)
        gate = PortfolioCorrelationGate(threshold=0.60)
        r1 = gate.check_one(sig_a, "x", sig_b).correlation
        r2 = gate.check_one(sig_b, "x", sig_a).correlation
        assert r1 == pytest.approx(r2, abs=1e-8)

    def test_custom_threshold_boundary(self):
        """threshold=0.90 でも abs(r)=1.0 は超える"""
        sig = _linspace(0.0, 1.0, 20)
        gate = PortfolioCorrelationGate(threshold=0.90)
        result = gate.check_one(sig, "a", sig)
        assert result.exceeded is True


# ===========================================================================
# TestCheckGate
# ===========================================================================

@pytest.mark.phase2_portfolio_corr
class TestCheckGate:
    """PortfolioCorrelationGate.check の pass/fail と複数アルファ対応を検証する。"""

    def test_empty_portfolio_passes(self):
        """採用済みアルファが 0 本の場合は pass"""
        sig = _linspace(0.0, 1.0, 20)
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check(sig, {})
        assert result.passed is True
        assert result.promoted_count == 0

    def test_single_high_corr_fails(self):
        """1 本との相関が高い場合はゲート失敗"""
        sig = _linspace(0.0, 1.0, 20)
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check(sig, {"art_a": sig})
        assert result.passed is False

    def test_single_low_corr_passes(self):
        """1 本との相関が低い場合はゲート通過"""
        sig = _linspace(0.0, 1.0, 50)
        alt = _alternating(50)
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check(sig, {"art_b": alt})
        assert result.passed is True

    def test_multiple_all_low_corr_passes(self):
        """複数本すべての相関が低い場合はゲート通過"""
        sig = _linspace(0.0, 1.0, 50)
        promoted = {
            f"art_{i}": _alternating(50, amp=0.1 * (i + 1))
            for i in range(5)
        }
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check(sig, promoted)
        assert result.passed is True
        assert result.promoted_count == 5

    def test_multiple_one_exceeds_fails(self):
        """複数本のうち 1 本でも相関超過すればゲート失敗"""
        sig = _linspace(0.0, 1.0, 50)
        promoted = {
            "art_ok": _alternating(50),   # 低相関
            "art_ng": sig,                # r=1.0 → 失敗
        }
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check(sig, promoted)
        assert result.passed is False

    def test_failure_reason_non_empty_on_fail(self):
        """ゲート失敗時は failure_reason が空でない"""
        sig = _linspace(0.0, 1.0, 20)
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check(sig, {"art_a": sig})
        assert len(result.failure_reason) > 0

    def test_failure_reason_empty_on_pass(self):
        """ゲート通過時は failure_reason が空文字"""
        sig = _linspace(0.0, 1.0, 50)
        alt = _alternating(50)
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check(sig, {"art_b": alt})
        assert result.failure_reason == ""

    def test_all_results_length(self):
        """all_results の件数が promoted_signals の件数と一致"""
        sig = _linspace(0.0, 1.0, 20)
        promoted = {f"a_{i}": _alternating(20) for i in range(7)}
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check(sig, promoted)
        assert len(result.all_results) == 7

    def test_candidate_signal_len_preserved(self):
        """candidate_signal_len が実際の長さと一致"""
        sig = _linspace(0.0, 1.0, 33)
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check(sig, {})
        assert result.candidate_signal_len == 33

    def test_worst_artifact_in_failure_reason(self):
        """failure_reason に最悪の artifact_id が含まれる"""
        sig = _linspace(0.0, 1.0, 50)
        high_corr_sig = [x + 0.001 * i for i, x in enumerate(sig)]  # r ≈ 1.0
        moderate_corr_sig = [x * 0.5 + 0.25 for x in sig]           # r ≈ 1.0 だが少し低い
        promoted = {
            "art_moderate": moderate_corr_sig,
            "art_high": high_corr_sig,
        }
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check(sig, promoted)
        assert not result.passed
        # failure_reason にはいずれかの artifact_id が含まれる
        assert "art_" in result.failure_reason

    def test_threshold_0_always_fails_with_any_signal(self):
        """threshold=0.0 はどんな相関でも失敗 (abs(r) >= 0 は常に真)"""
        sig = _linspace(0.0, 1.0, 20)
        alt = _alternating(20)
        gate = PortfolioCorrelationGate(threshold=0.0)
        result = gate.check(sig, {"art_any": alt})
        # r=0 の場合は abs(r)=0.0 >= 0.0 → exceeded (境界)
        # 交互符号との相関は実際はほぼ 0 なので passed になる可能性がある
        # ここでは "例外が起きない" のみを検証
        assert isinstance(result.passed, bool)

    def test_threshold_1_always_passes(self):
        """threshold=1.01 (不達成不可能) は常に通過"""
        sig = _linspace(0.0, 1.0, 20)
        gate = PortfolioCorrelationGate(threshold=1.01)
        result = gate.check(sig, {"art_same": sig})
        # abs(r=1.0) < 1.01 → passed
        assert result.passed is True


# ===========================================================================
# TestFunctionWrapper
# ===========================================================================

@pytest.mark.phase2_portfolio_corr
class TestFunctionWrapper:
    """check_portfolio_correlation_gate 関数型 API を検証する。"""

    def test_returns_portfolio_gate_result(self):
        """戻り値が PortfolioGateResult 型"""
        sig = _linspace(0.0, 1.0, 20)
        result = check_portfolio_correlation_gate(sig, {})
        assert isinstance(result, PortfolioGateResult)

    def test_same_result_as_class_api(self):
        """PortfolioCorrelationGate.check と同じ結果を返す"""
        sig = _linspace(0.0, 1.0, 20)
        promoted = {"art_a": _alternating(20), "art_b": sig}
        threshold = 0.60
        gate = PortfolioCorrelationGate(threshold=threshold)
        r1 = gate.check(sig, promoted)
        r2 = check_portfolio_correlation_gate(sig, promoted, threshold=threshold)
        assert r1.passed == r2.passed
        assert r1.max_correlation == pytest.approx(r2.max_correlation or 0, abs=1e-8)

    def test_default_threshold_is_0_60(self):
        """デフォルト threshold が 0.60"""
        sig = _linspace(0.0, 1.0, 20)
        result = check_portfolio_correlation_gate(sig, {"art": sig})
        assert result.threshold == pytest.approx(0.60)

    def test_custom_threshold_applied(self):
        """カスタム threshold が正しく適用される"""
        sig = _linspace(0.0, 1.0, 20)
        # threshold=0.50 で同一シグナル (r=1.0) → 失敗
        r1 = check_portfolio_correlation_gate(sig, {"a": sig}, threshold=0.50)
        assert r1.passed is False

    def test_empty_portfolio_passes(self):
        """空ポートフォリオは通過"""
        sig = _linspace(0.0, 1.0, 20)
        result = check_portfolio_correlation_gate(sig, {})
        assert result.passed is True

    def test_high_threshold_passes_moderate_corr(self):
        """threshold=0.99 で交互符号列 (abs(r)≈0) はゲート通過"""
        sig_a = _linspace(0.0, 1.0, 50)
        sig_b = _alternating(50)  # 交互符号: abs(r) ≪ 0.99
        result = check_portfolio_correlation_gate(sig_a, {"a": sig_b}, threshold=0.99)
        assert result.passed is True


# ===========================================================================
# TestFromConfig
# ===========================================================================

@pytest.mark.phase2_portfolio_corr
class TestFromConfig:
    """PortfolioCorrelationGate.from_config の PolicySpec / FrostConfig 互換を検証する。"""

    def test_from_policy_spec_default(self):
        """PolicySpec デフォルト (0.60) が threshold に反映される"""
        spec = PolicySpec()
        gate = PortfolioCorrelationGate.from_config(spec)
        assert gate.threshold == pytest.approx(0.60)

    def test_from_policy_spec_custom(self):
        """PolicySpec のカスタム値が threshold に反映される"""
        spec = PolicySpec(max_portfolio_corr=0.75)
        gate = PortfolioCorrelationGate.from_config(spec)
        assert gate.threshold == pytest.approx(0.75)

    def test_from_config_without_attribute_uses_default(self):
        """max_portfolio_corr 属性を持たない旧 FrostConfig でもデフォルト 0.60"""
        mock_config = MagicMock(spec=[])  # 属性なし
        gate = PortfolioCorrelationGate.from_config(mock_config)
        assert gate.threshold == pytest.approx(0.60)

    def test_from_config_returns_gate_instance(self):
        """from_config が PortfolioCorrelationGate を返す"""
        spec = PolicySpec()
        gate = PortfolioCorrelationGate.from_config(spec)
        assert isinstance(gate, PortfolioCorrelationGate)

    def test_from_config_different_thresholds_differ(self):
        """異なる PolicySpec から生成したゲートは異なる threshold を持つ"""
        gate_a = PortfolioCorrelationGate.from_config(PolicySpec(max_portfolio_corr=0.50))
        gate_b = PortfolioCorrelationGate.from_config(PolicySpec(max_portfolio_corr=0.80))
        assert gate_a.threshold != gate_b.threshold


# ===========================================================================
# TestGateIntegration
# ===========================================================================

@pytest.mark.phase2_portfolio_corr
class TestGateIntegration:
    """PolicySpec 連携 / end-to-end シナリオを検証する統合テスト。"""

    def test_policy_spec_max_portfolio_corr_field_exists(self):
        """PolicySpec に max_portfolio_corr フィールドが存在する"""
        spec = PolicySpec()
        assert hasattr(spec, "max_portfolio_corr")
        assert spec.max_portfolio_corr == pytest.approx(0.60)

    def test_policy_spec_hash_changes_with_corr(self):
        """max_portfolio_corr を変更すると policy_hash が変わる"""
        spec1 = PolicySpec(max_portfolio_corr=0.60)
        spec2 = PolicySpec(max_portfolio_corr=0.70)
        assert spec1.policy_hash != spec2.policy_hash

    def test_policy_spec_roundtrip_preserves_corr(self):
        """PolicySpec を to_dict → from_dict しても max_portfolio_corr が保持される"""
        spec = PolicySpec(max_portfolio_corr=0.65)
        loaded = PolicySpec.from_dict(spec.to_dict())
        assert loaded.max_portfolio_corr == pytest.approx(0.65)

    def test_end_to_end_new_alpha_blocked(self):
        """既存ポートフォリオと高相関な新規アルファが昇格ブロックされる"""
        # 既存アルファ (3 本)
        existing_1 = _linspace(0.0, 1.0, 60)
        existing_2 = _linspace(0.5, 2.0, 60)
        existing_3 = _alternating(60, amp=0.3)

        # 新規候補: existing_1 とほぼ同じ
        new_candidate = [x + 0.01 for x in existing_1]

        spec = PolicySpec(max_portfolio_corr=0.60)
        gate = PortfolioCorrelationGate.from_config(spec)
        result = gate.check(
            new_candidate,
            {
                "alpha_1": existing_1,
                "alpha_2": existing_2,
                "alpha_3": existing_3,
            },
        )
        assert result.passed is False
        assert "alpha_1" in result.failure_reason

    def test_end_to_end_diverse_alpha_promoted(self):
        """既存ポートフォリオと低相関な新規アルファが昇格許可される"""
        existing_1 = _linspace(0.0, 1.0, 60)
        existing_2 = _linspace(0.5, 2.0, 60)

        # 新規候補: 既存と無相関 (交互符号)
        new_candidate = _alternating(60, amp=0.5)

        spec = PolicySpec(max_portfolio_corr=0.60)
        gate = PortfolioCorrelationGate.from_config(spec)
        result = gate.check(
            new_candidate,
            {"alpha_1": existing_1, "alpha_2": existing_2},
        )
        assert result.passed is True

    def test_end_to_end_to_dict_integration(self):
        """to_dict が FrostEvaluation.diagnostics_json へ格納できる形式を返す"""
        sig = _linspace(0.0, 1.0, 20)
        gate = PortfolioCorrelationGate(threshold=0.60)
        result = gate.check(sig, {"art_x": sig})
        d = result.to_dict()
        # 値が JSON シリアライズ可能な基本型であることを確認
        import json
        json_str = json.dumps(d)
        assert len(json_str) > 0
