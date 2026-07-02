"""
test_phase8_meta_validator.py
-----------------------------
Phase 8 MetaValidator の単体テスト。

対象:
  analytics/python/frost/meta_validator.py
  analytics/python/frost/frost_metrics.py   (robust_normalize pure Python 化)
  analytics/python/frost/frost_stability.py (stdev_safe pure Python 化)

マーカー:
  @pytest.mark.meta_validation — Phase 8 メタ検証テスト

テストクラス構成 (9 クラス, 50 テスト):
  TestValidationIssue           (6)  — ValidationIssue データクラス
  TestMetaValidationResult      (7)  — MetaValidationResult データクラス
  TestMetaValidatorInit         (3)  — __init__ / pbo_threshold 設定
  TestMetaValidatorR01          (7)  — R01: SELECTED + hard_gate_passed=False
  TestMetaValidatorR02          (7)  — R02: decision_rank 単調性
  TestMetaValidatorR03          (5)  — R03: suppressed_by_dedup + SELECTED
  TestMetaValidatorR04          (5)  — R04: pbo_score 超過警告
  TestMetaValidatorR05          (5)  — R05: frost_score 負値
  TestMetaValidatorIntegration  (5)  — validate() 統合 + D8 負債解消確認
  TestMetaValidatorSignatureGuard (5) — 公開 API シグネチャ不変テスト
"""
from __future__ import annotations

import inspect
import math
import sys
import os

import pytest

# ── パス設定 ─────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../analytics/python/frost"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../analytics/python"))

from frost_contracts import FrostDecision, FrostEvaluation
from meta_validator import (
    DEFAULT_PBO_THRESHOLD,
    RULE_R01,
    RULE_R02,
    RULE_R03,
    RULE_R04,
    RULE_R05,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    MetaValidationResult,
    MetaValidator,
    ValidationIssue,
)
from frost_metrics import robust_normalize
from frost_stability import _stdev_safe


# ---------------------------------------------------------------------------
# フィクスチャ / ヘルパー
# ---------------------------------------------------------------------------

def _eval(
    candidate_id: str = "C001",
    frost_score: float = 0.5,
    hard_gate_passed: bool = True,
    hard_gate_failures: list[str] | None = None,
    pbo_score: float = 0.1,
) -> FrostEvaluation:
    """テスト用 FrostEvaluation を生成するファクトリ。"""
    ev = FrostEvaluation()
    ev.candidate_id = candidate_id
    ev.frost_score = frost_score
    ev.hard_gate_passed = hard_gate_passed
    ev.hard_gate_failures = hard_gate_failures or []
    ev.pbo_score = pbo_score
    return ev


def _dec(
    candidate_id: str = "C001",
    decision: str = "SELECTED",
    decision_rank: int | None = None,
    frost_score: float = 0.5,
    suppressed_by_dedup: bool = False,
    near_duplicate_of: str | None = None,
    gate_failures: list[str] | None = None,
) -> FrostDecision:
    """テスト用 FrostDecision を生成するファクトリ。"""
    dec = FrostDecision()
    dec.candidate_id = candidate_id
    dec.decision = decision
    dec.decision_rank = decision_rank
    dec.frost_score = frost_score
    dec.suppressed_by_dedup = suppressed_by_dedup
    dec.near_duplicate_of = near_duplicate_of
    dec.gate_failures = gate_failures or []
    return dec


# ---------------------------------------------------------------------------
# TestValidationIssue (6 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.meta_validation
class TestValidationIssue:
    """ValidationIssue データクラスの基本挙動テスト。"""

    def test_error_severity_is_error(self):
        issue = ValidationIssue(SEVERITY_ERROR, RULE_R01, "C001", "detail")
        assert issue.is_error() is True
        assert issue.is_warning() is False

    def test_warning_severity_is_warning(self):
        issue = ValidationIssue(SEVERITY_WARNING, RULE_R04, "C001", "detail")
        assert issue.is_warning() is True
        assert issue.is_error() is False

    def test_fields_are_stored_correctly(self):
        issue = ValidationIssue(SEVERITY_ERROR, RULE_R02, "C999", "test detail")
        assert issue.severity == SEVERITY_ERROR
        assert issue.rule_name == RULE_R02
        assert issue.candidate_id == "C999"
        assert issue.detail == "test detail"

    def test_rule_name_stored_as_string(self):
        for rule in [RULE_R01, RULE_R02, RULE_R03, RULE_R04, RULE_R05]:
            issue = ValidationIssue(SEVERITY_ERROR, rule, "X", "d")
            assert issue.rule_name == rule

    def test_empty_candidate_id_allowed(self):
        """R02 など横断ルールは空 candidate_id を許可する。"""
        issue = ValidationIssue(SEVERITY_ERROR, RULE_R02, "", "multi-candidate")
        assert issue.candidate_id == ""

    def test_detail_message_preserved(self):
        msg = "very long detail message with special chars: <>&\"'"
        issue = ValidationIssue(SEVERITY_WARNING, RULE_R04, "C1", msg)
        assert issue.detail == msg


# ---------------------------------------------------------------------------
# TestMetaValidationResult (7 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.meta_validation
class TestMetaValidationResult:
    """MetaValidationResult データクラスのテスト。"""

    def test_default_passed_true(self):
        result = MetaValidationResult()
        assert result.passed is True
        assert result.error_count == 0
        assert result.warning_count == 0
        assert result.issues == []

    def test_errors_filter(self):
        issues = [
            ValidationIssue(SEVERITY_ERROR, RULE_R01, "C1", "e"),
            ValidationIssue(SEVERITY_WARNING, RULE_R04, "C2", "w"),
            ValidationIssue(SEVERITY_ERROR, RULE_R05, "C3", "e2"),
        ]
        result = MetaValidationResult(issues=issues, error_count=2, warning_count=1, passed=False)
        errors = result.errors()
        assert len(errors) == 2
        assert all(i.is_error() for i in errors)

    def test_warnings_filter(self):
        issues = [
            ValidationIssue(SEVERITY_WARNING, RULE_R04, "C1", "w"),
        ]
        result = MetaValidationResult(issues=issues, error_count=0, warning_count=1, passed=True)
        warnings = result.warnings()
        assert len(warnings) == 1
        assert warnings[0].rule_name == RULE_R04

    def test_issues_for_candidate(self):
        issues = [
            ValidationIssue(SEVERITY_ERROR, RULE_R01, "C1", "e1"),
            ValidationIssue(SEVERITY_WARNING, RULE_R04, "C2", "w"),
            ValidationIssue(SEVERITY_ERROR, RULE_R05, "C1", "e2"),
        ]
        result = MetaValidationResult(issues=issues, error_count=2, warning_count=1, passed=False)
        c1_issues = result.issues_for("C1")
        assert len(c1_issues) == 2
        assert all(i.candidate_id == "C1" for i in c1_issues)

    def test_issues_for_rule(self):
        issues = [
            ValidationIssue(SEVERITY_ERROR, RULE_R01, "C1", "e"),
            ValidationIssue(SEVERITY_WARNING, RULE_R04, "C2", "w"),
        ]
        result = MetaValidationResult(issues=issues, error_count=1, warning_count=1, passed=False)
        r01_issues = result.issues_for_rule(RULE_R01)
        assert len(r01_issues) == 1
        assert r01_issues[0].rule_name == RULE_R01

    def test_summary_passed(self):
        result = MetaValidationResult(issues=[], error_count=0, warning_count=0, passed=True)
        summary = result.summary()
        assert "PASSED" in summary
        assert "0 error" in summary

    def test_summary_failed(self):
        issues = [ValidationIssue(SEVERITY_ERROR, RULE_R01, "C1", "e")]
        result = MetaValidationResult(issues=issues, error_count=1, warning_count=0, passed=False)
        summary = result.summary()
        assert "FAILED" in summary
        assert "1 error" in summary


# ---------------------------------------------------------------------------
# TestMetaValidatorInit (3 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.meta_validation
class TestMetaValidatorInit:
    """MetaValidator.__init__ の設定テスト。"""

    def test_default_pbo_threshold(self):
        v = MetaValidator()
        assert v._pbo_threshold == DEFAULT_PBO_THRESHOLD

    def test_custom_pbo_threshold(self):
        v = MetaValidator(pbo_threshold=0.7)
        assert v._pbo_threshold == pytest.approx(0.7)

    def test_pbo_threshold_zero_allowed(self):
        v = MetaValidator(pbo_threshold=0.0)
        assert v._pbo_threshold == 0.0


# ---------------------------------------------------------------------------
# TestMetaValidatorR01 (7 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.meta_validation
class TestMetaValidatorR01:
    """R01: SELECTED かつ hard_gate_passed=False → ERROR。"""

    def setup_method(self):
        self.v = MetaValidator()

    def test_selected_gate_passed_no_error(self):
        ev = _eval("C1", hard_gate_passed=True)
        dec = _dec("C1", "SELECTED")
        result = self.v.validate([ev], [dec])
        r01 = result.issues_for_rule(RULE_R01)
        assert r01 == []

    def test_selected_gate_failed_is_error(self):
        ev = _eval("C1", hard_gate_passed=False, hard_gate_failures=["min_oos_sharpe"])
        dec = _dec("C1", "SELECTED")
        result = self.v.validate([ev], [dec])
        r01 = result.issues_for_rule(RULE_R01)
        assert len(r01) == 1
        assert r01[0].severity == SEVERITY_ERROR
        assert r01[0].candidate_id == "C1"

    def test_rejected_gate_failed_no_r01_error(self):
        """REJECTED で gate_failed でも R01 エラーにはならない。"""
        ev = _eval("C1", hard_gate_passed=False)
        dec = _dec("C1", "REJECTED")
        result = self.v.validate([ev], [dec])
        r01 = result.issues_for_rule(RULE_R01)
        assert r01 == []

    def test_hold_gate_failed_no_r01_error(self):
        ev = _eval("C1", hard_gate_passed=False)
        dec = _dec("C1", "HOLD")
        result = self.v.validate([ev], [dec])
        assert result.issues_for_rule(RULE_R01) == []

    def test_selected_no_evaluation_skipped(self):
        """evaluation が存在しない SELECTED 決定はスキップ (別レイヤーで保証)。"""
        dec = _dec("C999", "SELECTED")
        result = self.v.validate([], [dec])
        assert result.issues_for_rule(RULE_R01) == []

    def test_multiple_candidates_mixed(self):
        ev1 = _eval("C1", hard_gate_passed=True)
        ev2 = _eval("C2", hard_gate_passed=False)
        ev3 = _eval("C3", hard_gate_passed=False)
        dec1 = _dec("C1", "SELECTED")
        dec2 = _dec("C2", "SELECTED")   # ← 問題あり
        dec3 = _dec("C3", "REJECTED")   # ← 問題なし
        result = self.v.validate([ev1, ev2, ev3], [dec1, dec2, dec3])
        r01 = result.issues_for_rule(RULE_R01)
        assert len(r01) == 1
        assert r01[0].candidate_id == "C2"

    def test_r01_detail_contains_gate_failures(self):
        ev = _eval("C1", hard_gate_passed=False, hard_gate_failures=["max_turnover", "min_ic"])
        dec = _dec("C1", "SELECTED")
        result = self.v.validate([ev], [dec])
        r01 = result.issues_for_rule(RULE_R01)
        assert "max_turnover" in r01[0].detail


# ---------------------------------------------------------------------------
# TestMetaValidatorR02 (7 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.meta_validation
class TestMetaValidatorR02:
    """R02: decision_rank 単調性確認。"""

    def setup_method(self):
        self.v = MetaValidator()

    def test_monotone_decreasing_no_error(self):
        evs = [_eval(f"C{i}") for i in range(1, 4)]
        decs = [
            _dec("C1", "SELECTED", decision_rank=1, frost_score=0.9),
            _dec("C2", "SELECTED", decision_rank=2, frost_score=0.7),
            _dec("C3", "HOLD",     decision_rank=3, frost_score=0.5),
        ]
        result = self.v.validate(evs, decs)
        assert result.issues_for_rule(RULE_R02) == []

    def test_monotone_violation_is_error(self):
        evs = [_eval(f"C{i}") for i in range(1, 3)]
        decs = [
            _dec("C1", "SELECTED", decision_rank=1, frost_score=0.5),
            _dec("C2", "SELECTED", decision_rank=2, frost_score=0.8),  # ← 逆転
        ]
        result = self.v.validate(evs, decs)
        r02 = result.issues_for_rule(RULE_R02)
        assert len(r02) == 1
        assert r02[0].severity == SEVERITY_ERROR

    def test_equal_scores_allowed(self):
        """同スコア (tie) は単調性違反ではない。"""
        evs = [_eval(f"C{i}") for i in range(1, 3)]
        decs = [
            _dec("C1", "SELECTED", decision_rank=1, frost_score=0.5),
            _dec("C2", "SELECTED", decision_rank=2, frost_score=0.5),
        ]
        result = self.v.validate(evs, decs)
        assert result.issues_for_rule(RULE_R02) == []

    def test_single_ranked_no_error(self):
        ev = _eval("C1")
        dec = _dec("C1", "SELECTED", decision_rank=1, frost_score=0.8)
        result = self.v.validate([ev], [dec])
        assert result.issues_for_rule(RULE_R02) == []

    def test_no_ranked_decisions_no_error(self):
        ev = _eval("C1")
        dec = _dec("C1", "SELECTED", decision_rank=None, frost_score=0.8)
        result = self.v.validate([ev], [dec])
        assert result.issues_for_rule(RULE_R02) == []

    def test_multiple_violations_detected(self):
        evs = [_eval(f"C{i}") for i in range(1, 5)]
        decs = [
            _dec("C1", "SELECTED", decision_rank=1, frost_score=0.9),
            _dec("C2", "SELECTED", decision_rank=2, frost_score=0.95),  # 違反1
            _dec("C3", "SELECTED", decision_rank=3, frost_score=0.8),
            _dec("C4", "HOLD",     decision_rank=4, frost_score=0.85),  # 違反2
        ]
        result = self.v.validate(evs, decs)
        r02 = result.issues_for_rule(RULE_R02)
        assert len(r02) == 2

    def test_r02_detail_contains_rank_and_score(self):
        evs = [_eval(f"C{i}") for i in range(1, 3)]
        decs = [
            _dec("C1", "SELECTED", decision_rank=1, frost_score=0.4),
            _dec("C2", "SELECTED", decision_rank=2, frost_score=0.9),  # 違反
        ]
        result = self.v.validate(evs, decs)
        r02 = result.issues_for_rule(RULE_R02)
        assert "rank=1" in r02[0].detail or "rank=2" in r02[0].detail


# ---------------------------------------------------------------------------
# TestMetaValidatorR03 (5 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.meta_validation
class TestMetaValidatorR03:
    """R03: suppressed_by_dedup=True かつ SELECTED → ERROR。"""

    def setup_method(self):
        self.v = MetaValidator()

    def test_not_suppressed_selected_no_error(self):
        ev = _eval("C1")
        dec = _dec("C1", "SELECTED", suppressed_by_dedup=False)
        result = self.v.validate([ev], [dec])
        assert result.issues_for_rule(RULE_R03) == []

    def test_suppressed_selected_is_error(self):
        ev = _eval("C1")
        dec = _dec("C1", "SELECTED", suppressed_by_dedup=True, near_duplicate_of="C0")
        result = self.v.validate([ev], [dec])
        r03 = result.issues_for_rule(RULE_R03)
        assert len(r03) == 1
        assert r03[0].severity == SEVERITY_ERROR
        assert r03[0].candidate_id == "C1"

    def test_suppressed_rejected_no_error(self):
        """REJECTED + suppressed_by_dedup=True は正常。"""
        ev = _eval("C1")
        dec = _dec("C1", "REJECTED", suppressed_by_dedup=True)
        result = self.v.validate([ev], [dec])
        assert result.issues_for_rule(RULE_R03) == []

    def test_suppressed_hold_no_error(self):
        ev = _eval("C1")
        dec = _dec("C1", "HOLD", suppressed_by_dedup=True)
        result = self.v.validate([ev], [dec])
        assert result.issues_for_rule(RULE_R03) == []

    def test_r03_detail_contains_near_duplicate_of(self):
        ev = _eval("C1")
        dec = _dec("C1", "SELECTED", suppressed_by_dedup=True, near_duplicate_of="C0")
        result = self.v.validate([ev], [dec])
        r03 = result.issues_for_rule(RULE_R03)
        assert "C0" in r03[0].detail


# ---------------------------------------------------------------------------
# TestMetaValidatorR04 (5 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.meta_validation
class TestMetaValidatorR04:
    """R04: pbo_score > threshold かつ SELECTED → WARNING。"""

    def setup_method(self):
        self.v = MetaValidator()  # threshold=0.5

    def test_pbo_below_threshold_no_warning(self):
        ev = _eval("C1", pbo_score=0.3)
        dec = _dec("C1", "SELECTED")
        result = self.v.validate([ev], [dec])
        assert result.issues_for_rule(RULE_R04) == []

    def test_pbo_above_threshold_is_warning(self):
        ev = _eval("C1", pbo_score=0.8)
        dec = _dec("C1", "SELECTED")
        result = self.v.validate([ev], [dec])
        r04 = result.issues_for_rule(RULE_R04)
        assert len(r04) == 1
        assert r04[0].severity == SEVERITY_WARNING
        assert r04[0].candidate_id == "C1"

    def test_pbo_exactly_threshold_no_warning(self):
        """threshold 丁度は WARNING を出さない (> strict)。"""
        ev = _eval("C1", pbo_score=0.5)
        dec = _dec("C1", "SELECTED")
        result = self.v.validate([ev], [dec])
        assert result.issues_for_rule(RULE_R04) == []

    def test_pbo_high_not_selected_no_warning(self):
        """SELECTED でなければ高 pbo でも R04 警告なし。"""
        ev = _eval("C1", pbo_score=0.9)
        dec = _dec("C1", "HOLD")
        result = self.v.validate([ev], [dec])
        assert result.issues_for_rule(RULE_R04) == []

    def test_custom_threshold_respected(self):
        v = MetaValidator(pbo_threshold=0.7)
        ev = _eval("C1", pbo_score=0.6)
        dec = _dec("C1", "SELECTED")
        result = v.validate([ev], [dec])
        # threshold=0.7 なので 0.6 は WARNING なし
        assert result.issues_for_rule(RULE_R04) == []
        # threshold=0.5 のデフォルト validator では WARNING あり
        result2 = self.v.validate([ev], [dec])
        assert len(result2.issues_for_rule(RULE_R04)) == 1


# ---------------------------------------------------------------------------
# TestMetaValidatorR05 (5 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.meta_validation
class TestMetaValidatorR05:
    """R05: frost_score < 0.0 の FrostEvaluation → ERROR。"""

    def setup_method(self):
        self.v = MetaValidator()

    def test_positive_score_no_error(self):
        ev = _eval("C1", frost_score=0.5)
        result = self.v.validate([ev], [])
        assert result.issues_for_rule(RULE_R05) == []

    def test_zero_score_no_error(self):
        ev = _eval("C1", frost_score=0.0)
        result = self.v.validate([ev], [])
        assert result.issues_for_rule(RULE_R05) == []

    def test_negative_score_is_error(self):
        ev = _eval("C1", frost_score=-0.1)
        result = self.v.validate([ev], [])
        r05 = result.issues_for_rule(RULE_R05)
        assert len(r05) == 1
        assert r05[0].severity == SEVERITY_ERROR
        assert r05[0].candidate_id == "C1"

    def test_multiple_negative_scores(self):
        evs = [
            _eval("C1", frost_score=0.5),
            _eval("C2", frost_score=-0.2),
            _eval("C3", frost_score=-1.0),
        ]
        result = self.v.validate(evs, [])
        r05 = result.issues_for_rule(RULE_R05)
        assert len(r05) == 2
        ids = {i.candidate_id for i in r05}
        assert ids == {"C2", "C3"}

    def test_r05_detail_contains_score_value(self):
        ev = _eval("C1", frost_score=-0.123456)
        result = self.v.validate([ev], [])
        r05 = result.issues_for_rule(RULE_R05)
        assert "-0.123456" in r05[0].detail


# ---------------------------------------------------------------------------
# TestMetaValidatorIntegration (5 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.meta_validation
class TestMetaValidatorIntegration:
    """validate() 統合テスト + D8 負債解消確認。"""

    def setup_method(self):
        self.v = MetaValidator()

    def test_clean_batch_passes(self):
        """全候補が整合性を満たす場合は passed=True、issues=[]。"""
        evs = [
            _eval("C1", frost_score=0.8, hard_gate_passed=True, pbo_score=0.1),
            _eval("C2", frost_score=0.6, hard_gate_passed=True, pbo_score=0.2),
            _eval("C3", frost_score=0.3, hard_gate_passed=False),
        ]
        decs = [
            _dec("C1", "SELECTED", decision_rank=1, frost_score=0.8),
            _dec("C2", "HOLD",     decision_rank=2, frost_score=0.6),
            _dec("C3", "REJECTED"),
        ]
        result = self.v.validate(evs, decs)
        assert result.passed is True
        assert result.error_count == 0
        assert result.issues == []

    def test_mixed_errors_and_warnings(self):
        """R01 ERROR + R04 WARNING が同時に検出できること。"""
        evs = [
            _eval("C1", frost_score=0.5, hard_gate_passed=False),
            _eval("C2", frost_score=0.4, pbo_score=0.9),
        ]
        decs = [
            _dec("C1", "SELECTED"),   # R01: gate_failed なのに SELECTED
            _dec("C2", "SELECTED"),   # R04: pbo 高いのに SELECTED
        ]
        result = self.v.validate(evs, decs)
        assert result.passed is False
        assert result.error_count >= 1
        assert result.warning_count >= 1

    def test_empty_batch_passes(self):
        result = self.v.validate([], [])
        assert result.passed is True
        assert result.issues == []

    # ── D8 負債解消確認 ────────────────────────────────────────────────────

    def test_frost_metrics_no_statistics_import(self):
        """D8: frost_metrics.py が statistics モジュールを import していないこと。"""
        import frost_metrics
        assert "statistics" not in dir(frost_metrics), (
            "frost_metrics が statistics モジュールを公開している: ADR-001 禁止リスト違反"
        )
        # モジュールの __dict__ に statistics オブジェクトがないことを確認
        assert not hasattr(frost_metrics, "statistics"), (
            "frost_metrics に statistics 属性が存在する"
        )

    def test_frost_stability_no_statistics_import(self):
        """D8: frost_stability.py が statistics モジュールを import していないこと。"""
        import frost_stability
        assert not hasattr(frost_stability, "statistics"), (
            "frost_stability に statistics 属性が存在する"
        )


# ---------------------------------------------------------------------------
# TestMetaValidatorSignatureGuard (5 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.meta_validation
class TestMetaValidatorSignatureGuard:
    """公開 API のシグネチャ不変テスト。"""

    def test_validation_issue_fields(self):
        """ValidationIssue のフィールドが期待どおりであること。"""
        issue = ValidationIssue("ERROR", "R01", "C1", "d")
        assert hasattr(issue, "severity")
        assert hasattr(issue, "rule_name")
        assert hasattr(issue, "candidate_id")
        assert hasattr(issue, "detail")

    def test_meta_validation_result_fields(self):
        """MetaValidationResult のフィールドが期待どおりであること。"""
        result = MetaValidationResult()
        assert hasattr(result, "issues")
        assert hasattr(result, "error_count")
        assert hasattr(result, "warning_count")
        assert hasattr(result, "passed")

    def test_meta_validator_validate_signature(self):
        """validate() が evaluations, decisions の 2 引数を受け取ること。"""
        sig = inspect.signature(MetaValidator.validate)
        params = list(sig.parameters.keys())
        assert "evaluations" in params
        assert "decisions" in params

    def test_meta_validation_result_summary_returns_string(self):
        result = MetaValidationResult(issues=[], error_count=0, warning_count=0, passed=True)
        assert isinstance(result.summary(), str)

    def test_meta_validator_validate_returns_result_type(self):
        v = MetaValidator()
        result = v.validate([], [])
        assert isinstance(result, MetaValidationResult)


# ---------------------------------------------------------------------------
# D8 負債解消: pure Python 置換の数値正確性テスト (各 5 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.meta_validation
class TestFrostMetricsPurePython:
    """D8: frost_metrics.robust_normalize の pure Python median/stdev の正確性。"""

    def test_odd_n_median(self):
        """奇数個の場合、中央値は sorted_v[n//2]。"""
        values = [3.0, 1.0, 2.0, 5.0, 4.0]  # sorted: [1,2,3,4,5] → median=3
        result = robust_normalize(values, clip_min=-10.0, clip_max=10.0)
        # 値 3.0 は median と一致 → z=0.0
        idx_of_3 = values.index(3.0)
        assert result[idx_of_3] == pytest.approx(0.0, abs=1e-9)

    def test_even_n_median(self):
        """偶数個の場合、中央値は (sorted_v[n//2-1] + sorted_v[n//2]) / 2。"""
        values = [1.0, 2.0, 3.0, 4.0]  # median = (2+3)/2 = 2.5
        result = robust_normalize(values, clip_min=-10.0, clip_max=10.0)
        # 値 2.5 より大きい 3.0 は正の z を持つべき
        idx_3 = values.index(3.0)
        idx_2 = values.index(2.0)
        assert result[idx_3] > 0.0
        assert result[idx_2] < 0.0

    def test_empty_returns_empty(self):
        assert robust_normalize([]) == []

    def test_single_returns_zero(self):
        assert robust_normalize([5.0]) == [0.0]

    def test_all_same_returns_zeros(self):
        """全要素が同値の場合は 0.0 リストを返す。"""
        result = robust_normalize([3.0, 3.0, 3.0, 3.0])
        assert all(v == 0.0 for v in result)

    def test_iqr_zero_stdev_fallback(self):
        """IQR=0 のとき stdev フォールバックで scale を計算する。"""
        # [1, 2, 2, 2, 3] → IQR = q3-q1 = 2-2 = 0 → stdev フォールバック
        values = [1.0, 2.0, 2.0, 2.0, 3.0]
        result = robust_normalize(values, clip_min=-10.0, clip_max=10.0)
        # scale > 0 なら全ゼロにはならないはず
        assert not all(v == 0.0 for v in result), "stdev フォールバックが機能していない"


@pytest.mark.meta_validation
class TestFrostStabilityPurePython:
    """D8: frost_stability._stdev_safe の pure Python 実装の正確性。"""

    def test_empty_returns_zero(self):
        assert _stdev_safe([]) == 0.0

    def test_single_returns_zero(self):
        assert _stdev_safe([5.0]) == 0.0

    def test_two_values_variance(self):
        """n=2: stdev = |a-b| / sqrt(1) = |a-b|。"""
        result = _stdev_safe([1.0, 3.0])  # mean=2, var=(1+1)/1=2, stdev=sqrt(2)
        expected = math.sqrt(2.0)
        assert result == pytest.approx(expected, rel=1e-9)

    def test_uniform_values(self):
        """全要素が同値 → 標準偏差 0。"""
        assert _stdev_safe([5.0, 5.0, 5.0, 5.0]) == pytest.approx(0.0, abs=1e-9)

    def test_known_values(self):
        """[2,4,4,4,5,5,7,9] → stdev≈2.0 (population)。標本は約2.138。"""
        values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        result = _stdev_safe(values)
        # n=8, mean=5, sum_sq_dev = 32, sample_var = 32/7 ≈ 4.571, stdev ≈ 2.138
        expected = math.sqrt(32.0 / 7)
        assert result == pytest.approx(expected, rel=1e-9)
