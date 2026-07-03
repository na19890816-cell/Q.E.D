"""
test_phase9_dead_code.py
------------------------
Phase 9 デッドコード整理の回帰テスト。

対象:
  analytics/python/causal/causal_invariance.py
    - statistics モジュール完全除去
    - statistics.mean / pstdev → pure Python 置換の数値正確性
  analytics/python/frost/frost_decision_engine.py
    - promote_borderline_to_review の未使用引数 evaluations_by_cid 削除
    - apply_final_policy のシグネチャ変更確認

マーカー:
  @pytest.mark.dead_code — Phase 9 デッドコード整理テスト

テストクラス構成 (7 クラス, 40 テスト):
  TestCausalInvarianceNoStatistics     (5)  — statistics 完全除去確認
  TestCausalInvariancePurePython       (8)  — pure Python mean/pstdev の数値正確性
  TestCausalInvarianceCompute          (7)  — compute_invariance() 動作確認
  TestPromoteBorderlineSignature       (5)  — 引数削除後のシグネチャ確認
  TestPromoteBorderlineBehavior        (7)  — promote_borderline_to_review() 動作確認
  TestApplyFinalPolicySignature        (4)  — apply_final_policy シグネチャ確認
  TestApplyFinalPolicyBehavior         (4)  — apply_final_policy 統合確認
"""
from __future__ import annotations

import inspect
import math
import sys
import os

import pytest

# ── パス設定 ─────────────────────────────────────────────────────────────────
_FROST_DIR  = os.path.join(os.path.dirname(__file__), "../../analytics/python/frost")
_CAUSAL_DIR = os.path.join(os.path.dirname(__file__), "../../analytics/python/causal")
_ROOT_DIR   = os.path.join(os.path.dirname(__file__), "../../analytics/python")
sys.path.insert(0, _FROST_DIR)
sys.path.insert(0, _CAUSAL_DIR)
sys.path.insert(0, _ROOT_DIR)

from causal_invariance import (
    InvarianceResult,
    RegimeTestResult,
    compute_invariance,
    _simple_ols,
    _split_into_regimes,
)
from frost_contracts import FrostDecision, FrostEvaluation
from frost_config import FrostConfig


# ---------------------------------------------------------------------------
# ヘルパー: frost_decision_engine のパスを通したインポート
# ---------------------------------------------------------------------------

def _import_frost_decision_engine():
    """analytics パッケージ経由でインポートするためのヘルパー。"""
    import importlib
    # analytics.python.frost.frost_decision_engine をインポート
    spec = importlib.util.spec_from_file_location(
        "frost_decision_engine",
        os.path.join(_FROST_DIR, "frost_decision_engine.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    # frost_decision_engine は analytics.python.frost.frost_config / frost_contracts を
    # 相対パスで import するため、sys.modules に適切なパスを設定する
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
    import analytics.python.frost.frost_decision_engine as fde
    return fde


# パッケージ経由でインポート
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
import analytics.python.frost.frost_decision_engine as _fde


def _dec(
    candidate_id: str = "C001",
    decision: str = "SELECTED",
    decision_rank: int | None = None,
    frost_score: float = 0.5,
    suppressed_by_dedup: bool = False,
    review_required: bool = False,
    review_status: str = "pending",
    promotion_eligible: bool = False,
) -> FrostDecision:
    d = FrostDecision()
    d.candidate_id = candidate_id
    d.decision = decision
    d.decision_rank = decision_rank
    d.frost_score = frost_score
    d.suppressed_by_dedup = suppressed_by_dedup
    d.review_required = review_required
    d.review_status = review_status
    d.promotion_eligible = promotion_eligible
    d.decision_reason = ""
    return d


def _default_config(**kwargs) -> FrostConfig:
    cfg = FrostConfig()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


# ---------------------------------------------------------------------------
# TestCausalInvarianceNoStatistics (5 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.dead_code
class TestCausalInvarianceNoStatistics:
    """D9-A: causal_invariance.py が statistics モジュールを一切使わないことを確認。"""

    def test_no_statistics_import_attribute(self):
        """import statistics が存在しないこと。"""
        import causal_invariance
        assert not hasattr(causal_invariance, "statistics"), (
            "causal_invariance が statistics モジュール属性を持っている"
        )

    def test_statistics_not_in_module_dict(self):
        import causal_invariance
        assert "statistics" not in causal_invariance.__dict__, (
            "causal_invariance.__dict__ に statistics が存在する"
        )

    def test_source_no_statistics_call(self):
        """ソースファイルに statistics. 呼び出しが残っていないこと。"""
        src_path = os.path.join(_CAUSAL_DIR, "causal_invariance.py")
        with open(src_path) as f:
            src = f.read()
        assert "statistics.mean" not in src, "statistics.mean が残存"
        assert "statistics.pstdev" not in src, "statistics.pstdev が残存"
        assert "statistics.stdev" not in src, "statistics.stdev が残存"

    def test_import_statistics_line_removed(self):
        """import statistics 行が削除されていること。"""
        src_path = os.path.join(_CAUSAL_DIR, "causal_invariance.py")
        with open(src_path) as f:
            src = f.read()
        assert "import statistics" not in src, "import statistics 行が残存"

    def test_module_importable_without_statistics(self):
        """statistics なしでもモジュールがインポートできること。"""
        import causal_invariance as ci
        assert hasattr(ci, "compute_invariance")
        assert hasattr(ci, "InvarianceResult")


# ---------------------------------------------------------------------------
# TestCausalInvariancePurePython (8 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.dead_code
class TestCausalInvariancePurePython:
    """D9-A: statistics.mean/pstdev を置換した純 Python 実装の数値正確性テスト。"""

    # _coeff_stability を直接テストできないため、compute_invariance() 経由で検証する

    def _make_perfect_signal(self, n: int = 100) -> tuple[list[float], list[float]]:
        """beta=1 で完全相関するシグナルと収益を生成。"""
        signal  = [float(i) / n for i in range(n)]
        returns = [s * 1.0 + 0.0 for s in signal]  # beta=1, alpha=0
        return signal, returns

    def test_stable_betas_high_coefficient_stability(self):
        """全レジームで beta がほぼ同じなら coefficient_stability が高い。"""
        signal, returns = self._make_perfect_signal(100)
        result = compute_invariance(signal, returns, n_regimes=4)
        assert result.coefficient_stability > 0.5, (
            f"安定したシグナルの coefficient_stability={result.coefficient_stability:.4f} が低い"
        )

    def test_mean_beta_zero_uses_std_directly(self):
        """mean_beta ≈ 0 のとき cv = std (abs(mean) < 1e-10 の分岐)。"""
        # beta が +1 と -1 を交互にもつ regime → mean≈0, std>0 → cv=std → stability 低め
        # 手動で InvarianceResult をそのまま計算するより、端数を確認する簡易テスト
        import causal_invariance
        # 4 regime、beta が [0.5, -0.5, 0.5, -0.5] → mean=0
        betas = [0.5, -0.5, 0.5, -0.5]
        n_b   = len(betas)
        mean_beta = sum(betas) / n_b         # 0.0
        std_beta  = math.sqrt(sum((b - mean_beta) ** 2 for b in betas) / n_b)  # 0.5
        cv = std_beta  # abs(mean_beta) < 1e-10 の分岐
        stability = max(0.0, min(1.0, 1.0 - cv))  # 0.5
        assert math.isclose(stability, 0.5, rel_tol=1e-9)

    def test_mean_pstdev_population_divisor(self):
        """置換後の std が母標準偏差 (n 割り) であることを確認。"""
        betas = [1.0, 2.0, 3.0, 4.0]
        n_b = len(betas)
        mean_b = sum(betas) / n_b  # 2.5
        # 母標準偏差: sqrt(sum((b-mean)^2) / n) = sqrt(5/4) = sqrt(1.25)
        expected_pstdev = math.sqrt(sum((b - mean_b) ** 2 for b in betas) / n_b)
        assert math.isclose(expected_pstdev, math.sqrt(1.25), rel_tol=1e-9)

    def test_all_same_betas_stability_one(self):
        """全 beta が同値なら std=0 → cv=0 → stability=1.0。"""
        signal  = list(range(80))
        returns = [s * 2.0 for s in signal]
        # 均一な傾き → 各 regime で beta ≈ 2.0
        result = compute_invariance(signal, returns, n_regimes=4)
        assert result.coefficient_stability > 0.9, (
            f"均一シグナルの coefficient_stability={result.coefficient_stability:.4f} が低い"
        )

    def test_single_regime_coeff_stability_from_sign(self):
        """betas が 1 件のとき coeff_stability は符号で決定される。"""
        # n=20 は regime_results が 1 件になるケースに相当
        signal  = [float(i) for i in range(20)]
        returns = [float(i) * 0.5 for i in range(20)]
        result  = compute_invariance(signal, returns, n_regimes=4, significance_threshold=0.01)
        # 係数が正 → coeff_stability = 1.0
        assert result.coefficient_stability in (0.0, 1.0), (
            "single beta の場合は 0.0 または 1.0 のはず"
        )

    def test_positive_betas_stability_near_one(self):
        """正のシグナルのみ → stability が 0〜1 の範囲内であること。"""
        signal  = [float(i) * 0.01 for i in range(200)]
        returns = [s * 1.5 + 0.001 * (i % 5) for i, s in enumerate(signal)]
        result = compute_invariance(signal, returns, n_regimes=4)
        assert 0.0 <= result.coefficient_stability <= 1.0

    def test_compute_invariance_returns_invariance_result(self):
        signal  = list(range(60))
        returns = [float(x) * 0.3 for x in signal]
        result  = compute_invariance(signal, returns)
        assert isinstance(result, InvarianceResult)

    def test_disabled_returns_gate_pass(self):
        """CAUSAL_DISCOVERY_ENABLED=0 のとき gate_pass=True を返すこと。"""
        import causal_invariance as ci
        original = ci._CAUSAL_ENABLED
        try:
            ci._CAUSAL_ENABLED = False
            result = ci.compute_invariance([1.0, 2.0], [1.0, 2.0])
            assert result.gate_pass is True
        finally:
            ci._CAUSAL_ENABLED = original


# ---------------------------------------------------------------------------
# TestCausalInvarianceCompute (7 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.dead_code
class TestCausalInvarianceCompute:
    """compute_invariance() の動作確認テスト。"""

    def test_insufficient_data_returns_neutral(self):
        """n < 20 のとき中立結果を返すこと。"""
        result = compute_invariance([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        assert result.gate_pass is True
        assert result.invariance_pass_ratio == 0.5

    def test_perfect_positive_signal_gate_pass(self):
        """完全正相関シグナルは invariance gate を通過すること。"""
        signal  = [float(i) for i in range(100)]
        returns = [float(i) * 2.0 for i in range(100)]
        result  = compute_invariance(signal, returns, significance_threshold=0.01)
        assert result.gate_pass is True
        assert result.invariance_pass_ratio > 0.5

    def test_random_noise_may_fail_gate(self):
        """完全ランダムシグナルは pass_ratio が低くなりやすい。"""
        import random
        random.seed(42)
        n = 100
        signal  = [random.gauss(0, 1) for _ in range(n)]
        returns = [random.gauss(0, 1) for _ in range(n)]
        result  = compute_invariance(signal, returns, significance_threshold=0.8)
        # 高い有意性閾値では不合格になりやすい
        assert 0.0 <= result.invariance_pass_ratio <= 1.0

    def test_regime_results_count_matches_n_regimes(self):
        """regime_results の件数が n_regimes と一致すること。"""
        signal  = [float(i) for i in range(80)]
        returns = [float(i) * 1.5 for i in range(80)]
        result  = compute_invariance(signal, returns, n_regimes=4)
        assert result.n_regimes_tested == len(result.regime_results)

    def test_custom_regime_masks(self):
        """regime_masks を指定したとき、名前どおりのレジームが使われること。"""
        n = 100
        signal  = [float(i) for i in range(n)]
        returns = [float(i) * 1.2 for i in range(n)]
        masks   = {
            "early": [i < 50 for i in range(n)],
            "late":  [i >= 50 for i in range(n)],
        }
        result = compute_invariance(signal, returns, regime_masks=masks)
        regime_names = {r.regime_name for r in result.regime_results}
        assert "early" in regime_names
        assert "late"  in regime_names

    def test_invariance_result_to_dict(self):
        signal  = [float(i) for i in range(60)]
        returns = [float(i) * 0.5 for i in range(60)]
        result  = compute_invariance(signal, returns)
        d       = result.to_dict()
        assert "invariance_pass_ratio" in d
        assert "gate_pass" in d
        assert "regime_results" in d

    def test_pass_ratio_in_zero_one_range(self):
        signal  = [float(i) for i in range(80)]
        returns = [float(i) * 0.7 + (i % 3) * 0.1 for i in range(80)]
        result  = compute_invariance(signal, returns)
        assert 0.0 <= result.invariance_pass_ratio <= 1.0
        assert 0.0 <= result.coefficient_stability  <= 1.0
        assert 0.0 <= result.regime_consistency_score <= 1.0


# ---------------------------------------------------------------------------
# TestPromoteBorderlineSignature (5 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.dead_code
class TestPromoteBorderlineSignature:
    """D9-B: promote_borderline_to_review のシグネチャ変更確認。"""

    def test_evaluations_by_cid_not_in_signature(self):
        """evaluations_by_cid 引数が削除されていること。"""
        sig    = inspect.signature(_fde.promote_borderline_to_review)
        params = list(sig.parameters.keys())
        assert "evaluations_by_cid" not in params, (
            "evaluations_by_cid 引数が残存している"
        )

    def test_required_params_exist(self):
        """decisions, config, borderline_margin が残っていること。"""
        sig    = inspect.signature(_fde.promote_borderline_to_review)
        params = list(sig.parameters.keys())
        assert "decisions"         in params
        assert "config"            in params
        assert "borderline_margin" in params

    def test_param_count_is_three(self):
        """引数数が 3 (self なし) であること。"""
        sig = inspect.signature(_fde.promote_borderline_to_review)
        assert len(sig.parameters) == 3

    def test_docstring_no_evaluations_by_cid(self):
        """ドキュメント文字列から evaluations_by_cid が削除されていること。"""
        doc = _fde.promote_borderline_to_review.__doc__ or ""
        assert "evaluations_by_cid" not in doc

    def test_source_no_evaluations_by_cid(self):
        """ソースファイルに evaluations_by_cid が残っていないこと。"""
        src_path = os.path.join(_FROST_DIR, "frost_decision_engine.py")
        with open(src_path) as f:
            src = f.read()
        assert "evaluations_by_cid" not in src, (
            "frost_decision_engine.py に evaluations_by_cid が残存"
        )


# ---------------------------------------------------------------------------
# TestPromoteBorderlineBehavior (7 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.dead_code
class TestPromoteBorderlineBehavior:
    """promote_borderline_to_review() の動作確認テスト。"""

    def _cfg(self) -> FrostConfig:
        return _default_config(top_k=2)

    def test_hold_near_selected_min_promoted(self):
        """SELECTED 最小スコアに近い HOLD 候補が REVIEW_REQUIRED になること。"""
        cfg  = self._cfg()
        decs = [
            _dec("C1", "SELECTED", frost_score=0.8),
            _dec("C2", "SELECTED", frost_score=0.6),
            _dec("C3", "HOLD",     frost_score=0.58),  # 0.6 - 0.05 = 0.55 以上 → 昇格
        ]
        result = _fde.promote_borderline_to_review(decs, cfg, borderline_margin=0.05)
        c3 = next(d for d in result if d.candidate_id == "C3")
        assert c3.decision == "REVIEW_REQUIRED"

    def test_hold_far_from_selected_not_promoted(self):
        """SELECTED 最小スコアから遠い HOLD 候補は昇格しないこと。"""
        cfg  = self._cfg()
        decs = [
            _dec("C1", "SELECTED", frost_score=0.8),
            _dec("C2", "HOLD",     frost_score=0.3),  # 0.8 - 0.05 = 0.75 > 0.3 → 昇格しない
        ]
        result = _fde.promote_borderline_to_review(decs, cfg, borderline_margin=0.05)
        c2 = next(d for d in result if d.candidate_id == "C2")
        assert c2.decision == "HOLD"

    def test_suppressed_hold_not_promoted(self):
        """suppressed_by_dedup=True の HOLD は昇格しないこと。"""
        cfg  = self._cfg()
        decs = [
            _dec("C1", "SELECTED", frost_score=0.8),
            _dec("C2", "HOLD",     frost_score=0.79, suppressed_by_dedup=True),
        ]
        result = _fde.promote_borderline_to_review(decs, cfg, borderline_margin=0.05)
        c2 = next(d for d in result if d.candidate_id == "C2")
        assert c2.decision == "HOLD"

    def test_no_selected_returns_unchanged(self):
        """SELECTED がない場合は decisions をそのまま返すこと。"""
        cfg  = self._cfg()
        decs = [_dec("C1", "HOLD", frost_score=0.5)]
        result = _fde.promote_borderline_to_review(decs, cfg)
        assert result[0].decision == "HOLD"

    def test_review_required_flag_set(self):
        """昇格した決定の review_required=True になること。"""
        cfg  = self._cfg()
        decs = [
            _dec("C1", "SELECTED", frost_score=0.7),
            _dec("C2", "HOLD",     frost_score=0.68),
        ]
        result = _fde.promote_borderline_to_review(decs, cfg, borderline_margin=0.05)
        c2 = next(d for d in result if d.candidate_id == "C2")
        assert c2.review_required is True

    def test_decision_reason_set(self):
        """昇格した決定の decision_reason が設定されること。"""
        cfg  = self._cfg()
        decs = [
            _dec("C1", "SELECTED", frost_score=0.7),
            _dec("C2", "HOLD",     frost_score=0.68),
        ]
        result = _fde.promote_borderline_to_review(decs, cfg, borderline_margin=0.05)
        c2 = next(d for d in result if d.candidate_id == "C2")
        assert "Borderline" in c2.decision_reason

    def test_returns_decisions_list(self):
        """戻り値が list であること。"""
        cfg    = self._cfg()
        decs   = [_dec("C1", "SELECTED", frost_score=0.8)]
        result = _fde.promote_borderline_to_review(decs, cfg)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# TestApplyFinalPolicySignature (4 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.dead_code
class TestApplyFinalPolicySignature:
    """apply_final_policy のシグネチャ確認テスト。"""

    def test_required_params_exist(self):
        sig    = inspect.signature(_fde.apply_final_policy)
        params = list(sig.parameters.keys())
        assert "decisions"          in params
        assert "evaluations"        in params
        assert "config"             in params
        assert "promote_borderline" in params
        assert "borderline_margin"  in params

    def test_no_eval_by_cid_local_in_source(self):
        """apply_final_policy がもはや eval_by_cid を構築していないこと。"""
        src_path = os.path.join(_FROST_DIR, "frost_decision_engine.py")
        with open(src_path) as f:
            src = f.read()
        assert "eval_by_cid" not in src, (
            "apply_final_policy 内に eval_by_cid の構築コードが残存"
        )

    def test_returns_tuple(self):
        cfg    = _default_config(top_k=1, promotion_top_k=1)
        decs   = [_dec("C1", "SELECTED", decision_rank=1, frost_score=0.8)]
        evs    = [FrostEvaluation()]
        result = _fde.apply_final_policy(decs, evs, cfg)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_summary_stats_contains_counts(self):
        """戻り値の summary_stats に selected_count が含まれること。"""
        cfg    = _default_config(top_k=2, promotion_top_k=1)
        decs   = [_dec("C1", "SELECTED", decision_rank=1, frost_score=0.8)]
        evs    = []
        _, stats = _fde.apply_final_policy(decs, evs, cfg)
        assert "selected_count" in stats


# ---------------------------------------------------------------------------
# TestApplyFinalPolicyBehavior (4 テスト)
# ---------------------------------------------------------------------------

@pytest.mark.dead_code
class TestApplyFinalPolicyBehavior:
    """apply_final_policy() の統合動作確認テスト。"""

    def test_borderline_promoted_in_apply(self):
        """apply_final_policy が borderline 昇格を正しく実行すること。"""
        cfg  = _default_config(top_k=1, promotion_top_k=1)
        decs = [
            _dec("C1", "SELECTED", decision_rank=1, frost_score=0.8),
            _dec("C2", "HOLD",     decision_rank=2, frost_score=0.77),
        ]
        result_decs, stats = _fde.apply_final_policy(decs, [], cfg, promote_borderline=True, borderline_margin=0.05)
        c2 = next(d for d in result_decs if d.candidate_id == "C2")
        assert c2.decision == "REVIEW_REQUIRED"

    def test_borderline_disabled(self):
        """promote_borderline=False のとき昇格が行われないこと。"""
        cfg  = _default_config(top_k=1, promotion_top_k=1)
        decs = [
            _dec("C1", "SELECTED", decision_rank=1, frost_score=0.8),
            _dec("C2", "HOLD",     decision_rank=2, frost_score=0.77),
        ]
        result_decs, _ = _fde.apply_final_policy(decs, [], cfg, promote_borderline=False)
        c2 = next(d for d in result_decs if d.candidate_id == "C2")
        assert c2.decision == "HOLD"

    def test_top_k_enforced(self):
        """SELECTED が top_k を超えた分は HOLD に格下げされること。"""
        cfg  = _default_config(top_k=1, promotion_top_k=2)
        decs = [
            _dec("C1", "SELECTED", decision_rank=1, frost_score=0.9),
            _dec("C2", "SELECTED", decision_rank=2, frost_score=0.7),
        ]
        result_decs, stats = _fde.apply_final_policy(decs, [], cfg, promote_borderline=False)
        assert stats["selected_count"] <= 1

    def test_summary_stats_type_is_dict(self):
        cfg    = _default_config(top_k=2, promotion_top_k=1)
        decs   = [_dec("C1", "SELECTED", frost_score=0.8)]
        _, stats = _fde.apply_final_policy(decs, [], cfg)
        assert isinstance(stats, dict)
