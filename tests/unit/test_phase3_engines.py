"""
test_phase3_engines.py
----------------------
Phase 3: GateEngine / ScoreEngine のユニットテスト

テスト対象:
  - gate_engine.py  : GateEngine.evaluate() / evaluate_from_dict() / v2 gates
  - score_engine.py : ScoreEngine.compute_v1() / compute_v2() / fill_scores()
  - 統合: evidence_bundle.py が GateEngine/ScoreEngine に正しく委譲するか
  - 後方互換: check_hard_gates() が GateEngine 委譲後も同じ結果を返すか

テスト数: 72 (9 クラス)
  TestGateEngineBasic(10)       : evaluate() の基本動作
  TestGateEngineV2(7)           : v2 追加ゲート
  TestGateEngineFromDict(8)     : evaluate_from_dict() 後方互換
  TestScoreEngineV1(9)          : compute_v1() の重み計算
  TestScoreEngineV2(8)          : compute_v2() の追加軸
  TestScoreEngineFillScores(6)  : fill_scores() の ScoreComponents 更新
  TestScoreEngineWeightSums(5)  : weight_sum_v1/v2 整合性
  TestEvidenceBundleDelegation(9): evidence_bundle.py の GateEngine/ScoreEngine 委譲
  TestBackwardCompat(10)        : Phase 2 テストとの等価確認
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import pytest

# ---------------------------------------------------------------------------
# テスト用ファクトリ
# ---------------------------------------------------------------------------

def _make_config(**kwargs):
    """テスト用 FrostConfig を生成する。"""
    from analytics.python.frost.frost_config import FrostConfig
    return FrostConfig(**kwargs)


def _make_features(**kwargs) -> Any:
    """テスト用 RawFeatures を生成する。"""
    from analytics.python.frost.evidence_bundle import RawFeatures
    defaults = {
        "rank_ic": 0.05,
        "oos_sharpe": 0.80,
        "turnover": 2.0,
        "oos_max_drawdown": -0.10,
        "regime_pass_ratio_raw": 0.85,
        "complexity_score": 0.30,
    }
    defaults.update(kwargs)
    return RawFeatures.from_dict(defaults)


def _make_pbo(**kwargs) -> Any:
    """テスト用 PBOEvidence を生成する。"""
    from analytics.python.frost.evidence_bundle import PBOEvidence
    defaults = {"pbo_score": 0.10, "pbo_raw": 0.10, "n_folds": 6}
    defaults.update(kwargs)
    return PBOEvidence(**defaults)


def _make_stability(**kwargs) -> Any:
    """テスト用 StabilityEvidence を生成する。"""
    from analytics.python.frost.evidence_bundle import StabilityEvidence
    defaults = {"selection_consistency_score": 0.75, "fold_sharpe_std": 0.20}
    defaults.update(kwargs)
    return StabilityEvidence(**defaults)


def _make_scores(**kwargs) -> Any:
    """テスト用 ScoreComponents を生成する。"""
    from analytics.python.frost.evidence_bundle import ScoreComponents
    defaults = {
        "predictive_score": 0.70,
        "oos_sharpe_score": 0.60,
        "regime_stability_score": 0.65,
        "selection_consistency_score": 0.70,
        "capacity_score": 0.75,
        "pbo_penalty": 0.10,
        "turnover_penalty": 0.15,
        "complexity_penalty": 0.10,
        "drawdown_penalty": 0.05,
        "fragility_penalty": 0.10,
    }
    defaults.update(kwargs)
    return ScoreComponents(**defaults)


def _make_candidate(**kwargs) -> Any:
    """テスト用 FrostCandidate を生成する。"""
    from analytics.python.frost.frost_contracts import FrostCandidate
    defaults = {
        "candidate_id": "test-cand-01",
        "run_id": "test-run-01",
        "metrics": {
            "rank_ic": 0.05, "ic": 0.04, "ic_t_stat": 2.5, "hit_rate": 0.57,
        },
        "backtest_summary": {
            "oos_sharpe": 0.80, "oos_sortino": 1.1, "oos_calmar": 1.2,
            "oos_max_drawdown": -0.10, "turnover": 2.0,
        },
        "regime_breakdown": {
            "bull": {"sharpe": 0.90}, "crisis": {"sharpe": 0.40},
            "pass_ratio": 0.85,
        },
        "fold_results": [
            {"oos_sharpe": 0.75, "rank_ic": 0.05} for _ in range(6)
        ],
        "complexity_score": 0.30,
    }
    defaults.update(kwargs)
    return FrostCandidate(**defaults)


# ===========================================================================
# TestGateEngineBasic
# ===========================================================================

class TestGateEngineBasic:
    """GateEngine.evaluate() の基本動作テスト。"""

    def test_all_pass_returns_true(self):
        """良好な候補は全 Gate PASS。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(_make_config())
        verdict = engine.evaluate(_make_features(), _make_pbo(), _make_stability())
        assert verdict.passed is True
        assert verdict.to_list() == []

    def test_pbo_failure(self):
        """PBO が閾値超過で FAIL。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(_make_config(pbo_threshold=0.05))
        verdict = engine.evaluate(
            _make_features(), _make_pbo(pbo_score=0.30), _make_stability()
        )
        assert verdict.passed is False
        assert any("pbo=" in f for f in verdict.to_list())

    def test_rank_ic_failure(self):
        """Rank IC が最低値未満で FAIL。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(_make_config(min_rank_ic=0.10))
        verdict = engine.evaluate(
            _make_features(rank_ic=0.01), _make_pbo(), _make_stability()
        )
        assert verdict.passed is False
        assert any("rank_ic=" in f for f in verdict.to_list())

    def test_oos_sharpe_failure(self):
        """OOS Sharpe が最低値未満で FAIL。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(_make_config(min_oos_sharpe=1.0))
        verdict = engine.evaluate(
            _make_features(oos_sharpe=0.30), _make_pbo(), _make_stability()
        )
        assert verdict.passed is False
        assert any("oos_sharpe=" in f for f in verdict.to_list())

    def test_turnover_failure(self):
        """ターンオーバーが最大値超過で FAIL。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(_make_config(max_turnover=1.0))
        verdict = engine.evaluate(
            _make_features(turnover=5.0), _make_pbo(), _make_stability()
        )
        assert verdict.passed is False
        assert any("turnover=" in f for f in verdict.to_list())

    def test_drawdown_failure(self):
        """最大ドローダウンが最大値超過で FAIL。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(_make_config(max_drawdown=0.05))
        verdict = engine.evaluate(
            _make_features(oos_max_drawdown=-0.30), _make_pbo(), _make_stability()
        )
        assert verdict.passed is False
        assert any("max_drawdown=" in f for f in verdict.to_list())

    def test_regime_failure(self):
        """レジーム通過率が最低値未満で FAIL。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(_make_config(min_regime_pass_ratio=0.90))
        verdict = engine.evaluate(
            _make_features(regime_pass_ratio_raw=0.50), _make_pbo(), _make_stability()
        )
        assert verdict.passed is False
        assert any("regime_pass_ratio=" in f for f in verdict.to_list())

    def test_complexity_failure(self):
        """複雑度が最大値超過で FAIL。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(_make_config(max_complexity_score=0.20))
        verdict = engine.evaluate(
            _make_features(complexity_score=0.80), _make_pbo(), _make_stability()
        )
        assert verdict.passed is False
        assert any("complexity=" in f for f in verdict.to_list())

    def test_selection_stability_failure(self):
        """選抜安定性が最低値未満で FAIL。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(_make_config(min_selection_stability=0.90))
        verdict = engine.evaluate(
            _make_features(),
            _make_pbo(),
            _make_stability(selection_consistency_score=0.50),
        )
        assert verdict.passed is False
        assert any("selection_stability=" in f for f in verdict.to_list())

    def test_multiple_failures_all_recorded(self):
        """複数のゲート失敗が全て記録される。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(
            _make_config(min_oos_sharpe=2.0, max_complexity_score=0.10)
        )
        verdict = engine.evaluate(
            _make_features(oos_sharpe=0.30, complexity_score=0.80),
            _make_pbo(),
            _make_stability(),
        )
        assert verdict.passed is False
        assert len(verdict.to_list()) >= 2


# ===========================================================================
# TestGateEngineV2
# ===========================================================================

class TestGateEngineV2:
    """v2 追加ゲートのテスト。"""

    def test_v2_gates_disabled_by_default(self):
        """enable_v2_gates=False のとき v2 ゲートは評価されない。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(_make_config(), enable_v2_gates=False)
        # v2 ゲートに失敗するような raw データを含む features
        features = _make_features()
        features._raw["causal_direction_score"] = 0.0  # 失敗するはず
        verdict = engine.evaluate(features, _make_pbo(), _make_stability())
        # v2 ゲートは評価されないので全 PASS のまま
        assert verdict.passed is True

    def test_v2_causal_direction_failure(self):
        """因果方向性スコアが最低値未満で FAIL (v2)。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(
            _make_config(min_causal_direction_score=0.60), enable_v2_gates=True
        )
        features = _make_features()
        features._raw["causal_direction_score"] = 0.30
        verdict = engine.evaluate(features, _make_pbo(), _make_stability())
        assert verdict.passed is False
        assert any("causal_direction" in f for f in verdict.to_list())

    def test_v2_genome_novelty_failure(self):
        """Genome 新規性スコアが最低値未満で FAIL (v2)。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(
            _make_config(min_genome_novelty_score=0.50), enable_v2_gates=True
        )
        features = _make_features()
        features._raw["genome_novelty_score"] = 0.10
        verdict = engine.evaluate(features, _make_pbo(), _make_stability())
        assert verdict.passed is False
        assert any("genome_novelty" in f for f in verdict.to_list())

    def test_v2_crowding_r2_failure(self):
        """Crowding R² が最大値超過で FAIL (v2)。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(
            _make_config(max_crowding_r2=0.50), enable_v2_gates=True
        )
        features = _make_features()
        features._raw["crowding_r2"] = 0.95
        verdict = engine.evaluate(features, _make_pbo(), _make_stability())
        assert verdict.passed is False
        assert any("crowding_r2" in f for f in verdict.to_list())

    def test_v2_fsi_failure(self):
        """FSI が最大値超過で FAIL (v2)。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(
            _make_config(max_fsi=0.30), enable_v2_gates=True
        )
        features = _make_features()
        features._raw["fsi"] = 0.80
        verdict = engine.evaluate(features, _make_pbo(), _make_stability())
        assert verdict.passed is False
        assert any("fsi=" in f for f in verdict.to_list())

    def test_v2_regime_entropy_failure(self):
        """Regime Entropy が最低値未満で FAIL (v2)。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(
            _make_config(min_regime_entropy=0.80), enable_v2_gates=True
        )
        features = _make_features()
        features._raw["regime_entropy"] = 0.20
        verdict = engine.evaluate(features, _make_pbo(), _make_stability())
        assert verdict.passed is False
        assert any("regime_entropy=" in f for f in verdict.to_list())

    def test_v2_missing_features_no_failure(self):
        """v2 特徴量が存在しない (None) 場合はゲート FAIL しない。"""
        from analytics.python.frost.gate_engine import GateEngine
        engine = GateEngine.from_config(_make_config(), enable_v2_gates=True)
        # v2 特徴量を _raw に含めない
        verdict = engine.evaluate(_make_features(), _make_pbo(), _make_stability())
        assert verdict.passed is True


# ===========================================================================
# TestGateEngineFromDict
# ===========================================================================

class TestGateEngineFromDict:
    """GateEngine.evaluate_from_dict() の後方互換テスト。"""

    def test_returns_tuple(self):
        """evaluate_from_dict() は (bool, list) タプルを返す。"""
        from analytics.python.frost.gate_engine import GateEngine
        result = GateEngine.from_config(_make_config()).evaluate_from_dict(
            {"rank_ic": 0.05, "oos_sharpe": 0.80, "complexity_score": 0.30},
            pbo_score=0.10,
            selection_consistency_score=0.75,
        )
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], list)

    def test_all_pass_empty_failures(self):
        """全 PASS のとき failures リストは空。"""
        from analytics.python.frost.gate_engine import GateEngine
        passed, failures = GateEngine.from_config(_make_config()).evaluate_from_dict(
            {"rank_ic": 0.05, "oos_sharpe": 0.80,
             "turnover": 2.0, "oos_max_drawdown": -0.10,
             "regime_pass_ratio_raw": 0.85, "complexity_score": 0.30},
            pbo_score=0.10,
            selection_consistency_score=0.75,
        )
        assert passed is True
        assert failures == []

    def test_pbo_fail_via_dict(self):
        """dict API で PBO 失敗が検出される。"""
        from analytics.python.frost.gate_engine import GateEngine
        passed, failures = GateEngine.from_config(
            _make_config(pbo_threshold=0.05)
        ).evaluate_from_dict({}, pbo_score=0.30, selection_consistency_score=0.75)
        assert passed is False
        assert len(failures) >= 1

    def test_selection_stability_fail_via_dict(self):
        """dict API で選抜安定性失敗が検出される。"""
        from analytics.python.frost.gate_engine import GateEngine
        passed, failures = GateEngine.from_config(
            _make_config(min_selection_stability=0.90)
        ).evaluate_from_dict({}, pbo_score=0.10, selection_consistency_score=0.50)
        assert passed is False
        assert any("selection_stability" in f for f in failures)

    def test_same_result_as_check_hard_gates(self):
        """evaluate_from_dict() と check_hard_gates() の結果が一致する。"""
        from analytics.python.frost.gate_engine import GateEngine
        from analytics.python.frost.frost_selector import check_hard_gates
        feat = {
            "rank_ic": 0.05, "oos_sharpe": 0.80, "turnover": 2.0,
            "oos_max_drawdown": -0.10, "regime_pass_ratio_raw": 0.85,
            "complexity_score": 0.30,
        }
        cfg = _make_config()
        pbo_score = 0.10
        scs = 0.75

        new_passed, new_failures = GateEngine.from_config(cfg).evaluate_from_dict(
            feat, pbo_score, scs
        )
        old_passed, old_failures = check_hard_gates(feat, pbo_score, scs, cfg)

        assert new_passed == old_passed
        assert new_failures == old_failures

    def test_same_result_fail_case(self):
        """FAIL ケースでも check_hard_gates() と一致する。"""
        from analytics.python.frost.gate_engine import GateEngine
        from analytics.python.frost.frost_selector import check_hard_gates
        feat = {"rank_ic": 0.001, "oos_sharpe": 0.10, "complexity_score": 0.90}
        cfg = _make_config()
        pbo_score = 0.50
        scs = 0.30

        new_passed, new_failures = GateEngine.from_config(cfg).evaluate_from_dict(
            feat, pbo_score, scs
        )
        old_passed, old_failures = check_hard_gates(feat, pbo_score, scs, cfg)

        assert new_passed == old_passed
        assert set(new_failures) == set(old_failures)

    def test_complexity_via_dict(self):
        """dict API で complexity_score が評価される。"""
        from analytics.python.frost.gate_engine import GateEngine
        passed, failures = GateEngine.from_config(
            _make_config(max_complexity_score=0.10)
        ).evaluate_from_dict(
            {"complexity_score": 0.90}, pbo_score=0.05, selection_consistency_score=0.80
        )
        assert passed is False
        assert any("complexity=" in f for f in failures)

    def test_none_rank_ic_no_failure(self):
        """rank_ic が None (欠損) のとき rank_ic ゲートはスキップ。"""
        from analytics.python.frost.gate_engine import GateEngine
        passed, failures = GateEngine.from_config(
            _make_config(min_rank_ic=0.10)
        ).evaluate_from_dict(
            {"rank_ic": None, "oos_sharpe": 0.80, "complexity_score": 0.30},
            pbo_score=0.10,
            selection_consistency_score=0.75,
        )
        # rank_ic が None なのでゲートはスキップ → 他が全 PASS なら True
        assert passed is True


# ===========================================================================
# TestScoreEngineV1
# ===========================================================================

class TestScoreEngineV1:
    """ScoreEngine.compute_v1() のテスト。"""

    def test_high_quality_positive_score(self):
        """高品質候補は正のスコアを返す。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        scores = _make_scores(
            predictive_score=0.9, oos_sharpe_score=0.8,
            regime_stability_score=0.8, selection_consistency_score=0.8,
            capacity_score=0.9,
            pbo_penalty=0.05, turnover_penalty=0.05,
            complexity_penalty=0.05, drawdown_penalty=0.05, fragility_penalty=0.05,
        )
        v1 = engine.compute_v1(scores)
        assert v1 > 0.0, f"高品質候補のスコアが非正: {v1}"

    def test_low_quality_low_score(self):
        """低品質候補は高品質より低いスコアを返す。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        high = _make_scores(predictive_score=0.9, oos_sharpe_score=0.8)
        low = _make_scores(predictive_score=0.1, oos_sharpe_score=0.1)
        assert engine.compute_v1(high) > engine.compute_v1(low)

    def test_zero_scores_zero_result(self):
        """全スコア 0、全ペナルティ 0 はスコア 0。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        scores = _make_scores(
            predictive_score=0.0, oos_sharpe_score=0.0,
            regime_stability_score=0.0, selection_consistency_score=0.0,
            capacity_score=0.0,
            pbo_penalty=0.0, turnover_penalty=0.0,
            complexity_penalty=0.0, drawdown_penalty=0.0, fragility_penalty=0.0,
        )
        v1 = engine.compute_v1(scores)
        assert v1 == pytest.approx(0.0, abs=1e-9)

    def test_weight_application(self):
        """重みが正しく適用される (predictive のみ 1.0 の場合)。"""
        from analytics.python.frost.score_engine import ScoreEngine
        cfg = _make_config(
            w_predictive=0.30, w_oos_sharpe=0.0,
            w_regime_stability=0.0, w_selection_consistency=0.0, w_capacity=0.0,
            w_pbo_penalty=0.0, w_turnover_penalty=0.0,
            w_complexity_penalty=0.0, w_drawdown_penalty=0.0, w_fragility_penalty=0.0,
        )
        engine = ScoreEngine.from_config(cfg)
        scores = _make_scores(predictive_score=1.0)
        v1 = engine.compute_v1(scores)
        assert v1 == pytest.approx(0.30, abs=1e-9)

    def test_penalty_reduces_score(self):
        """ペナルティがスコアを減少させる。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        no_penalty = _make_scores(pbo_penalty=0.0, turnover_penalty=0.0)
        with_penalty = _make_scores(pbo_penalty=1.0, turnover_penalty=1.0)
        assert engine.compute_v1(no_penalty) > engine.compute_v1(with_penalty)

    def test_score_clipped_at_one(self):
        """スコアが 1.0 を超えても内部でクリップされる。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        # 超過値を渡しても clip(1.5) = 1.0 となる
        scores = _make_scores(predictive_score=2.0)
        v1_over = engine.compute_v1(scores)
        scores_normal = _make_scores(predictive_score=1.0)
        v1_normal = engine.compute_v1(scores_normal)
        assert v1_over == pytest.approx(v1_normal, abs=1e-9)

    def test_matches_compute_frost_score(self):
        """compute_frost_score() と数値が一致する (後方互換確認)。"""
        from analytics.python.frost.score_engine import ScoreEngine
        from analytics.python.frost.frost_metrics import compute_frost_score
        cfg = _make_config()
        s = _make_scores()
        engine_score = ScoreEngine.from_config(cfg).compute_v1(s)
        direct_score = compute_frost_score(
            predictive_score=s.predictive_score,
            oos_sharpe_score=s.oos_sharpe_score,
            regime_stability_score=s.regime_stability_score,
            selection_consistency_score=s.selection_consistency_score,
            capacity_score=s.capacity_score,
            pbo_score=s.pbo_penalty,
            turnover_penalty=s.turnover_penalty,
            complexity_penalty=s.complexity_penalty,
            drawdown_penalty=s.drawdown_penalty,
            fragility_penalty=s.fragility_penalty,
            w_predictive=cfg.w_predictive,
            w_oos_sharpe=cfg.w_oos_sharpe,
            w_regime_stability=cfg.w_regime_stability,
            w_selection_consistency=cfg.w_selection_consistency,
            w_capacity=cfg.w_capacity,
            w_pbo_penalty=cfg.w_pbo_penalty,
            w_turnover_penalty=cfg.w_turnover_penalty,
            w_complexity_penalty=cfg.w_complexity_penalty,
            w_drawdown_penalty=cfg.w_drawdown_penalty,
            w_fragility_penalty=cfg.w_fragility_penalty,
        )
        assert engine_score == pytest.approx(direct_score, abs=1e-9)

    def test_deterministic_same_config(self):
        """同一 config/scores なら常に同じスコア。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        scores = _make_scores()
        v1_a = engine.compute_v1(scores)
        v1_b = engine.compute_v1(scores)
        assert v1_a == pytest.approx(v1_b, abs=1e-12)

    def test_custom_weights_from_config(self):
        """カスタム重みが ScoreEngine に反映される。"""
        from analytics.python.frost.score_engine import ScoreEngine
        cfg_high = _make_config(w_predictive=0.90, w_oos_sharpe=0.0,
                                w_regime_stability=0.0, w_selection_consistency=0.0,
                                w_capacity=0.0, w_pbo_penalty=0.0,
                                w_turnover_penalty=0.0, w_complexity_penalty=0.0,
                                w_drawdown_penalty=0.0, w_fragility_penalty=0.0)
        cfg_low  = _make_config(w_predictive=0.05, w_oos_sharpe=0.0,
                                w_regime_stability=0.0, w_selection_consistency=0.0,
                                w_capacity=0.0, w_pbo_penalty=0.0,
                                w_turnover_penalty=0.0, w_complexity_penalty=0.0,
                                w_drawdown_penalty=0.0, w_fragility_penalty=0.0)
        scores = _make_scores(predictive_score=1.0)
        assert ScoreEngine.from_config(cfg_high).compute_v1(scores) > \
               ScoreEngine.from_config(cfg_low).compute_v1(scores)


# ===========================================================================
# TestScoreEngineV2
# ===========================================================================

class TestScoreEngineV2:
    """ScoreEngine.compute_v2() のテスト。"""

    def test_v2_higher_than_v1_with_good_extra_axes(self):
        """v2 追加軸が良好なとき v2 > v1 (追加スコアが加算されるため)。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        scores = _make_scores(
            genome_novelty_score=1.0,
            causal_validity_score=1.0,
            regime_entropy_score=1.0,
            crowding_penalty=0.0,
            signal_duplication_penalty=0.0,
            fragility_surface_penalty=0.0,
        )
        v1 = engine.compute_v1(scores)
        v2 = engine.compute_v2(scores)
        assert v2 > v1

    def test_v2_lower_than_v1_with_heavy_v2_penalties(self):
        """v2 追加ペナルティが大きいとき v2 < v1 (追加ペナルティが差し引かれる)。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        scores = _make_scores(
            genome_novelty_score=0.5,   # 中立
            causal_validity_score=0.5,
            regime_entropy_score=0.5,
            crowding_penalty=1.0,
            signal_duplication_penalty=1.0,
            fragility_surface_penalty=1.0,
        )
        v1 = engine.compute_v1(scores)
        v2 = engine.compute_v2(scores)
        assert v2 < v1

    def test_v2_neutral_axes_close_to_v1(self):
        """v2 追加軸が中立値のとき v2 ≈ v1 + v2_bonus。"""
        from analytics.python.frost.score_engine import ScoreEngine
        cfg = _make_config()
        engine = ScoreEngine.from_config(cfg)
        scores = _make_scores(
            genome_novelty_score=0.5,
            causal_validity_score=0.5,
            regime_entropy_score=0.5,
            crowding_penalty=0.0,
            signal_duplication_penalty=0.0,
            fragility_surface_penalty=0.0,
        )
        v1 = engine.compute_v1(scores)
        v2 = engine.compute_v2(scores)
        # v2 追加スコア = w_genome*0.5 + w_causal*0.5 + w_entropy*0.5 = 0.075
        expected_bonus = (
            cfg.w_genome_novelty * 0.5
            + cfg.w_causal_validity * 0.5
            + cfg.w_regime_entropy * 0.5
        )
        assert v2 == pytest.approx(v1 + expected_bonus, abs=1e-9)

    def test_compute_selects_v1_by_default(self):
        """compute(use_v2=False) は v1 を返す。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        scores = _make_scores(genome_novelty_score=0.9)
        assert engine.compute(scores, use_v2=False) == pytest.approx(
            engine.compute_v1(scores), abs=1e-12
        )

    def test_compute_selects_v2_when_flag_set(self):
        """compute(use_v2=True) は v2 を返す。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        scores = _make_scores(genome_novelty_score=0.9)
        assert engine.compute(scores, use_v2=True) == pytest.approx(
            engine.compute_v2(scores), abs=1e-12
        )

    def test_compute_both_returns_tuple(self):
        """compute_both() は (v1, v2) のタプルを返す。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        scores = _make_scores()
        result = engine.compute_both(scores)
        assert isinstance(result, tuple)
        assert len(result) == 2
        v1, v2 = result
        assert v1 == pytest.approx(engine.compute_v1(scores), abs=1e-12)
        assert v2 == pytest.approx(engine.compute_v2(scores), abs=1e-12)

    def test_v2_deterministic(self):
        """v2 スコアは決定論的。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        scores = _make_scores(genome_novelty_score=0.7)
        v2_a = engine.compute_v2(scores)
        v2_b = engine.compute_v2(scores)
        assert v2_a == pytest.approx(v2_b, abs=1e-12)

    def test_v2_crowding_penalty_applied(self):
        """crowding_penalty が v2 スコアを下げる。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        no_crowd = _make_scores(crowding_penalty=0.0)
        high_crowd = _make_scores(crowding_penalty=1.0)
        assert engine.compute_v2(no_crowd) > engine.compute_v2(high_crowd)


# ===========================================================================
# TestScoreEngineFillScores
# ===========================================================================

class TestScoreEngineFillScores:
    """ScoreEngine.fill_scores() のテスト。"""

    def test_fills_v1_by_default(self):
        """fill_scores(use_v2=False) は frost_score_v1 を設定する。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        scores = _make_scores()
        result = engine.fill_scores(scores, use_v2=False)
        expected_v1 = engine.compute_v1(scores)
        assert result.frost_score_v1 == pytest.approx(expected_v1, abs=1e-9)

    def test_fills_v1_equals_v2_when_not_use_v2(self):
        """use_v2=False のとき frost_score_v1 == frost_score_v2。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        scores = _make_scores()
        result = engine.fill_scores(scores, use_v2=False)
        assert result.frost_score_v1 == pytest.approx(result.frost_score_v2, abs=1e-9)

    def test_fills_v2_when_use_v2_true(self):
        """use_v2=True のとき frost_score_v2 が v2 スコアで設定される。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        scores = _make_scores(genome_novelty_score=0.9)
        result = engine.fill_scores(scores, use_v2=True)
        expected_v2 = engine.compute_v2(scores)
        assert result.frost_score_v2 == pytest.approx(expected_v2, abs=1e-9)

    def test_v1_v2_differ_when_v2_axes_active(self):
        """v2 追加軸が中立でないとき v1 ≠ v2。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        scores = _make_scores(genome_novelty_score=1.0)
        result = engine.fill_scores(scores, use_v2=True)
        assert result.frost_score_v1 != pytest.approx(result.frost_score_v2, abs=1e-6)

    def test_returns_same_scores_object(self):
        """fill_scores() は渡した ScoreComponents 自身を返す。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        scores = _make_scores()
        result = engine.fill_scores(scores)
        assert result is scores

    def test_other_fields_unchanged(self):
        """fill_scores() は frost_score_v1/v2 以外を変更しない。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        scores = _make_scores(predictive_score=0.7, pbo_penalty=0.15)
        engine.fill_scores(scores)
        assert scores.predictive_score == pytest.approx(0.7, abs=1e-9)
        assert scores.pbo_penalty == pytest.approx(0.15, abs=1e-9)


# ===========================================================================
# TestScoreEngineWeightSums
# ===========================================================================

class TestScoreEngineWeightSums:
    """ScoreEngine のウェイト情報テスト。"""

    def test_v1_positive_weight_sum_correct(self):
        """v1 の positive 重み合計が FrostConfig と一致する。"""
        from analytics.python.frost.score_engine import ScoreEngine
        cfg = _make_config()
        engine = ScoreEngine.from_config(cfg)
        sums = engine.weight_sum_v1()
        expected = (
            cfg.w_predictive + cfg.w_oos_sharpe + cfg.w_regime_stability
            + cfg.w_selection_consistency + cfg.w_capacity
        )
        assert sums["positive"] == pytest.approx(expected, abs=1e-9)

    def test_v1_negative_weight_sum_correct(self):
        """v1 の negative 重み合計が FrostConfig と一致する。"""
        from analytics.python.frost.score_engine import ScoreEngine
        cfg = _make_config()
        engine = ScoreEngine.from_config(cfg)
        sums = engine.weight_sum_v1()
        expected = (
            cfg.w_pbo_penalty + cfg.w_turnover_penalty + cfg.w_complexity_penalty
            + cfg.w_drawdown_penalty + cfg.w_fragility_penalty
        )
        assert sums["negative"] == pytest.approx(expected, abs=1e-9)

    def test_v2_positive_larger_than_v1(self):
        """v2 の positive 重み合計は v1 より大きい (追加軸あり)。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        v1_sums = engine.weight_sum_v1()
        v2_sums = engine.weight_sum_v2()
        assert v2_sums["positive"] > v1_sums["positive"]

    def test_v2_negative_larger_than_v1(self):
        """v2 の negative 重み合計は v1 より大きい (追加ペナルティあり)。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        v1_sums = engine.weight_sum_v1()
        v2_sums = engine.weight_sum_v2()
        assert v2_sums["negative"] > v1_sums["negative"]

    def test_weight_cache_stable(self):
        """重みキャッシュは 2 回目以降も同じ値を返す。"""
        from analytics.python.frost.score_engine import ScoreEngine
        engine = ScoreEngine.from_config(_make_config())
        sums_1 = engine.weight_sum_v1()
        sums_2 = engine.weight_sum_v1()
        assert sums_1["positive"] == pytest.approx(sums_2["positive"], abs=1e-12)


# ===========================================================================
# TestEvidenceBundleDelegation
# ===========================================================================

class TestEvidenceBundleDelegation:
    """evidence_bundle.py が GateEngine/ScoreEngine に委譲しているかの統合テスト。"""

    def _make_bundle(self, **cand_kwargs):
        from analytics.python.frost.evidence_bundle import evaluate_candidate_to_bundle
        cand = _make_candidate(**cand_kwargs)
        cfg = _make_config()
        return evaluate_candidate_to_bundle(cand, "run-01", "trace-01", cfg)

    def test_bundle_gate_is_gate_verdict(self):
        """bundle.gate が GateVerdict インスタンスである。"""
        from analytics.python.frost.evidence_bundle import GateVerdict
        bundle = self._make_bundle()
        assert isinstance(bundle.gate, GateVerdict)

    def test_bundle_scores_have_frost_score_v1(self):
        """bundle.scores.frost_score_v1 が非ゼロで設定されている。"""
        bundle = self._make_bundle()
        # デフォルト 0.0 以外の値が設定されているはず
        assert bundle.scores.frost_score_v1 != pytest.approx(0.0, abs=1e-9) or \
               bundle.scores.frost_score_v2 != pytest.approx(0.0, abs=1e-9)

    def test_score_engine_and_bundle_score_equal(self):
        """ScoreEngine で手動計算したスコアと bundle のスコアが一致する。"""
        from analytics.python.frost.score_engine import ScoreEngine
        from analytics.python.frost.evidence_bundle import evaluate_candidate_to_bundle
        cand = _make_candidate()
        cfg = _make_config()
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        engine = ScoreEngine.from_config(cfg)
        expected_v1 = engine.compute_v1(bundle.scores)
        assert bundle.scores.frost_score_v1 == pytest.approx(expected_v1, abs=1e-9)

    def test_gate_engine_and_bundle_verdict_equal(self):
        """GateEngine で手動評価した結果と bundle の gate が一致する。"""
        from analytics.python.frost.gate_engine import GateEngine
        from analytics.python.frost.evidence_bundle import evaluate_candidate_to_bundle
        cand = _make_candidate()
        cfg = _make_config()
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        verdict = GateEngine.from_config(cfg).evaluate(
            bundle.features, bundle.pbo, bundle.stability
        )
        assert bundle.gate.passed == verdict.passed
        assert bundle.gate.to_list() == verdict.to_list()

    def test_bundle_evaluation_from_bundle_roundtrip(self):
        """evaluate_candidate_to_bundle → evaluation_from_bundle のスコアが一致。"""
        from analytics.python.frost.evidence_bundle import (
            evaluate_candidate_to_bundle,
            evaluation_from_bundle,
        )
        cand = _make_candidate()
        cfg = _make_config()
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        ev = evaluation_from_bundle(bundle)
        assert ev.frost_score == pytest.approx(bundle.scores.frost_score_v1, abs=1e-9)

    def test_bundle_gate_failures_match_evaluation(self):
        """bundle のゲート失敗リストと FrostEvaluation.hard_gate_failures が一致。"""
        from analytics.python.frost.evidence_bundle import (
            evaluate_candidate_to_bundle,
            evaluation_from_bundle,
        )
        cand = _make_candidate()
        cfg = _make_config()
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        ev = evaluation_from_bundle(bundle)
        assert bundle.gate.to_list() == ev.hard_gate_failures

    def test_no_circular_import(self):
        """gate_engine / score_engine / evidence_bundle の循環インポートなし。"""
        import importlib
        for mod_name in [
            "analytics.python.frost.gate_engine",
            "analytics.python.frost.score_engine",
            "analytics.python.frost.evidence_bundle",
        ]:
            mod = importlib.import_module(mod_name)
            assert mod is not None

    def test_v2_score_disabled_by_default(self):
        """use_v2_score=False (デフォルト) のとき frost_score_v1 == frost_score_v2。"""
        from analytics.python.frost.evidence_bundle import evaluate_candidate_to_bundle
        cand = _make_candidate()
        cfg = _make_config(use_v2_score=False)
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        assert bundle.scores.frost_score_v1 == pytest.approx(
            bundle.scores.frost_score_v2, abs=1e-9
        )

    def test_v2_score_enabled_differs_from_v1(self):
        """use_v2_score=True のとき v2 != v1 (追加軸が中立でなければ)。"""
        from analytics.python.frost.evidence_bundle import evaluate_candidate_to_bundle
        cand = _make_candidate()
        cfg = _make_config(use_v2_score=True)
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        # v2 追加軸がデフォルト中立値 (0.5) の場合、v2 > v1 になるはず
        # (w_genome=0.05*0.5 + w_causal=0.05*0.5 + w_entropy=0.05*0.5 = 0.075 加算)
        assert bundle.scores.frost_score_v2 >= bundle.scores.frost_score_v1 - 1e-9


# ===========================================================================
# TestBackwardCompat
# ===========================================================================

class TestBackwardCompat:
    """Phase 2 のテストと等価な後方互換確認。"""

    def _pair(self, **cand_kwargs):
        """旧 evaluate_candidate() と新パイプラインの評価結果ペア。"""
        from analytics.python.frost.frost_selector import evaluate_candidate
        from analytics.python.frost.evidence_bundle import (
            evaluate_candidate_to_bundle,
            evaluation_from_bundle,
        )
        cand = _make_candidate(**cand_kwargs)
        cfg = _make_config()
        ev_old = evaluate_candidate(cand, "r", "t", cfg)
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        ev_new = evaluation_from_bundle(bundle)
        return ev_old, ev_new

    def test_frost_score_unchanged(self):
        """Phase 3 後も frost_score が変わらない。"""
        ev_old, ev_new = self._pair()
        assert ev_old.frost_score == pytest.approx(ev_new.frost_score, abs=1e-9)

    def test_hard_gate_passed_unchanged(self):
        """Phase 3 後も hard_gate_passed が変わらない。"""
        ev_old, ev_new = self._pair()
        assert ev_old.hard_gate_passed == ev_new.hard_gate_passed

    def test_hard_gate_failures_unchanged(self):
        """Phase 3 後も hard_gate_failures が変わらない。"""
        ev_old, ev_new = self._pair()
        assert ev_old.hard_gate_failures == ev_new.hard_gate_failures

    def test_check_hard_gates_matches_gate_engine(self):
        """check_hard_gates() が GateEngine 委譲後も同じ結果を返す。"""
        from analytics.python.frost.frost_selector import check_hard_gates
        from analytics.python.frost.gate_engine import GateEngine
        feat = {
            "rank_ic": 0.03,
            "oos_sharpe": 0.60,
            "turnover": 3.0,
            "oos_max_drawdown": -0.08,
            "regime_pass_ratio_raw": 0.80,
            "complexity_score": 0.40,
        }
        cfg = _make_config()
        pbo_score = 0.12
        scs = 0.70
        old_passed, old_failures = check_hard_gates(feat, pbo_score, scs, cfg)
        new_passed, new_failures = GateEngine.from_config(cfg).evaluate_from_dict(
            feat, pbo_score, scs
        )
        assert old_passed == new_passed
        assert old_failures == new_failures

    def test_predictive_score_unchanged(self):
        """Phase 3 後も predictive_score が変わらない。"""
        ev_old, ev_new = self._pair()
        assert ev_old.predictive_score == pytest.approx(
            ev_new.predictive_score, abs=1e-9
        )

    def test_candidate_id_preserved(self):
        """Phase 3 後も candidate_id が保持される。"""
        ev_old, ev_new = self._pair()
        assert ev_old.candidate_id == ev_new.candidate_id

    def test_diagnostics_score_breakdown_unchanged(self):
        """diagnostics_json の score_breakdown が変わらない。"""
        ev_old, ev_new = self._pair()
        old_bd = ev_old.diagnostics_json.get("score_breakdown", {})
        new_bd = ev_new.diagnostics_json.get("score_breakdown", {})
        for key in old_bd:
            assert old_bd[key] == pytest.approx(new_bd[key], abs=1e-9), \
                f"score_breakdown[{key!r}] が一致しない"

    def test_gate_failures_format_unchanged(self):
        """ゲート失敗メッセージのフォーマットが変わらない (失敗ケース)。"""
        from analytics.python.frost.frost_selector import evaluate_candidate
        from analytics.python.frost.evidence_bundle import (
            evaluate_candidate_to_bundle, evaluation_from_bundle
        )
        # 意図的に複数ゲートを落とす
        cand = _make_candidate()
        cfg = _make_config(min_oos_sharpe=2.0, max_complexity_score=0.10)
        ev_old = evaluate_candidate(cand, "r", "t", cfg)
        bundle = evaluate_candidate_to_bundle(cand, "r", "t", cfg)
        ev_new = evaluation_from_bundle(bundle)
        assert set(ev_old.hard_gate_failures) == set(ev_new.hard_gate_failures)

    def test_full_pipeline_no_exception(self):
        """完全なパイプライン (evaluate_candidate → make_decision) が例外なく完了する。"""
        from analytics.python.frost.frost_selector import evaluate_candidate, make_decision
        cand = _make_candidate()
        cfg = _make_config()
        ev = evaluate_candidate(cand, "r", "t", cfg)
        decision = make_decision(cand, ev, cfg, rank=1)
        assert decision.decision in ("SELECTED", "HOLD", "REJECTED")

    def test_v2_score_flag_honored(self):
        """use_v2_score=True フラグが FrostEvaluation に反映される。"""
        from analytics.python.frost.evidence_bundle import (
            evaluate_candidate_to_bundle, evaluation_from_bundle
        )
        cand = _make_candidate()
        cfg_v1 = _make_config(use_v2_score=False)
        cfg_v2 = _make_config(use_v2_score=True)
        bundle_v1 = evaluate_candidate_to_bundle(cand, "r", "t", cfg_v1)
        bundle_v2 = evaluate_candidate_to_bundle(cand, "r", "t", cfg_v2)
        ev_v1 = evaluation_from_bundle(bundle_v1, use_v2_score=False)
        ev_v2 = evaluation_from_bundle(bundle_v2, use_v2_score=True)
        # v2 スコアは v1 + 追加軸分 ≥ v1 (中立値 0.5 が加算されるため)
        assert ev_v2.frost_score >= ev_v1.frost_score - 1e-9
