"""
tests/unit/test_phase2_evidence_bundle.py
------------------------------------------
Phase 2: EvidenceBundle 型付き境界の単体テスト (DB 不要)

カバー範囲:
  TestScoreComponents         (8)  スコア軸コンテナ・to_dict・effective_score
  TestGateVerdict             (9)  全PASS / 失敗追加 / to_list / 個別フラグ
  TestRawFeatures             (7)  from_dict / get 互換 / Noneハンドリング
  TestStabilityEvidence       (3)  フィールド存在・デフォルト値
  TestPBOEvidence             (3)  フィールド存在・デフォルト値
  TestEvidenceBundle          (8)  集約構造・is_gate_passed・to_diagnostics_dict
  TestEvaluateCandidateToBundle(10) 純関数の出力型・スコア一致・Gate判定
  TestEvaluationFromBundle    (9)  FrostEvaluation 変換・後方互換・v2フラグ
  TestSelectorBackwardCompat  (6)  evaluate_candidate() との出力等価確認

合計: 63 テスト
"""
from __future__ import annotations

from dataclasses import fields
from typing import Any

import pytest

from analytics.python.frost.evidence_bundle import (
    EvidenceBundle,
    GateVerdict,
    PBOEvidence,
    RawFeatures,
    ScoreComponents,
    StabilityEvidence,
    _safe,
    _safe_opt,
    evaluate_candidate_to_bundle,
    evaluation_from_bundle,
)
from analytics.python.frost.frost_config import FrostConfig
from analytics.python.frost.frost_contracts import FrostCandidate, FrostEvaluation
from analytics.python.frost.frost_selector import evaluate_candidate


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _make_candidate(**kwargs) -> FrostCandidate:
    defaults = {"candidate_id": "test-cand-001"}
    defaults.update(kwargs)
    return FrostCandidate(**defaults)


def _make_config(**kwargs) -> FrostConfig:
    return FrostConfig(**kwargs)


# ===========================================================================
# TestScoreComponents
# ===========================================================================

class TestScoreComponents:
    def test_default_instantiation(self):
        sc = ScoreComponents()
        assert sc is not None

    def test_positive_score_fields(self):
        sc = ScoreComponents(predictive_score=0.8, oos_sharpe_score=0.7)
        assert sc.predictive_score == 0.8
        assert sc.oos_sharpe_score == 0.7

    def test_penalty_fields(self):
        sc = ScoreComponents(pbo_penalty=0.1, turnover_penalty=0.2)
        assert sc.pbo_penalty == 0.1
        assert sc.turnover_penalty == 0.2

    def test_to_dict_key_count(self):
        sc = ScoreComponents()
        d = sc.to_dict()
        # 正スコア 9 + ペナルティ 8 + frost_score x2 = 19
        assert len(d) == 19

    def test_to_dict_contains_frost_scores(self):
        sc = ScoreComponents(frost_score_v1=0.5, frost_score_v2=0.6)
        d = sc.to_dict()
        assert d["frost_score_v1"] == 0.5
        assert d["frost_score_v2"] == 0.6

    def test_effective_frost_score_v1(self):
        sc = ScoreComponents(frost_score_v1=0.5, frost_score_v2=0.7)
        assert sc.effective_frost_score(use_v2=False) == 0.5

    def test_effective_frost_score_v2(self):
        sc = ScoreComponents(frost_score_v1=0.5, frost_score_v2=0.7)
        assert sc.effective_frost_score(use_v2=True) == 0.7

    def test_default_neutral_values(self):
        """未評価軸はデフォルト中立値 0.5"""
        sc = ScoreComponents()
        assert sc.genome_novelty_score == 0.5
        assert sc.causal_validity_score == 0.5
        assert sc.regime_entropy_score == 0.5


# ===========================================================================
# TestGateVerdict
# ===========================================================================

class TestGateVerdict:
    def test_all_pass_factory(self):
        gv = GateVerdict.all_pass()
        assert gv.passed is True
        assert len(gv.failures) == 0

    def test_add_failure_sets_passed_false(self):
        gv = GateVerdict.all_pass()
        gv.add_failure("pbo", "pbo=0.30 > 0.20")
        assert gv.passed is False

    def test_add_failure_appends_message(self):
        gv = GateVerdict.all_pass()
        gv.add_failure("pbo", "pbo=0.30 > 0.20")
        assert "pbo=0.30 > 0.20" in gv.failures

    def test_multiple_failures(self):
        gv = GateVerdict.all_pass()
        gv.add_failure("pbo", "msg1")
        gv.add_failure("rank_ic", "msg2")
        assert len(gv.failures) == 2

    def test_gate_pbo_flag_updated(self):
        gv = GateVerdict.all_pass()
        gv.add_failure("pbo", "msg")
        assert gv.gate_pbo is False

    def test_gate_oos_sharpe_flag_updated(self):
        gv = GateVerdict.all_pass()
        gv.add_failure("oos_sharpe", "msg")
        assert gv.gate_oos_sharpe is False

    def test_to_list_returns_failure_messages(self):
        gv = GateVerdict.all_pass()
        gv.add_failure("turnover", "turnover=5.0 > 4.0")
        lst = gv.to_list()
        assert isinstance(lst, list)
        assert "turnover=5.0 > 4.0" in lst

    def test_to_list_empty_when_all_pass(self):
        gv = GateVerdict.all_pass()
        assert gv.to_list() == []

    def test_individual_gate_flags_default_true(self):
        gv = GateVerdict()
        assert gv.gate_pbo is True
        assert gv.gate_complexity is True
        assert gv.gate_causal_direction is True


# ===========================================================================
# TestRawFeatures
# ===========================================================================

class TestRawFeatures:
    def test_from_dict_basic(self):
        d = {"rank_ic": 0.05, "oos_sharpe": 0.8, "complexity_score": 0.3}
        rf = RawFeatures.from_dict(d)
        assert rf.rank_ic == pytest.approx(0.05)
        assert rf.oos_sharpe == pytest.approx(0.8)
        assert rf.complexity_score == pytest.approx(0.3)

    def test_from_dict_none_when_missing(self):
        rf = RawFeatures.from_dict({})
        assert rf.rank_ic is None
        assert rf.oos_sharpe is None

    def test_from_dict_nan_becomes_none(self):
        import math
        rf = RawFeatures.from_dict({"rank_ic": float("nan")})
        assert rf.rank_ic is None

    def test_from_dict_inf_becomes_none(self):
        rf = RawFeatures.from_dict({"oos_sharpe": float("inf")})
        assert rf.oos_sharpe is None

    def test_get_compat_explicit_field(self):
        rf = RawFeatures.from_dict({"rank_ic": 0.06})
        assert rf.get("rank_ic") == pytest.approx(0.06)

    def test_get_compat_raw_dict_fallback(self):
        rf = RawFeatures.from_dict({"custom_key": 42.0})
        assert rf.get("custom_key") == 42.0

    def test_fold_lists_populated(self):
        d = {"fold_sharpes": [0.5, 0.6, 0.7], "fold_ics": [0.04, 0.05]}
        rf = RawFeatures.from_dict(d)
        assert rf.fold_sharpes == [0.5, 0.6, 0.7]
        assert rf.fold_ics == [0.04, 0.05]


# ===========================================================================
# TestStabilityEvidence
# ===========================================================================

class TestStabilityEvidence:
    def test_default_instantiation(self):
        se = StabilityEvidence()
        assert se.selection_consistency_score == 0.0

    def test_fields_accessible(self):
        se = StabilityEvidence(
            selection_consistency_score=0.7,
            fold_sharpe_std=0.05,
            n_folds=8,
        )
        assert se.fold_sharpe_std == pytest.approx(0.05)
        assert se.n_folds == 8

    def test_optional_fields_none(self):
        se = StabilityEvidence()
        assert se.top_k_stability is None
        assert se.sign_stability is None


# ===========================================================================
# TestPBOEvidence
# ===========================================================================

class TestPBOEvidence:
    def test_default_instantiation(self):
        pe = PBOEvidence()
        assert pe.pbo_score == 0.0

    def test_fields_accessible(self):
        pe = PBOEvidence(pbo_score=0.15, pbo_raw=0.14, n_folds=10)
        assert pe.pbo_score == pytest.approx(0.15)
        assert pe.n_folds == 10

    def test_selection_fragility_default_zero(self):
        pe = PBOEvidence()
        assert pe.selection_fragility == 0.0


# ===========================================================================
# TestEvidenceBundle
# ===========================================================================

class TestEvidenceBundle:
    def test_default_instantiation(self):
        eb = EvidenceBundle()
        assert eb is not None

    def test_fields_have_defaults(self):
        eb = EvidenceBundle(candidate_id="c1", run_id="r1", trace_id="t1")
        assert eb.candidate_id == "c1"
        assert isinstance(eb.features, RawFeatures)
        assert isinstance(eb.scores, ScoreComponents)
        assert isinstance(eb.gate, GateVerdict)

    def test_is_gate_passed_default_true(self):
        eb = EvidenceBundle()
        assert eb.is_gate_passed() is True

    def test_is_gate_passed_after_failure(self):
        eb = EvidenceBundle()
        eb.gate.add_failure("pbo", "fail")
        assert eb.is_gate_passed() is False

    def test_effective_frost_score_v1(self):
        eb = EvidenceBundle()
        eb.scores.frost_score_v1 = 0.42
        eb.scores.frost_score_v2 = 0.55
        assert eb.effective_frost_score(use_v2=False) == pytest.approx(0.42)

    def test_effective_frost_score_v2(self):
        eb = EvidenceBundle()
        eb.scores.frost_score_v1 = 0.42
        eb.scores.frost_score_v2 = 0.55
        assert eb.effective_frost_score(use_v2=True) == pytest.approx(0.55)

    def test_to_diagnostics_dict_keys(self):
        eb = EvidenceBundle()
        d = eb.to_diagnostics_dict()
        expected = {
            "pbo_raw", "selection_fragility", "fold_sharpe_std",
            "fold_sharpe_mean", "fold_ic_mean", "n_folds",
            "gate_failures", "score_breakdown",
        }
        assert set(d.keys()) == expected

    def test_to_diagnostics_dict_score_breakdown_type(self):
        eb = EvidenceBundle()
        d = eb.to_diagnostics_dict()
        assert isinstance(d["score_breakdown"], dict)


# ===========================================================================
# TestEvaluateCandidateToBundle
# ===========================================================================

class TestEvaluateCandidateToBundle:
    def test_returns_evidence_bundle(self):
        cand = _make_candidate()
        cfg = _make_config()
        result = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        assert isinstance(result, EvidenceBundle)

    def test_candidate_id_preserved(self):
        cand = _make_candidate(candidate_id="cand-xyz")
        cfg = _make_config()
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        assert bundle.candidate_id == "cand-xyz"

    def test_run_id_preserved(self):
        cand = _make_candidate()
        cfg = _make_config()
        bundle = evaluate_candidate_to_bundle(cand, "run-abc", "t", cfg)
        assert bundle.run_id == "run-abc"

    def test_features_populated(self):
        cand = _make_candidate()
        cfg = _make_config()
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        assert isinstance(bundle.features, RawFeatures)

    def test_pbo_evidence_populated(self):
        cand = _make_candidate()
        cfg = _make_config()
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        assert isinstance(bundle.pbo, PBOEvidence)
        assert bundle.pbo.pbo_score >= 0.0

    def test_stability_evidence_populated(self):
        cand = _make_candidate()
        cfg = _make_config()
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        assert isinstance(bundle.stability, StabilityEvidence)

    def test_scores_populated(self):
        cand = _make_candidate()
        cfg = _make_config()
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        assert isinstance(bundle.scores, ScoreComponents)
        assert 0.0 <= bundle.scores.frost_score_v1 <= 10.0  # reasonable range

    def test_gate_verdict_populated(self):
        cand = _make_candidate()
        cfg = _make_config()
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        assert isinstance(bundle.gate, GateVerdict)

    def test_good_candidate_may_pass_gate(self):
        """OOS Sharpe が高く PBO が低い候補はゲートを通過しうる"""
        cand = _make_candidate(
            backtest_summary={
                "oos_sharpe": 1.5, "max_dd": 0.05, "turnover": 1.0,
            },
            metrics={"rank_ic": 0.08, "ic": 0.06},
            fold_results=[
                {"fold_sharpe": 1.2, "oos_sharpe": 1.1},
                {"fold_sharpe": 1.3, "oos_sharpe": 1.2},
                {"fold_sharpe": 1.1, "oos_sharpe": 1.0},
                {"fold_sharpe": 1.4, "oos_sharpe": 1.3},
                {"fold_sharpe": 1.2, "oos_sharpe": 1.1},
            ],
        )
        cfg = _make_config(min_oos_sharpe=0.3, pbo_threshold=0.5)
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        # ゲート通過の場合、スコアは正のはず
        if bundle.gate.passed:
            assert bundle.scores.frost_score_v1 >= 0.0

    def test_determinism_same_candidate_same_result(self):
        """同一候補・同一設定なら同一スコア"""
        cand = _make_candidate()
        cfg = _make_config()
        b1 = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        b2 = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        assert b1.scores.frost_score_v1 == pytest.approx(b2.scores.frost_score_v1)


# ===========================================================================
# TestEvaluationFromBundle
# ===========================================================================

class TestEvaluationFromBundle:
    def _make_bundle(self) -> EvidenceBundle:
        cand = _make_candidate()
        cfg = _make_config()
        return evaluate_candidate_to_bundle(cand, "r", "t", cfg)

    def test_returns_frost_evaluation(self):
        bundle = self._make_bundle()
        ev = evaluation_from_bundle(bundle)
        assert isinstance(ev, FrostEvaluation)

    def test_candidate_id_preserved(self):
        bundle = self._make_bundle()
        ev = evaluation_from_bundle(bundle)
        assert ev.candidate_id == bundle.candidate_id

    def test_frost_score_v1_used_by_default(self):
        bundle = self._make_bundle()
        bundle.scores.frost_score_v1 = 0.42
        bundle.scores.frost_score_v2 = 0.99
        ev = evaluation_from_bundle(bundle, use_v2_score=False)
        assert ev.frost_score == pytest.approx(0.42)

    def test_frost_score_v2_when_flag_set(self):
        bundle = self._make_bundle()
        bundle.scores.frost_score_v1 = 0.42
        bundle.scores.frost_score_v2 = 0.99
        ev = evaluation_from_bundle(bundle, use_v2_score=True)
        assert ev.frost_score == pytest.approx(0.99)

    def test_hard_gate_passed_preserved(self):
        bundle = self._make_bundle()
        bundle.gate.passed = True
        ev = evaluation_from_bundle(bundle)
        assert ev.hard_gate_passed is True

    def test_hard_gate_failures_preserved(self):
        bundle = self._make_bundle()
        bundle.gate.failures = ["gate1 fail", "gate2 fail"]
        bundle.gate.passed = False
        ev = evaluation_from_bundle(bundle)
        assert len(ev.hard_gate_failures) == 2

    def test_diagnostics_json_contains_score_breakdown(self):
        bundle = self._make_bundle()
        ev = evaluation_from_bundle(bundle)
        assert "score_breakdown" in ev.diagnostics_json

    def test_diagnostics_json_contains_pbo_raw(self):
        bundle = self._make_bundle()
        ev = evaluation_from_bundle(bundle)
        assert "pbo_raw" in ev.diagnostics_json

    def test_metrics_json_preserved(self):
        bundle = self._make_bundle()
        bundle.metrics_json = {"rank_ic": 0.07}
        ev = evaluation_from_bundle(bundle)
        assert ev.metrics_json == {"rank_ic": 0.07}


# ===========================================================================
# TestSelectorBackwardCompat
# ===========================================================================

class TestSelectorBackwardCompat:
    """
    evaluate_candidate() (Phase 1 以前の API) と
    evaluate_candidate_to_bundle() + evaluation_from_bundle() の
    出力が等価であることを確認する。
    """

    def _pair(self, **cand_kwargs):
        cand = _make_candidate(**cand_kwargs)
        cfg = _make_config()
        ev_old = evaluate_candidate(cand, "r", "t", cfg)
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        ev_new = evaluation_from_bundle(bundle)
        return ev_old, ev_new

    def test_frost_score_equal(self):
        ev_old, ev_new = self._pair()
        assert ev_old.frost_score == pytest.approx(ev_new.frost_score, abs=1e-9)

    def test_hard_gate_passed_equal(self):
        ev_old, ev_new = self._pair()
        assert ev_old.hard_gate_passed == ev_new.hard_gate_passed

    def test_hard_gate_failures_equal(self):
        ev_old, ev_new = self._pair()
        assert sorted(ev_old.hard_gate_failures) == sorted(ev_new.hard_gate_failures)

    def test_predictive_score_equal(self):
        ev_old, ev_new = self._pair()
        assert ev_old.predictive_score == pytest.approx(ev_new.predictive_score, abs=1e-9)

    def test_candidate_id_equal(self):
        ev_old, ev_new = self._pair(candidate_id="back-compat-test")
        assert ev_old.candidate_id == ev_new.candidate_id

    def test_diagnostics_score_breakdown_equal(self):
        ev_old, ev_new = self._pair()
        old_bd = ev_old.diagnostics_json.get("score_breakdown", {})
        new_bd = ev_new.diagnostics_json.get("score_breakdown", {})
        # 共通キーのスコアが一致
        for k in ["predictive", "oos_sharpe", "capacity"]:
            if k in old_bd and k in new_bd:
                assert old_bd[k] == pytest.approx(new_bd[k], abs=1e-9), f"key={k} mismatch"
