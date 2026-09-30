"""
test_phase3_detect_kill.py
--------------------------
Phase 3 (G3): Detect→Kill ライフサイクル実装の単体テスト

## テスト対象モジュール

- analytics/python/frost/frost_cusum.py
    CusumParams / CusumStepResult / CusumRunResult / CusumDetector / detect_degradation_cusum

- analytics/python/frost/frost_lifecycle.py
    LifecycleStatus / LifecycleRecord / LifecycleCheckResult / KillQueue
    AlphaLifecycleEngine / check_alpha_degradation

## CUSUM 数理仕様 (Page 1954)

  下方 CUSUM: S_neg[t] = max(0, S_neg[t-1] - (v[t] - mu0 + k))
  上方 CUSUM: S_pos[t] = max(0, S_pos[t-1] + (v[t] - mu0) - k)
  下方中立点: v < mu0 - k のとき S_neg が増加する
  増分/ステップ: |v - (mu0 - k)| when v < mu0 - k

## テストシナリオの数値根拠 (k=0.5, h=5.0, mu0=0.0)

  中立点 = mu0 - k = -0.5
  IC=-0.8: 増分 = |-0.8 - (-0.5)| = 0.3/step → h=5.0 達成に ceil(5.0/0.3)=17step
           → first_degradation_index = 16 (0-indexed)
  IC=0.0:  増分なし (>= 中立点) → S_neg は増加しない
  IC=0.1:  増分なし (>= 中立点) → S_neg は増加しない
  IC=1.0:  上方 CUSUM 増分 = |1.0 - 0.5| = 0.5/step → h=5.0 達成に 10step
           → first_recovery_index = 9 (0-indexed)
"""
import math
import pytest

from analytics.python.frost.frost_cusum import (
    CusumDetector,
    CusumParams,
    CusumRunResult,
    CusumStepResult,
    detect_degradation_cusum,
)
from analytics.python.frost.frost_lifecycle import (
    AlphaLifecycleEngine,
    KillQueue,
    LifecycleCheckResult,
    LifecycleRecord,
    LifecycleStatus,
    check_alpha_degradation,
)

# ---------------------------------------------------------------------------
# ヘルパー定数
# ---------------------------------------------------------------------------

# k=0.5, h=5.0, mu0=0.0 のデフォルトパラメータでの動作根拠
# 中立点 = mu0 - k = -0.5
# IC=-0.8: 毎ステップ +0.3 蓄積 → 17ステップで h=5.0 到達 (index 16)
DEGRADED_IC = -0.8     # < 中立点(-0.5) → 劣化シグナル
NEUTRAL_IC = 0.0       # >= 中立点(-0.5) → 正常シグナル
POSITIVE_IC = 0.1      # > mu0(0.0) → 明確に正常
HIGH_IC = 1.0          # 上方 CUSUM トリガー用 (+0.5/step)


# ===========================================================================
# TestCusumParams — パラメータバリデーション (8 tests)
# ===========================================================================

@pytest.mark.phase3_detect_kill
class TestCusumParams:
    """CusumParams のデフォルト値・バリデーション・from_config をテストする。"""

    def test_default_values(self):
        p = CusumParams()
        assert p.k == 0.5
        assert p.h == 5.0
        assert p.mu0 == 0.0

    def test_custom_values(self):
        p = CusumParams(k=1.0, h=3.0, mu0=0.1)
        assert p.k == 1.0
        assert p.h == 3.0
        assert p.mu0 == 0.1

    def test_frozen_immutable(self):
        """frozen=True → フィールド代入は TypeError を送出する。"""
        p = CusumParams()
        with pytest.raises((AttributeError, TypeError)):
            p.k = 0.0  # type: ignore[misc]

    def test_negative_k_raises(self):
        with pytest.raises(ValueError, match="k"):
            CusumParams(k=-0.1)

    def test_zero_k_is_valid(self):
        """k=0 は許容（ドリフト無しの純粋シフト検知）。"""
        p = CusumParams(k=0.0)
        assert p.k == 0.0

    def test_zero_h_raises(self):
        with pytest.raises(ValueError, match="h"):
            CusumParams(h=0.0)

    def test_negative_h_raises(self):
        with pytest.raises(ValueError, match="h"):
            CusumParams(h=-1.0)

    def test_from_config_defaults(self):
        class Empty:
            pass
        p = CusumParams.from_config(Empty())
        assert p.k == 0.5
        assert p.h == 5.0
        assert p.mu0 == 0.0

    def test_from_config_custom(self):
        class Cfg:
            cusum_k = 0.25
            cusum_h = 4.0
            cusum_mu0 = 0.05
        p = CusumParams.from_config(Cfg())
        assert p.k == 0.25
        assert p.h == 4.0
        assert p.mu0 == 0.05


# ===========================================================================
# TestCusumStepResult — step 結果プロパティ (5 tests)
# ===========================================================================

@pytest.mark.phase3_detect_kill
class TestCusumStepResult:
    """CusumStepResult.triggered プロパティ等を確認する。"""

    def test_triggered_when_degraded(self):
        s = CusumStepResult(0, -0.8, cusum_neg=5.1, cusum_pos=0.0,
                            degraded=True, recovered=False)
        assert s.triggered is True

    def test_triggered_when_recovered(self):
        s = CusumStepResult(0, 1.0, cusum_neg=0.0, cusum_pos=5.1,
                            degraded=False, recovered=True)
        assert s.triggered is True

    def test_not_triggered_when_both_false(self):
        s = CusumStepResult(0, 0.0, cusum_neg=0.0, cusum_pos=0.0,
                            degraded=False, recovered=False)
        assert s.triggered is False

    def test_triggered_when_both_true(self):
        s = CusumStepResult(0, 0.0, cusum_neg=5.1, cusum_pos=5.1,
                            degraded=True, recovered=True)
        assert s.triggered is True

    def test_step_index_preserved(self):
        s = CusumStepResult(42, 0.5, 0.0, 0.0, False, False)
        assert s.step_index == 42


# ===========================================================================
# TestCusumRunResult — バッチ結果プロパティ (7 tests)
# ===========================================================================

@pytest.mark.phase3_detect_kill
class TestCusumRunResult:
    """CusumRunResult のプロパティと to_dict を確認する。"""

    def _make_result(self, degradation=False, recovery=False,
                     deg_idx=None, rec_idx=None, n=5):
        params = CusumParams()
        steps = [
            CusumStepResult(i, 0.0, 0.0, 0.0, False, False)
            for i in range(n)
        ]
        return CusumRunResult(
            params=params,
            steps=steps,
            degradation_detected=degradation,
            recovery_detected=recovery,
            first_degradation_index=deg_idx,
            first_recovery_index=rec_idx,
        )

    def test_triggered_degradation(self):
        r = self._make_result(degradation=True, deg_idx=2)
        assert r.triggered is True

    def test_triggered_recovery(self):
        r = self._make_result(recovery=True, rec_idx=3)
        assert r.triggered is True

    def test_not_triggered(self):
        r = self._make_result()
        assert r.triggered is False

    def test_n_steps(self):
        r = self._make_result(n=7)
        assert r.n_steps == 7

    def test_final_cusum_neg_empty(self):
        """steps が空のとき final_cusum_neg は 0.0 を返す。"""
        r = CusumRunResult(params=CusumParams(), steps=[])
        assert r.final_cusum_neg == 0.0

    def test_to_dict_keys(self):
        r = self._make_result(degradation=True, deg_idx=1)
        d = r.to_dict()
        expected_keys = {
            "detector", "k", "h", "mu0", "n_steps",
            "degradation_detected", "recovery_detected",
            "first_degradation_index", "first_recovery_index",
            "final_cusum_neg", "final_cusum_pos",
        }
        assert expected_keys == set(d.keys())

    def test_to_dict_values(self):
        params = CusumParams(k=0.5, h=5.0, mu0=0.0)
        steps = [CusumStepResult(0, 0.0, 2.5, 0.0, False, False)]
        r = CusumRunResult(params=params, steps=steps,
                           degradation_detected=False, recovery_detected=False)
        d = r.to_dict()
        assert d["detector"] == "cusum"
        assert d["k"] == 0.5
        assert d["n_steps"] == 1
        assert d["final_cusum_neg"] == pytest.approx(2.5)


# ===========================================================================
# TestCusumDetectorStep — ストリーミング (10 tests)
# ===========================================================================

@pytest.mark.phase3_detect_kill
class TestCusumDetectorStep:
    """CusumDetector.step() のストリーミング動作を確認する。"""

    def test_step_neutral_ic_no_increase(self):
        """IC=0.0 (>= 中立点 -0.5) → S_neg は増加しない。"""
        det = CusumDetector(CusumParams())
        s = det.step(0.0)
        assert s.cusum_neg == pytest.approx(0.0)
        assert s.degraded is False

    def test_step_positive_ic_no_increase(self):
        """IC=0.1 > 中立点 -0.5 → S_neg は増加しない。"""
        det = CusumDetector(CusumParams())
        s = det.step(POSITIVE_IC)
        assert s.cusum_neg == pytest.approx(0.0)

    def test_step_degraded_ic_increases_sneg(self):
        """IC=-0.8 < 中立点 -0.5 → S_neg が 0.3 増加する。"""
        det = CusumDetector(CusumParams())
        s = det.step(DEGRADED_IC)
        # S_neg = max(0, 0 - (-0.8 - 0.0 + 0.5)) = max(0, 0.3) = 0.3
        assert s.cusum_neg == pytest.approx(0.3)
        assert s.degraded is False

    def test_step_high_ic_increases_spos(self):
        """IC=1.0 > mu0+k=0.5 → S_pos が 0.5 増加する。"""
        det = CusumDetector(CusumParams())
        s = det.step(HIGH_IC)
        # S_pos = max(0, 0 + (1.0 - 0.0) - 0.5) = max(0, 0.5) = 0.5
        assert s.cusum_pos == pytest.approx(0.5)

    def test_step_degradation_trigger_at_17th(self):
        """IC=-0.8 を17回処理すると step=16 で degraded=True になる。"""
        det = CusumDetector(CusumParams())
        last = None
        for i in range(17):
            last = det.step(DEGRADED_IC)
        assert last.degraded is True
        assert last.step_index == 16

    def test_step_index_increments(self):
        det = CusumDetector(CusumParams())
        for i in range(5):
            s = det.step(POSITIVE_IC)
            assert s.step_index == i

    def test_reset_clears_state(self):
        det = CusumDetector(CusumParams())
        for _ in range(10):
            det.step(DEGRADED_IC)
        det.reset()
        s = det.step(DEGRADED_IC)
        assert s.step_index == 0
        assert s.cusum_neg == pytest.approx(0.3)

    def test_step_nan_treated_as_zero(self):
        """NaN は 0.0 として扱われる（S_neg は増加しない）。"""
        det = CusumDetector(CusumParams())
        s = det.step(float("nan"))
        assert s.cusum_neg == pytest.approx(0.0)

    def test_step_inf_treated_as_zero(self):
        """±Inf は 0.0 として扱われる（S_neg は増加しない）。"""
        det = CusumDetector(CusumParams())
        s_pos = det.step(float("inf"))
        assert math.isfinite(s_pos.cusum_neg)
        det.reset()
        s_neg = det.step(float("-inf"))
        assert math.isfinite(s_neg.cusum_neg)

    def test_step_recovery_trigger(self):
        """IC=1.0 を10回処理すると step=9 で recovered=True になる。"""
        det = CusumDetector(CusumParams())
        last = None
        for _ in range(10):
            last = det.step(HIGH_IC)
        assert last.recovered is True


# ===========================================================================
# TestCusumDetectorRun — バッチ処理 (10 tests)
# ===========================================================================

@pytest.mark.phase3_detect_kill
class TestCusumDetectorRun:
    """CusumDetector.run() のバッチ動作を確認する。"""

    def test_run_empty_list(self):
        """空系列 → degradation_detected=False, n_steps=0。"""
        det = CusumDetector(CusumParams())
        r = det.run([])
        assert r.degradation_detected is False
        assert r.n_steps == 0

    def test_run_healthy_ic_no_degradation(self):
        """正常 IC=0.0 を 20 点 → 劣化なし。"""
        det = CusumDetector(CusumParams())
        r = det.run([NEUTRAL_IC] * 20)
        assert r.degradation_detected is False
        assert r.first_degradation_index is None

    def test_run_degraded_ic_detected(self):
        """IC=-0.8 を 20 点 → 劣化検知。"""
        det = CusumDetector(CusumParams())
        r = det.run([DEGRADED_IC] * 20)
        assert r.degradation_detected is True
        assert r.first_degradation_index == 16

    def test_run_first_degradation_index_correct(self):
        """劣化開始インデックスが正確に返ること。"""
        det = CusumDetector(CusumParams())
        r = det.run([DEGRADED_IC] * 20)
        # 16 番目のステップ (0-indexed) で初めて degraded
        step_at_first = r.steps[r.first_degradation_index]
        assert step_at_first.degraded is True
        # 一つ前は degraded でない
        assert r.steps[r.first_degradation_index - 1].degraded is False

    def test_run_returns_all_steps(self):
        """steps リストの長さが入力と一致する。"""
        det = CusumDetector(CusumParams())
        r = det.run([0.0] * 15)
        assert len(r.steps) == 15

    def test_run_recovery_detected(self):
        """IC=1.0 を 10 点 → 上方 Detect (recovery_detected=True)。"""
        det = CusumDetector(CusumParams())
        r = det.run([HIGH_IC] * 10)
        assert r.recovery_detected is True
        assert r.first_recovery_index == 9

    def test_run_mixed_healthy_then_degraded(self):
        """正常10点 → 劣化20点 → first_degradation_index = 26 (10 + 16)。"""
        det = CusumDetector(CusumParams())
        mixed = [POSITIVE_IC] * 10 + [DEGRADED_IC] * 20
        r = det.run(mixed)
        assert r.degradation_detected is True
        assert r.first_degradation_index == 26

    def test_run_resets_state_on_each_call(self):
        """run() は毎回 reset() してから処理するため前回結果に依存しない。"""
        det = CusumDetector(CusumParams())
        # 1回目: 劣化系列
        r1 = det.run([DEGRADED_IC] * 20)
        assert r1.degradation_detected is True
        # 2回目: 正常系列のみ
        r2 = det.run([POSITIVE_IC] * 20)
        assert r2.degradation_detected is False

    def test_run_params_preserved_in_result(self):
        """CusumRunResult に渡したパラメータが保持される。"""
        p = CusumParams(k=0.3, h=3.0, mu0=0.0)
        det = CusumDetector(p)
        r = det.run([0.0] * 5)
        assert r.params.k == 0.3
        assert r.params.h == 3.0

    def test_run_from_config(self):
        """from_config() で生成した Detector が正常に動作する。"""
        class Cfg:
            cusum_k = 0.5
            cusum_h = 2.0
            cusum_mu0 = 0.0
        det = CusumDetector.from_config(Cfg())
        r = det.run([DEGRADED_IC] * 10)
        # h=2.0, 増分0.3 → ceil(2.0/0.3)=7 → index 6
        assert r.degradation_detected is True
        assert r.first_degradation_index == 6


# ===========================================================================
# TestDetectDegradationCusum — 関数型ラッパー (6 tests)
# ===========================================================================

@pytest.mark.phase3_detect_kill
class TestDetectDegradationCusum:
    """detect_degradation_cusum() 関数型ラッパーを確認する。"""

    def test_degraded_returns_true(self):
        assert detect_degradation_cusum([DEGRADED_IC] * 20) is True

    def test_healthy_returns_false(self):
        assert detect_degradation_cusum([POSITIVE_IC] * 20) is False

    def test_empty_returns_false(self):
        assert detect_degradation_cusum([]) is False

    def test_custom_params(self):
        """低い h=2.0 なら短い系列でも検知できる。"""
        assert detect_degradation_cusum([DEGRADED_IC] * 10, h=2.0) is True

    def test_neutral_ic_no_detection(self):
        """IC=0.0 (中立点丁度) では増加しないので不検知。"""
        assert detect_degradation_cusum([NEUTRAL_IC] * 50) is False

    def test_below_neutral_point_triggers_with_sufficient_data(self):
        """中立点 -0.5 より十分下の IC=-1.0 は10点で検知できる。"""
        # IC=-1.0: 増分 = -((-1.0) - 0.0 + 0.5) = 0.5/step → h=5.0 を 10ステップで達成
        assert detect_degradation_cusum([-1.0] * 10) is True


# ===========================================================================
# TestLifecycleStatus — 定数 (4 tests)
# ===========================================================================

@pytest.mark.phase3_detect_kill
class TestLifecycleStatus:
    """LifecycleStatus 定数の存在と値を確認する。"""

    def test_all_constants_defined(self):
        assert LifecycleStatus.ACTIVE == "active"
        assert LifecycleStatus.DEGRADED == "degraded"
        assert LifecycleStatus.UNDER_REVIEW == "under_review"
        assert LifecycleStatus.REVOKED == "revoked"
        assert LifecycleStatus.SUSPENDED == "suspended"

    def test_all_tuple_contains_five(self):
        assert len(LifecycleStatus.ALL) == 5

    def test_all_tuple_contains_all_constants(self):
        expected = {
            LifecycleStatus.ACTIVE, LifecycleStatus.DEGRADED,
            LifecycleStatus.UNDER_REVIEW, LifecycleStatus.REVOKED,
            LifecycleStatus.SUSPENDED,
        }
        assert set(LifecycleStatus.ALL) == expected

    def test_constants_are_strings(self):
        for status in LifecycleStatus.ALL:
            assert isinstance(status, str)


# ===========================================================================
# TestLifecycleRecord — 入力 DTO (4 tests)
# ===========================================================================

@pytest.mark.phase3_detect_kill
class TestLifecycleRecord:
    """LifecycleRecord の構築とデフォルト値を確認する。"""

    def test_default_status_is_active(self):
        r = LifecycleRecord("art_1")
        assert r.current_status == LifecycleStatus.ACTIVE

    def test_rolling_ic_default_empty(self):
        r = LifecycleRecord("art_1")
        assert r.rolling_ic == []

    def test_metadata_default_empty(self):
        r = LifecycleRecord("art_1")
        assert r.metadata == {}

    def test_custom_values(self):
        r = LifecycleRecord(
            artifact_id="art_X",
            current_status=LifecycleStatus.DEGRADED,
            rolling_ic=[0.1, -0.2],
            metadata={"source": "live"},
        )
        assert r.artifact_id == "art_X"
        assert r.current_status == LifecycleStatus.DEGRADED
        assert r.rolling_ic == [0.1, -0.2]
        assert r.metadata == {"source": "live"}


# ===========================================================================
# TestLifecycleCheckResult — 結果プロパティ (5 tests)
# ===========================================================================

@pytest.mark.phase3_detect_kill
class TestLifecycleCheckResult:
    """LifecycleCheckResult のプロパティと to_dict を確認する。"""

    def _make(self, current="active", new="degraded", degraded=True):
        return LifecycleCheckResult(
            artifact_id="art_1",
            current_status=current,
            new_status=new,
            degradation_detected=degraded,
            rolling_ic_len=20,
            review_required=degraded,
            reason="test",
        )

    def test_status_changed_true(self):
        r = self._make(current="active", new="degraded")
        assert r.status_changed is True

    def test_status_changed_false(self):
        r = self._make(current="active", new="active", degraded=False)
        assert r.status_changed is False

    def test_to_dict_keys(self):
        r = self._make()
        d = r.to_dict()
        expected = {
            "lifecycle", "artifact_id", "current_status", "new_status",
            "degradation_detected", "rolling_ic_len", "review_required",
            "reason", "cusum",
        }
        assert expected == set(d.keys())

    def test_to_dict_lifecycle_value(self):
        r = self._make()
        assert r.to_dict()["lifecycle"] == "detect_kill"

    def test_to_dict_cusum_empty_when_none(self):
        r = self._make()
        assert r.to_dict()["cusum"] == {}


# ===========================================================================
# TestKillQueue — キュー管理 (6 tests)
# ===========================================================================

@pytest.mark.phase3_detect_kill
class TestKillQueue:
    """KillQueue のプロパティと to_dict を確認する。"""

    def _make_check_result(self, artifact_id="art_1", review=True):
        return LifecycleCheckResult(
            artifact_id=artifact_id,
            current_status=LifecycleStatus.ACTIVE,
            new_status=LifecycleStatus.DEGRADED,
            degradation_detected=True,
            review_required=review,
            reason="test",
        )

    def test_empty_queue(self):
        q = KillQueue()
        assert q.count == 0
        assert q.artifact_ids == []

    def test_count(self):
        items = [self._make_check_result(f"art_{i}") for i in range(3)]
        q = KillQueue(pending_reviews=items)
        assert q.count == 3

    def test_artifact_ids(self):
        items = [self._make_check_result("art_A"), self._make_check_result("art_B")]
        q = KillQueue(pending_reviews=items)
        assert q.artifact_ids == ["art_A", "art_B"]

    def test_to_dict_keys(self):
        q = KillQueue()
        d = q.to_dict()
        assert set(d.keys()) == {"kill_queue", "pending_count", "pending_artifact_ids", "items"}

    def test_to_dict_pending_count(self):
        items = [self._make_check_result(f"art_{i}") for i in range(2)]
        q = KillQueue(pending_reviews=items)
        assert q.to_dict()["pending_count"] == 2

    def test_to_dict_items_serializable(self):
        items = [self._make_check_result("art_1")]
        q = KillQueue(pending_reviews=items)
        d = q.to_dict()
        assert isinstance(d["items"], list)
        assert d["items"][0]["artifact_id"] == "art_1"


# ===========================================================================
# TestAlphaLifecycleEngineCheck — check() メソッド (14 tests)
# ===========================================================================

@pytest.mark.phase3_detect_kill
class TestAlphaLifecycleEngineCheck:
    """AlphaLifecycleEngine.check() のステータス遷移ロジックを確認する。"""

    def _engine(self, k=0.5, h=5.0, min_ic_len=10):
        return AlphaLifecycleEngine(CusumParams(k=k, h=h), min_ic_len=min_ic_len)

    def test_degraded_ic_sets_new_status_degraded(self):
        engine = self._engine()
        record = LifecycleRecord("art_1", rolling_ic=[DEGRADED_IC] * 20)
        result = engine.check(record)
        assert result.new_status == LifecycleStatus.DEGRADED
        assert result.degradation_detected is True
        assert result.review_required is True

    def test_healthy_ic_maintains_active(self):
        engine = self._engine()
        record = LifecycleRecord("art_1", rolling_ic=[POSITIVE_IC] * 20)
        result = engine.check(record)
        assert result.new_status == LifecycleStatus.ACTIVE
        assert result.degradation_detected is False
        assert result.review_required is False

    def test_neutral_ic_no_degradation(self):
        """IC=0.0 は中立点丁度なので劣化検知されない。"""
        engine = self._engine()
        record = LifecycleRecord("art_1", rolling_ic=[NEUTRAL_IC] * 20)
        result = engine.check(record)
        assert result.degradation_detected is False

    def test_ic_insufficient_skips_cusum(self):
        """min_ic_len=10 未満 → CUSUM スキップ・状態維持。"""
        engine = self._engine(min_ic_len=10)
        record = LifecycleRecord("art_1", rolling_ic=[DEGRADED_IC] * 5)
        result = engine.check(record)
        assert result.degradation_detected is False
        assert result.new_status == LifecycleStatus.ACTIVE
        assert "不足" in result.reason

    def test_revoked_status_skipped(self):
        """REVOKED アルファは再検査されない。"""
        engine = self._engine()
        record = LifecycleRecord(
            "art_1",
            current_status=LifecycleStatus.REVOKED,
            rolling_ic=[DEGRADED_IC] * 20,
        )
        result = engine.check(record)
        assert result.degradation_detected is False
        assert result.new_status == LifecycleStatus.REVOKED
        assert result.review_required is False

    def test_under_review_status_skipped(self):
        """UNDER_REVIEW アルファは再検査されない。"""
        engine = self._engine()
        record = LifecycleRecord(
            "art_1",
            current_status=LifecycleStatus.UNDER_REVIEW,
            rolling_ic=[DEGRADED_IC] * 20,
        )
        result = engine.check(record)
        assert result.new_status == LifecycleStatus.UNDER_REVIEW
        assert result.review_required is False

    def test_degraded_then_healthy_returns_active(self):
        """以前 DEGRADED だったが CUSUM 未検知 → ACTIVE に復帰。"""
        engine = self._engine()
        record = LifecycleRecord(
            "art_1",
            current_status=LifecycleStatus.DEGRADED,
            rolling_ic=[POSITIVE_IC] * 20,
        )
        result = engine.check(record)
        assert result.new_status == LifecycleStatus.ACTIVE
        assert result.degradation_detected is False

    def test_active_healthy_status_unchanged(self):
        """正常 ACTIVE → 状態維持 (status_changed=False)。"""
        engine = self._engine()
        record = LifecycleRecord("art_1", rolling_ic=[POSITIVE_IC] * 20)
        result = engine.check(record)
        assert result.status_changed is False

    def test_active_degraded_status_changed(self):
        """ACTIVE → DEGRADED のとき status_changed=True。"""
        engine = self._engine()
        record = LifecycleRecord("art_1", rolling_ic=[DEGRADED_IC] * 20)
        result = engine.check(record)
        assert result.status_changed is True

    def test_check_result_artifact_id_preserved(self):
        engine = self._engine()
        record = LifecycleRecord("unique_art_xyz", rolling_ic=[POSITIVE_IC] * 20)
        result = engine.check(record)
        assert result.artifact_id == "unique_art_xyz"

    def test_check_result_rolling_ic_len(self):
        engine = self._engine()
        record = LifecycleRecord("art_1", rolling_ic=[POSITIVE_IC] * 15)
        result = engine.check(record)
        assert result.rolling_ic_len == 15

    def test_cusum_result_present_on_full_check(self):
        """CUSUM 実行時は cusum_result が None でない。"""
        engine = self._engine()
        record = LifecycleRecord("art_1", rolling_ic=[POSITIVE_IC] * 20)
        result = engine.check(record)
        assert result.cusum_result is not None

    def test_cusum_result_none_on_skip(self):
        """CUSUM スキップ時は cusum_result が None。"""
        engine = self._engine(min_ic_len=10)
        record = LifecycleRecord("art_1", rolling_ic=[POSITIVE_IC] * 3)
        result = engine.check(record)
        assert result.cusum_result is None

    def test_suspended_status_is_checked(self):
        """SUSPENDED アルファは REVOKED/UNDER_REVIEW と異なり CUSUM 実行対象。"""
        engine = self._engine()
        record = LifecycleRecord(
            "art_1",
            current_status=LifecycleStatus.SUSPENDED,
            rolling_ic=[DEGRADED_IC] * 20,
        )
        result = engine.check(record)
        # SUSPENDED は再検査対象 → 劣化検知される
        assert result.degradation_detected is True
        assert result.new_status == LifecycleStatus.DEGRADED


# ===========================================================================
# TestAlphaLifecycleEngineBatch — check_batch() / build_kill_queue() (8 tests)
# ===========================================================================

@pytest.mark.phase3_detect_kill
class TestAlphaLifecycleEngineBatch:
    """AlphaLifecycleEngine.check_batch() と build_kill_queue() を確認する。"""

    def _engine(self):
        return AlphaLifecycleEngine(CusumParams(k=0.5, h=5.0), min_ic_len=10)

    def test_check_batch_returns_same_length(self):
        engine = self._engine()
        records = [
            LifecycleRecord(f"art_{i}", rolling_ic=[POSITIVE_IC] * 20)
            for i in range(4)
        ]
        results = engine.check_batch(records)
        assert len(results) == 4

    def test_check_batch_empty_list(self):
        engine = self._engine()
        results = engine.check_batch([])
        assert results == []

    def test_check_batch_order_preserved(self):
        engine = self._engine()
        records = [
            LifecycleRecord(f"art_{i}", rolling_ic=[POSITIVE_IC] * 20)
            for i in range(3)
        ]
        results = engine.check_batch(records)
        assert [r.artifact_id for r in results] == ["art_0", "art_1", "art_2"]

    def test_build_kill_queue_count(self):
        """3 アルファのうち 2 つが劣化 → KillQueue.count == 2。"""
        engine = self._engine()
        records = [
            LifecycleRecord("art_a", rolling_ic=[DEGRADED_IC] * 20),
            LifecycleRecord("art_b", rolling_ic=[POSITIVE_IC] * 20),
            LifecycleRecord("art_c", rolling_ic=[DEGRADED_IC] * 20),
        ]
        q = engine.build_kill_queue(records)
        assert q.count == 2

    def test_build_kill_queue_artifact_ids(self):
        engine = self._engine()
        records = [
            LifecycleRecord("art_a", rolling_ic=[DEGRADED_IC] * 20),
            LifecycleRecord("art_b", rolling_ic=[POSITIVE_IC] * 20),
        ]
        q = engine.build_kill_queue(records)
        assert "art_a" in q.artifact_ids
        assert "art_b" not in q.artifact_ids

    def test_build_kill_queue_empty_when_all_healthy(self):
        engine = self._engine()
        records = [
            LifecycleRecord(f"art_{i}", rolling_ic=[POSITIVE_IC] * 20)
            for i in range(3)
        ]
        q = engine.build_kill_queue(records)
        assert q.count == 0

    def test_build_kill_queue_all_degraded(self):
        engine = self._engine()
        records = [
            LifecycleRecord(f"art_{i}", rolling_ic=[DEGRADED_IC] * 20)
            for i in range(4)
        ]
        q = engine.build_kill_queue(records)
        assert q.count == 4

    def test_build_kill_queue_to_dict_serializable(self):
        engine = self._engine()
        records = [LifecycleRecord("art_1", rolling_ic=[DEGRADED_IC] * 20)]
        q = engine.build_kill_queue(records)
        d = q.to_dict()
        assert d["pending_count"] == 1
        assert d["pending_artifact_ids"] == ["art_1"]


# ===========================================================================
# TestAlphaLifecycleEngineFromConfig — from_config() (5 tests)
# ===========================================================================

@pytest.mark.phase3_detect_kill
class TestAlphaLifecycleEngineFromConfig:
    """AlphaLifecycleEngine.from_config() の動作を確認する。"""

    def test_from_config_defaults(self):
        class Empty:
            pass
        engine = AlphaLifecycleEngine.from_config(Empty())
        assert engine.params.k == 0.5
        assert engine.params.h == 5.0
        assert engine.min_ic_len == 10

    def test_from_config_custom(self):
        class Cfg:
            cusum_k = 0.3
            cusum_h = 3.0
            cusum_mu0 = 0.0
            lifecycle_min_ic_len = 15
        engine = AlphaLifecycleEngine.from_config(Cfg())
        assert engine.params.k == 0.3
        assert engine.params.h == 3.0
        assert engine.min_ic_len == 15

    def test_from_config_partial(self):
        """一部属性のみ定義されている config → 未定義はデフォルト使用。"""
        class PartialCfg:
            cusum_h = 4.0
        engine = AlphaLifecycleEngine.from_config(PartialCfg())
        assert engine.params.k == 0.5   # デフォルト
        assert engine.params.h == 4.0   # 上書き

    def test_from_config_engine_works(self):
        class Cfg:
            cusum_k = 0.5
            cusum_h = 2.0
            cusum_mu0 = 0.0
            lifecycle_min_ic_len = 5
        engine = AlphaLifecycleEngine.from_config(Cfg())
        record = LifecycleRecord("art_1", rolling_ic=[DEGRADED_IC] * 10)
        result = engine.check(record)
        # h=2.0 → 7ステップで検知
        assert result.degradation_detected is True

    def test_from_policy_spec_attrs(self):
        """PolicySpec 風の属性名でも動作する。"""
        class MockPolicySpec:
            cusum_k = 0.5
            cusum_h = 5.0
            cusum_mu0 = 0.0
            lifecycle_min_ic_len = 10
        engine = AlphaLifecycleEngine.from_config(MockPolicySpec())
        assert engine.min_ic_len == 10


# ===========================================================================
# TestCheckAlphaDegradation — 関数型ラッパー (6 tests)
# ===========================================================================

@pytest.mark.phase3_detect_kill
class TestCheckAlphaDegradation:
    """check_alpha_degradation() 関数型ラッパーを確認する。"""

    def test_degraded_ic_detected(self):
        result = check_alpha_degradation("art_1", rolling_ic=[DEGRADED_IC] * 20)
        assert result.degradation_detected is True
        assert result.new_status == LifecycleStatus.DEGRADED

    def test_healthy_ic_not_detected(self):
        result = check_alpha_degradation("art_1", rolling_ic=[POSITIVE_IC] * 20)
        assert result.degradation_detected is False
        assert result.new_status == LifecycleStatus.ACTIVE

    def test_artifact_id_preserved(self):
        result = check_alpha_degradation("my_special_art", rolling_ic=[POSITIVE_IC] * 20)
        assert result.artifact_id == "my_special_art"

    def test_custom_params(self):
        """低い h=2.0 → 短い系列でも検知可能。"""
        params = CusumParams(k=0.5, h=2.0)
        result = check_alpha_degradation("art_1", rolling_ic=[DEGRADED_IC] * 10, params=params)
        assert result.degradation_detected is True

    def test_insufficient_ic_skipped(self):
        """min_ic_len=10 未満 → CUSUM スキップ。"""
        result = check_alpha_degradation("art_1", rolling_ic=[DEGRADED_IC] * 5, min_ic_len=10)
        assert result.degradation_detected is False

    def test_returns_lifecycle_check_result(self):
        result = check_alpha_degradation("art_1", rolling_ic=[POSITIVE_IC] * 20)
        assert isinstance(result, LifecycleCheckResult)


# ===========================================================================
# TestIntegration — 統合シナリオ (6 tests)
# ===========================================================================

@pytest.mark.phase3_detect_kill
class TestIntegration:
    """実際のユースケースに近い統合シナリオを確認する。"""

    def test_portfolio_of_10_alphas_3_degraded(self):
        """10 アルファのうち 3 つが劣化 → KillQueue.count == 3。"""
        engine = AlphaLifecycleEngine(CusumParams(k=0.5, h=5.0), min_ic_len=10)
        records = []
        for i in range(7):
            records.append(LifecycleRecord(f"healthy_{i}", rolling_ic=[POSITIVE_IC] * 20))
        for i in range(3):
            records.append(LifecycleRecord(f"degraded_{i}", rolling_ic=[DEGRADED_IC] * 20))
        q = engine.build_kill_queue(records)
        assert q.count == 3
        assert all(aid.startswith("degraded_") for aid in q.artifact_ids)

    def test_cusum_result_embedded_in_kill_queue(self):
        """KillQueue のアイテムに CUSUM 詳細が含まれること。"""
        engine = AlphaLifecycleEngine(CusumParams(k=0.5, h=5.0), min_ic_len=10)
        records = [LifecycleRecord("art_1", rolling_ic=[DEGRADED_IC] * 20)]
        q = engine.build_kill_queue(records)
        item_dict = q.to_dict()["items"][0]
        assert item_dict["cusum"]["degradation_detected"] is True
        assert item_dict["cusum"]["first_degradation_index"] == 16

    def test_to_dict_round_trip_serializable(self):
        """to_dict() の出力が JSON シリアライズ可能か確認する。"""
        import json
        engine = AlphaLifecycleEngine(CusumParams(k=0.5, h=5.0), min_ic_len=10)
        records = [
            LifecycleRecord("art_1", rolling_ic=[DEGRADED_IC] * 20),
            LifecycleRecord("art_2", rolling_ic=[POSITIVE_IC] * 20),
        ]
        q = engine.build_kill_queue(records)
        d = q.to_dict()
        json_str = json.dumps(d)  # 例外が出なければ OK
        restored = json.loads(json_str)
        assert restored["pending_count"] == 1

    def test_revoked_and_under_review_not_in_queue(self):
        """REVOKED / UNDER_REVIEW は劣化 IC でも KillQueue に積まれない。"""
        engine = AlphaLifecycleEngine(CusumParams(k=0.5, h=5.0), min_ic_len=10)
        records = [
            LifecycleRecord("art_r", current_status=LifecycleStatus.REVOKED,
                            rolling_ic=[DEGRADED_IC] * 20),
            LifecycleRecord("art_u", current_status=LifecycleStatus.UNDER_REVIEW,
                            rolling_ic=[DEGRADED_IC] * 20),
        ]
        q = engine.build_kill_queue(records)
        assert q.count == 0

    def test_lifecycle_check_result_to_dict_has_cusum_details(self):
        """CUSUM 実行時 to_dict()['cusum'] に詳細が含まれる。"""
        engine = AlphaLifecycleEngine(CusumParams(k=0.5, h=5.0), min_ic_len=10)
        record = LifecycleRecord("art_1", rolling_ic=[DEGRADED_IC] * 20)
        result = engine.check(record)
        d = result.to_dict()
        assert d["cusum"]["detector"] == "cusum"
        assert d["cusum"]["degradation_detected"] is True

    def test_gradual_degradation_detected(self):
        """緩やかな劣化 (IC = 0.0 から -0.8 に徐々に低下) も最終的に検知される。"""
        engine = AlphaLifecycleEngine(CusumParams(k=0.5, h=5.0), min_ic_len=10)
        # 最初は正常 → 徐々に劣化 → 最終的に DEGRADED_IC
        ic_series = [POSITIVE_IC] * 5 + [NEUTRAL_IC] * 5 + [DEGRADED_IC] * 20
        record = LifecycleRecord("art_1", rolling_ic=ic_series)
        result = engine.check(record)
        assert result.degradation_detected is True
        assert result.new_status == LifecycleStatus.DEGRADED
