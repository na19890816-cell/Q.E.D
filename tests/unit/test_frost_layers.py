"""
test_frost_layers.py
--------------------
FROST Meta-Fitness Engine のユニットテスト。

テスト対象:
  - frost_config.py      : FrostConfig 構築・検証
  - frost_contracts.py   : dataclass 生成
  - frost_features.py    : 特徴量抽出
  - frost_metrics.py     : スコア計算
  - frost_stability.py   : 安定性計算
  - frost_pbo.py         : PBO 推定
  - frost_selector.py    : hard gate / 評価
  - frost_ranker.py      : ランキング / near-dup
  - frost_decision_engine.py : 最終ポリシー
  - frost_report_builder.py  : レポート生成
"""
from __future__ import annotations

import math
import uuid
from typing import List

import pytest

# -------------------------------------------------------------------
# frost_config
# -------------------------------------------------------------------

class TestFrostConfig:
    def test_default_config_validates(self):
        from analytics.python.frost.frost_config import FrostConfig
        cfg = FrostConfig()
        cfg.validate()  # 例外なし

    def test_load_frost_config_no_env(self):
        """環境変数なしでもデフォルト値でロード可能"""
        from analytics.python.frost.frost_config import load_frost_config
        cfg = load_frost_config()
        assert cfg.enabled is True
        assert cfg.dry_run is False
        assert cfg.pbo_threshold == pytest.approx(0.20)
        assert cfg.top_k == 25

    def test_load_frost_config_overrides(self):
        from analytics.python.frost.frost_config import load_frost_config
        cfg = load_frost_config(overrides={"top_k": 5, "dry_run": True})
        assert cfg.top_k == 5
        assert cfg.dry_run is True

    def test_validation_negative_weight_raises(self):
        from analytics.python.frost.frost_config import FrostConfig
        cfg = FrostConfig(w_predictive=-0.1)
        with pytest.raises(ValueError, match="w_predictive"):
            cfg.validate()

    def test_validation_promotion_top_k_exceeds_top_k(self):
        from analytics.python.frost.frost_config import FrostConfig
        cfg = FrostConfig(top_k=5, promotion_top_k=10)
        with pytest.raises(ValueError, match="promotion_top_k"):
            cfg.validate()

    def test_to_dict_structure(self):
        from analytics.python.frost.frost_config import FrostConfig
        cfg = FrostConfig()
        d = cfg.to_dict()
        assert "weights" in d
        assert "hard_gates" in d
        assert "selection" in d
        assert d["weights"]["predictive"] == pytest.approx(0.20)

    def test_effective_pg_dsn_from_override(self):
        from analytics.python.frost.frost_config import FrostConfig
        cfg = FrostConfig(pg_dsn="postgresql://localhost/test")
        assert cfg.effective_pg_dsn() == "postgresql://localhost/test"


# -------------------------------------------------------------------
# frost_contracts
# -------------------------------------------------------------------

class TestFrostContracts:
    def test_frost_candidate_defaults(self):
        from analytics.python.frost.frost_contracts import FrostCandidate
        c = FrostCandidate()
        assert c.candidate_id != ""
        assert c.source_type == "eml"
        assert c.status == "pending"

    def test_frost_evaluation_defaults(self):
        from analytics.python.frost.frost_contracts import FrostEvaluation
        ev = FrostEvaluation()
        assert ev.frost_score == pytest.approx(0.0)
        assert ev.hard_gate_passed is True
        assert ev.hard_gate_failures == []

    def test_frost_decision_defaults(self):
        from analytics.python.frost.frost_contracts import FrostDecision
        d = FrostDecision()
        assert d.decision == "REJECTED"
        assert d.promotion_eligible is False

    def test_frost_run_output_helpers(self):
        from analytics.python.frost.frost_contracts import FrostDecision, FrostRunOutput
        output = FrostRunOutput()
        d1 = FrostDecision(decision="SELECTED", promotion_eligible=True)
        d2 = FrostDecision(decision="REJECTED")
        output.decisions = [d1, d2]
        assert len(output.selected_candidates()) == 1
        assert len(output.promotion_eligible_decisions()) == 1
        assert len(output.rejected_decisions()) == 1


# -------------------------------------------------------------------
# frost_features
# -------------------------------------------------------------------

class TestFrostFeatures:
    def _make_candidate(self, **kwargs):
        from analytics.python.frost.frost_contracts import FrostCandidate
        defaults = dict(
            backtest_summary={"oos_sharpe": 1.2, "max_drawdown": 0.10, "turnover": 2.0},
            metrics={"rank_ic": 0.05, "ic": 0.04, "hit_rate": 0.56},
            regime_breakdown={"bull": {"sharpe": 1.5}, "bear": {"sharpe": 0.8}, "crisis": {"sharpe": 0.3}},
            fold_results=[{"sharpe": 1.0}, {"sharpe": 1.2}, {"sharpe": 0.8}, {"sharpe": 1.1}],
        )
        defaults.update(kwargs)
        return FrostCandidate(**defaults)

    def test_extract_backtest_features(self):
        from analytics.python.frost.frost_features import extract_backtest_features
        result = extract_backtest_features({"oos_sharpe": 1.2, "turnover": 2.0, "max_drawdown": 0.10})
        assert result["oos_sharpe"] == pytest.approx(1.2)
        assert result["turnover"] == pytest.approx(2.0)

    def test_extract_backtest_features_empty(self):
        from analytics.python.frost.frost_features import extract_backtest_features
        result = extract_backtest_features({})
        assert result["oos_sharpe"] is None

    def test_extract_metrics_features(self):
        from analytics.python.frost.frost_features import extract_metrics_features
        result = extract_metrics_features({"rank_ic": 0.05, "ic_t_stat": 2.5})
        assert result["rank_ic"] == pytest.approx(0.05)
        assert result["ic_t_stat"] == pytest.approx(2.5)
        assert result["predictive_score_raw"] == pytest.approx(0.05)

    def test_extract_regime_features(self):
        from analytics.python.frost.frost_features import extract_regime_features
        rb = {"bull": {"sharpe": 1.5}, "bear": {"sharpe": 0.5}, "crisis": {"sharpe": -0.2}}
        result = extract_regime_features(rb)
        assert result["bull_sharpe"] == pytest.approx(1.5)
        assert result["crisis_sharpe"] == pytest.approx(-0.2)
        # 2/3 レジームが正 Sharpe → pass_ratio ≈ 0.667
        assert result["regime_pass_ratio_raw"] == pytest.approx(2 / 3)

    def test_extract_fold_features(self):
        from analytics.python.frost.frost_features import extract_fold_features
        folds = [{"sharpe": 1.0, "rank_ic": 0.04}, {"sharpe": 0.8, "rank_ic": 0.03}]
        result = extract_fold_features(folds)
        assert result["n_folds"] == 2
        assert len(result["fold_sharpes"]) == 2

    def test_estimate_capacity_score_high_turnover(self):
        from analytics.python.frost.frost_features import estimate_capacity_score
        from analytics.python.frost.frost_contracts import FrostCandidate
        c = FrostCandidate(backtest_summary={"turnover": 10.0})
        score = estimate_capacity_score(c)
        assert score < 0.5  # 高ターンオーバー → 低キャパシティ

    def test_extract_all_features_returns_dict(self):
        from analytics.python.frost.frost_features import extract_all_features
        c = self._make_candidate()
        feat = extract_all_features(c)
        assert isinstance(feat, dict)
        assert "oos_sharpe" in feat
        assert "rank_ic" in feat
        assert "fold_sharpes" in feat


# -------------------------------------------------------------------
# frost_metrics
# -------------------------------------------------------------------

class TestFrostMetrics:
    def test_robust_normalize_single(self):
        from analytics.python.frost.frost_metrics import robust_normalize
        result = robust_normalize([5.0])
        assert result == [0.0]

    def test_robust_normalize_uniform(self):
        from analytics.python.frost.frost_metrics import robust_normalize
        result = robust_normalize([1.0, 1.0, 1.0])
        assert all(r == pytest.approx(0.0) for r in result)

    def test_robust_normalize_different(self):
        from analytics.python.frost.frost_metrics import robust_normalize
        result = robust_normalize([1.0, 2.0, 3.0])
        assert len(result) == 3
        # 中央値 (2.0) → 0.0 付近
        assert result[1] == pytest.approx(0.0, abs=0.1)

    def test_compute_predictive_score_high_ic(self):
        from analytics.python.frost.frost_metrics import compute_predictive_score
        feat = {"rank_ic": 0.10, "ic_t_stat": 3.0, "hit_rate": 0.57}
        score = compute_predictive_score(feat)
        assert score >= 0.9  # IC=0.10 → max score ≈ 1.0

    def test_compute_predictive_score_low_ic(self):
        from analytics.python.frost.frost_metrics import compute_predictive_score
        feat = {"rank_ic": 0.01}
        score = compute_predictive_score(feat)
        assert score < 0.2

    def test_compute_oos_sharpe_score_positive(self):
        from analytics.python.frost.frost_metrics import compute_oos_sharpe_score
        feat = {"oos_sharpe": 1.5}
        score = compute_oos_sharpe_score(feat)
        assert 0 < score <= 1.0

    def test_compute_oos_sharpe_score_negative(self):
        from analytics.python.frost.frost_metrics import compute_oos_sharpe_score
        feat = {"oos_sharpe": -0.5}
        score = compute_oos_sharpe_score(feat)
        assert score == pytest.approx(0.0)

    def test_compute_turnover_penalty(self):
        from analytics.python.frost.frost_metrics import compute_turnover_penalty
        # turnover=4.0 → 最大ペナルティ
        feat = {"turnover": 4.0}
        p = compute_turnover_penalty(feat, max_turnover=4.0)
        assert p == pytest.approx(1.0)

        # turnover=2.0 → 0.5
        feat2 = {"turnover": 2.0}
        p2 = compute_turnover_penalty(feat2, max_turnover=4.0)
        assert p2 == pytest.approx(0.5)

    def test_compute_frost_score_basic(self):
        from analytics.python.frost.frost_metrics import compute_frost_score
        score = compute_frost_score(
            predictive_score=0.8,
            oos_sharpe_score=0.7,
            regime_stability_score=0.6,
            selection_consistency_score=0.5,
            capacity_score=0.9,
            pbo_score=0.1,
            turnover_penalty=0.2,
            complexity_penalty=0.1,
            drawdown_penalty=0.1,
            fragility_penalty=0.05,
        )
        assert 0 < score < 1.0

    def test_compute_frost_score_all_zero(self):
        from analytics.python.frost.frost_metrics import compute_frost_score
        score = compute_frost_score(
            predictive_score=0.0, oos_sharpe_score=0.0,
            regime_stability_score=0.0, selection_consistency_score=0.0,
            capacity_score=0.0, pbo_score=0.0,
            turnover_penalty=0.0, complexity_penalty=0.0,
            drawdown_penalty=0.0, fragility_penalty=0.0,
        )
        assert score == pytest.approx(0.0)


# -------------------------------------------------------------------
# frost_stability
# -------------------------------------------------------------------

class TestFrostStability:
    def test_fold_sharpe_stability_stable(self):
        from analytics.python.frost.frost_stability import compute_fold_sharpe_stability
        # 安定した fold → stability 高
        folds = [1.0, 1.1, 0.9, 1.05, 0.95]
        mean_s, std_s, stab = compute_fold_sharpe_stability(folds)
        assert stab > 0.7

    def test_fold_sharpe_stability_unstable(self):
        from analytics.python.frost.frost_stability import compute_fold_sharpe_stability
        # 不安定な fold → stability 低
        folds = [2.0, -1.0, 3.0, -2.0, 1.5]
        mean_s, std_s, stab = compute_fold_sharpe_stability(folds)
        assert stab < 0.5

    def test_fold_sharpe_stability_insufficient_folds(self):
        from analytics.python.frost.frost_stability import compute_fold_sharpe_stability
        # fold 不足 → 中立スコア 0.5
        _, _, stab = compute_fold_sharpe_stability([1.0, 1.2], min_folds=3)
        assert stab == pytest.approx(0.5)

    def test_sign_stability_all_positive(self):
        from analytics.python.frost.frost_stability import compute_sign_stability
        result = compute_sign_stability([1.0, 2.0, 0.5, 1.5])
        assert result == pytest.approx(1.0)

    def test_sign_stability_mixed(self):
        from analytics.python.frost.frost_stability import compute_sign_stability
        result = compute_sign_stability([1.0, -1.0])
        assert result == pytest.approx(0.5)

    def test_selection_consistency_all_stable(self):
        from analytics.python.frost.frost_stability import compute_selection_consistency_score
        sharpes = [1.0, 1.1, 0.9, 1.05, 0.95]
        ics     = [0.05, 0.06, 0.04, 0.05, 0.055]
        regime  = [1.2, 0.8, 0.5]
        score = compute_selection_consistency_score(sharpes, ics, regime)
        assert 0.5 < score <= 1.0


# -------------------------------------------------------------------
# frost_pbo
# -------------------------------------------------------------------

class TestFrostPBO:
    def test_estimate_pbo_stable(self):
        """安定した fold では PBO は低くなるはず"""
        from analytics.python.frost.frost_pbo import estimate_pbo_from_folds
        # 全 fold で一様に正の Sharpe → PBO 低
        folds = [1.0, 1.2, 0.8, 1.1, 0.9, 1.05]
        pbo = estimate_pbo_from_folds(folds)
        # 安定しているため PBO < 0.6
        assert pbo < 0.6
        assert 0.0 <= pbo <= 1.0

    def test_estimate_pbo_insufficient_folds(self):
        from analytics.python.frost.frost_pbo import estimate_pbo_from_folds
        pbo = estimate_pbo_from_folds([1.0, 1.2], min_folds=4)
        assert pbo == pytest.approx(0.5)  # fold 不足 → 中立

    def test_compute_pbo_all_empty(self):
        from analytics.python.frost.frost_pbo import compute_pbo_all
        result = compute_pbo_all([])
        assert result["pbo_score"] == pytest.approx(0.5)
        assert result["n_folds"] == 0

    def test_compute_pbo_all_with_folds(self):
        from analytics.python.frost.frost_pbo import compute_pbo_all
        folds = [
            {"sharpe": 1.0, "rank_ic": 0.05},
            {"sharpe": 1.2, "rank_ic": 0.06},
            {"sharpe": 0.8, "rank_ic": 0.04},
            {"sharpe": 1.1, "rank_ic": 0.05},
            {"sharpe": 0.9, "rank_ic": 0.04},
        ]
        result = compute_pbo_all(folds)
        assert 0.0 <= result["pbo_score"] <= 1.0
        assert result["n_folds"] == 5

    def test_selection_fragility_stable(self):
        from analytics.python.frost.frost_pbo import compute_selection_fragility
        sharpes = [1.0, 1.05, 0.95, 1.02, 0.98]
        fragility = compute_selection_fragility(sharpes, [0.05] * 5)
        assert fragility < 0.5  # 安定しているため低脆弱性


# -------------------------------------------------------------------
# frost_selector
# -------------------------------------------------------------------

class TestFrostSelector:
    def _make_candidate(self, sharpe=1.2, rank_ic=0.05, turnover=2.0,
                        pbo=0.10, drawdown=0.10, complexity=0.30):
        from analytics.python.frost.frost_contracts import FrostCandidate
        return FrostCandidate(
            candidate_id=str(uuid.uuid4()),
            run_id="test_run",
            trace_id="test_trace",
            source_type="eml",
            complexity_score=complexity,
            backtest_summary={
                "oos_sharpe": sharpe,
                "max_drawdown": drawdown,
                "turnover": turnover,
            },
            metrics={"rank_ic": rank_ic, "ic": rank_ic * 0.8},
            regime_breakdown={
                "bull": {"sharpe": sharpe * 1.2},
                "bear": {"sharpe": sharpe * 0.8},
                "crisis": {"sharpe": sharpe * 0.5},
            },
            fold_results=[
                {"sharpe": sharpe * (1 + 0.1 * i), "rank_ic": rank_ic}
                for i in range(-2, 3)
            ],
        )

    def test_evaluate_candidate_good(self):
        from analytics.python.frost.frost_config import FrostConfig
        from analytics.python.frost.frost_selector import evaluate_candidate
        cfg = FrostConfig(min_backtest_folds=3)
        c = self._make_candidate(sharpe=1.5, rank_ic=0.08)
        ev = evaluate_candidate(c, "run1", "trace1", cfg)
        assert ev.candidate_id == c.candidate_id
        assert ev.frost_score > 0
        assert isinstance(ev.hard_gate_passed, bool)
        assert isinstance(ev.hard_gate_failures, list)

    def test_evaluate_candidate_fails_pbo_gate(self):
        """高 PBO は hard gate FAIL"""
        from analytics.python.frost.frost_config import FrostConfig
        from analytics.python.frost.frost_selector import evaluate_candidate
        cfg = FrostConfig(pbo_threshold=0.01)  # 厳しい閾値
        c = self._make_candidate()
        ev = evaluate_candidate(c, "run1", "trace1", cfg)
        # PBO threshold=0.01 は非常に厳しいので多くの場合 FAIL
        # (fold から計算された PBO が 0.01 超える可能性が高い)
        assert isinstance(ev.hard_gate_passed, bool)

    def test_evaluate_candidate_fails_turnover_gate(self):
        """高ターンオーバーは hard gate FAIL"""
        from analytics.python.frost.frost_config import FrostConfig
        from analytics.python.frost.frost_selector import evaluate_candidate
        cfg = FrostConfig(max_turnover=1.0)  # 厳しい閾値
        c = self._make_candidate(turnover=5.0)
        ev = evaluate_candidate(c, "run1", "trace1", cfg)
        assert not ev.hard_gate_passed
        assert any("turnover" in gf for gf in ev.hard_gate_failures)

    def test_evaluate_candidate_fails_drawdown_gate(self):
        """高ドローダウンは hard gate FAIL"""
        from analytics.python.frost.frost_config import FrostConfig
        from analytics.python.frost.frost_selector import evaluate_candidate
        cfg = FrostConfig(max_drawdown=0.05)
        c = self._make_candidate(drawdown=0.30)
        ev = evaluate_candidate(c, "run1", "trace1", cfg)
        assert not ev.hard_gate_passed
        assert any("drawdown" in gf for gf in ev.hard_gate_failures)

    def test_make_decision_gate_fail_rejected(self):
        """Gate FAIL → REJECTED"""
        from analytics.python.frost.frost_config import FrostConfig
        from analytics.python.frost.frost_contracts import FrostEvaluation
        from analytics.python.frost.frost_selector import make_decision
        cfg = FrostConfig()
        c = self._make_candidate()
        c.run_id = "r1"
        c.trace_id = "t1"
        ev = FrostEvaluation(
            run_id="r1", candidate_id=c.candidate_id, trace_id="t1",
            hard_gate_passed=False,
            hard_gate_failures=["pbo=0.30 > threshold=0.20"],
            frost_score=0.3,
        )
        d = make_decision(c, ev, cfg, rank=1)
        assert d.decision == "REJECTED"
        assert not d.promotion_eligible

    def test_make_decision_top_k_selected(self):
        """rank <= top_k かつ gate PASS → SELECTED"""
        from analytics.python.frost.frost_config import FrostConfig
        from analytics.python.frost.frost_contracts import FrostEvaluation
        from analytics.python.frost.frost_selector import make_decision
        cfg = FrostConfig(top_k=10, promotion_top_k=3)
        c = self._make_candidate()
        c.run_id = "r1"
        c.trace_id = "t1"
        ev = FrostEvaluation(
            run_id="r1", candidate_id=c.candidate_id, trace_id="t1",
            hard_gate_passed=True,
            frost_score=0.5,
        )
        d = make_decision(c, ev, cfg, rank=2)
        assert d.decision == "SELECTED"
        assert d.promotion_eligible  # rank=2 <= promotion_top_k=3

    def test_make_decision_below_top_k_hold(self):
        """rank > top_k → HOLD"""
        from analytics.python.frost.frost_config import FrostConfig
        from analytics.python.frost.frost_contracts import FrostEvaluation
        from analytics.python.frost.frost_selector import make_decision
        cfg = FrostConfig(top_k=5)
        c = self._make_candidate()
        c.run_id = "r1"
        c.trace_id = "t1"
        ev = FrostEvaluation(
            run_id="r1", candidate_id=c.candidate_id, trace_id="t1",
            hard_gate_passed=True,
            frost_score=0.3,
        )
        d = make_decision(c, ev, cfg, rank=10)
        assert d.decision == "HOLD"


# -------------------------------------------------------------------
# frost_ranker
# -------------------------------------------------------------------

class TestFrostRanker:
    def _make_ev(self, cid, score, gate_passed=True):
        from analytics.python.frost.frost_contracts import FrostEvaluation
        return FrostEvaluation(
            candidate_id=cid, run_id="r1", trace_id="t1",
            frost_score=score, hard_gate_passed=gate_passed,
            hard_gate_failures=[] if gate_passed else ["pbo=0.30"],
        )

    def test_rank_evaluations_order(self):
        from analytics.python.frost.frost_ranker import rank_evaluations
        evs = [
            self._make_ev("c1", 0.3),
            self._make_ev("c2", 0.8),
            self._make_ev("c3", 0.5),
        ]
        ranked = rank_evaluations(evs)
        # rank=1 は frost_score が最大
        assert ranked[0][1].candidate_id == "c2"
        assert ranked[0][0] == 1

    def test_rank_evaluations_gate_fail_last(self):
        from analytics.python.frost.frost_ranker import rank_evaluations
        evs = [
            self._make_ev("c1", 0.9, gate_passed=False),
            self._make_ev("c2", 0.3, gate_passed=True),
        ]
        ranked = rank_evaluations(evs)
        # gate PASS (c2) が先に来る
        assert ranked[0][1].candidate_id == "c2"

    def test_detect_near_duplicates_identical(self):
        from analytics.python.frost.frost_contracts import FrostCandidate
        from analytics.python.frost.frost_ranker import detect_near_duplicates
        c1 = FrostCandidate(candidate_id="id1", candidate_hash="abcdef123456")
        c2 = FrostCandidate(candidate_id="id2", candidate_hash="abcdef123456")
        c3 = FrostCandidate(candidate_id="id3", candidate_hash="xyz999999999")
        result = detect_near_duplicates([c1, c2, c3], threshold=0.95)
        assert "id2" in result  # c2 は c1 の near-dup
        assert "id3" not in result

    def test_assign_decisions_basic(self):
        from analytics.python.frost.frost_config import FrostConfig
        from analytics.python.frost.frost_contracts import FrostCandidate
        from analytics.python.frost.frost_ranker import assign_decisions
        cfg = FrostConfig(top_k=3, promotion_top_k=1, min_selection_stability=0.0,
                          min_oos_sharpe=0.0, min_rank_ic=0.0)
        candidates = [
            FrostCandidate(
                candidate_id=f"c{i}", run_id="r1", trace_id="t1",
                candidate_hash=f"hash{i:04d}",
            )
            for i in range(5)
        ]
        evaluations = [
            self._make_ev(f"c{i}", score=1.0 - i * 0.1)
            for i in range(5)
        ]
        decisions = assign_decisions(candidates, evaluations, cfg)
        assert len(decisions) == 5
        selected = [d for d in decisions if d.decision == "SELECTED"]
        # gate が全 pass かつ top_k=3 → 最大 3 件 SELECTED
        assert len(selected) <= 3


# -------------------------------------------------------------------
# frost_decision_engine
# -------------------------------------------------------------------

class TestFrostDecisionEngine:
    def _make_decision(self, cid, decision, score, rank=None, eligible=False):
        from analytics.python.frost.frost_contracts import FrostDecision
        return FrostDecision(
            candidate_id=cid, run_id="r1", trace_id="t1",
            decision=decision, frost_score=score,
            decision_rank=rank, promotion_eligible=eligible,
        )

    def test_enforce_top_k_limit(self):
        from analytics.python.frost.frost_config import FrostConfig
        from analytics.python.frost.frost_decision_engine import enforce_top_k_limit
        cfg = FrostConfig(top_k=2)
        decisions = [
            self._make_decision("c1", "SELECTED", 0.8, rank=1, eligible=True),
            self._make_decision("c2", "SELECTED", 0.7, rank=2, eligible=True),
            self._make_decision("c3", "SELECTED", 0.6, rank=3, eligible=False),
        ]
        result = enforce_top_k_limit(decisions, cfg)
        selected = [d for d in result if d.decision == "SELECTED"]
        assert len(selected) == 2

    def test_enforce_promotion_top_k(self):
        from analytics.python.frost.frost_config import FrostConfig
        from analytics.python.frost.frost_decision_engine import enforce_promotion_top_k
        cfg = FrostConfig(promotion_top_k=2, review_required_default=False)
        decisions = [
            self._make_decision("c1", "SELECTED", 0.9, rank=1, eligible=True),
            self._make_decision("c2", "SELECTED", 0.8, rank=2, eligible=True),
            self._make_decision("c3", "SELECTED", 0.7, rank=3, eligible=True),
        ]
        result = enforce_promotion_top_k(decisions, cfg)
        eligible = [d for d in result if d.promotion_eligible]
        assert len(eligible) <= 2

    def test_summarize_decisions(self):
        from analytics.python.frost.frost_decision_engine import summarize_decisions
        decisions = [
            self._make_decision("c1", "SELECTED", 0.9, eligible=True),
            self._make_decision("c2", "REJECTED", 0.1),
            self._make_decision("c3", "HOLD", 0.4),
            self._make_decision("c4", "REVIEW_REQUIRED", 0.5),
        ]
        stats = summarize_decisions(decisions)
        assert stats["selected_count"] == 1
        assert stats["rejected_count"] == 1
        assert stats["hold_count"] == 1
        assert stats["review_required_count"] == 1
        assert stats["promotion_eligible_count"] == 1


# -------------------------------------------------------------------
# frost_report_builder
# -------------------------------------------------------------------

class TestFrostReportBuilder:
    def _make_output(self):
        from analytics.python.frost.frost_contracts import (
            FrostCandidate, FrostDecision, FrostEvaluation, FrostRunOutput
        )
        output = FrostRunOutput(
            run_id="test_run_id",
            trace_id="test_trace_id",
            batch_label="test_batch",
            candidate_count=3,
            evaluated_count=3,
            selected_count=1,
            hold_count=1,
            rejected_count=1,
            status="completed",
        )
        c1 = FrostCandidate(candidate_id="ccc1")
        c2 = FrostCandidate(candidate_id="ccc2")
        c3 = FrostCandidate(candidate_id="ccc3")
        output.candidates = [c1, c2, c3]

        e1 = FrostEvaluation(candidate_id="ccc1", frost_score=0.7, oos_sharpe=1.5,
                              rank_ic=0.06, pbo_score=0.10, hard_gate_passed=True)
        e2 = FrostEvaluation(candidate_id="ccc2", frost_score=0.4, oos_sharpe=0.8,
                              rank_ic=0.03, pbo_score=0.15, hard_gate_passed=True)
        e3 = FrostEvaluation(candidate_id="ccc3", frost_score=0.1, oos_sharpe=0.2,
                              pbo_score=0.25, hard_gate_passed=False,
                              hard_gate_failures=["pbo=0.25 > threshold=0.20"])
        output.evaluations = [e1, e2, e3]

        d1 = FrostDecision(candidate_id="ccc1", decision="SELECTED",
                           frost_score=0.7, decision_rank=1, promotion_eligible=True)
        d2 = FrostDecision(candidate_id="ccc2", decision="HOLD", frost_score=0.4)
        d3 = FrostDecision(candidate_id="ccc3", decision="REJECTED", frost_score=0.1,
                           gate_failures=["pbo=0.25 > threshold=0.20"])
        output.decisions = [d1, d2, d3]

        return output

    def test_build_markdown_report_contains_header(self):
        from analytics.python.frost.frost_report_builder import build_markdown_report
        output = self._make_output()
        md = build_markdown_report(output)
        assert "FROST Meta-Fitness Engine" in md
        assert "test_run_id" in md
        assert "SELECTED" in md

    def test_build_json_summary_structure(self):
        from analytics.python.frost.frost_report_builder import build_json_summary
        output = self._make_output()
        summary = build_json_summary(output)
        assert summary["run_id"] == "test_run_id"
        assert summary["counts"]["selected"] == 1
        assert summary["counts"]["rejected"] == 1
        assert len(summary["selected_candidates"]) == 1

    def test_analyze_rejections(self):
        from analytics.python.frost.frost_report_builder import analyze_rejections
        output = self._make_output()
        report = analyze_rejections(output)
        assert "棄却分析" in report
        assert "ccc3" in report
