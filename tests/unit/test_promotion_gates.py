"""
test_promotion_gates.py
-----------------------
昇格前ゲート統合 (G1 DSR + G2 相関) の DB レス単体テスト

対象:
  analytics/python/frost/promotion_gates.py
  analytics/python/pg_io/postgres_promotion_evidence.py
  analytics/python/alpha/promotion_bridge.py (gate_verdict 経路 / 書式バグ修正)
"""
from __future__ import annotations

import json
import random
from unittest.mock import MagicMock

import pytest

from analytics.python.alpha.eml.eml_search import EMLCandidate
from analytics.python.alpha.eml.eml_tree import build_leaf
from analytics.python.alpha.promotion_bridge import promote_alpha_candidate, promote_batch
from analytics.python.frost.frost_dsr import DsrGate, DsrParams
from analytics.python.frost.frost_lineage import TrialBatch, TrialLedger, make_family_key
from analytics.python.frost.policy_spec import PolicySpec, load_policy_spec
from analytics.python.frost.portfolio_correlation_gate import PortfolioCorrelationGate
from analytics.python.frost.promotion_gates import (
    MODE_ENFORCE,
    MODE_OFF,
    MODE_SHADOW,
    REASON_CORR,
    REASON_CORR_NO_EVIDENCE,
    REASON_DSR,
    REASON_DSR_NO_EVIDENCE,
    PromotionEvidence,
    PromotionGateEngine,
    normalize_mode,
)
from analytics.python.pg_io.postgres_promotion_evidence import (
    BASIS_KEY,
    SIGNAL_KEY,
    encode_signal,
    fetch_promoted_signals,
    make_signal_basis,
)

pytestmark = pytest.mark.promotion_gates

FAM = make_family_key("5d", "U", "T", "ts")


def _rets(mu=0.002, n=500, seed=1):
    rng = random.Random(seed)
    return [rng.gauss(mu, 0.01) for _ in range(n)]


STRONG = _rets(0.002, seed=1)    # DSR ≈ 1
NOISE = _rets(0.0, seed=2)       # DSR 不合格
OTHER = _rets(0.002, seed=99)    # STRONG と無相関


def _ev(cid, rets=STRONG, sig="same"):
    return PromotionEvidence(candidate_id=cid, oos_returns=rets,
                             signal=rets if sig == "same" else sig, returns_source="t")


# ===========================================================================
# モード
# ===========================================================================

class TestMode:
    @pytest.mark.parametrize("m", ["off", "shadow", "enforce", " Shadow ", "ENFORCE"])
    def test_valid(self, m):
        assert normalize_mode(m) in (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)

    def test_default_shadow(self):
        assert normalize_mode(None) == MODE_SHADOW
        assert PromotionGateEngine().mode == MODE_SHADOW

    def test_invalid_raises(self):
        """不正値を黙って off にしない"""
        with pytest.raises(ValueError):
            normalize_mode("enforced")
        with pytest.raises(ValueError):
            PromotionGateEngine(mode="on")

    def test_off_returns_none(self):
        assert PromotionGateEngine(mode=MODE_OFF).evaluate(_ev("a")) is None
        assert PromotionGateEngine(mode=MODE_OFF).evaluate_batch([_ev("a")]) == {}


# ===========================================================================
# 単体判定
# ===========================================================================

class TestEvaluate:
    def test_strong_passes(self):
        v = PromotionGateEngine().evaluate(_ev("a"), promoted_signals={})
        assert v.passed and v.reason_codes == []
        assert v.dsr["passed"] and v.portfolio_corr["passed"]

    def test_noise_fails_dsr(self):
        v = PromotionGateEngine().evaluate(_ev("a", NOISE))
        assert not v.passed and REASON_DSR in v.reason_codes and v.review_required

    def test_no_returns(self):
        v = PromotionGateEngine().evaluate(PromotionEvidence("a"))
        assert not v.passed and v.reason_codes == [REASON_DSR_NO_EVIDENCE]
        assert v.dsr is None

    def test_too_few_returns(self):
        v = PromotionGateEngine().evaluate(_ev("a", [0.01, 0.02]))
        assert REASON_DSR_NO_EVIDENCE in v.reason_codes

    def test_corr_exceeded(self):
        v = PromotionGateEngine().evaluate(_ev("a"), promoted_signals={"art1": STRONG})
        assert REASON_CORR in v.reason_codes
        assert v.portfolio_corr["max_correlation"] == pytest.approx(1.0)

    def test_uncorrelated_passes(self):
        v = PromotionGateEngine().evaluate(_ev("a"), promoted_signals={"art1": OTHER})
        assert v.passed

    def test_no_signal_with_portfolio(self):
        v = PromotionGateEngine().evaluate(_ev("a", sig=None), promoted_signals={"x": OTHER})
        assert REASON_CORR_NO_EVIDENCE in v.reason_codes

    def test_no_signal_without_portfolio_ok(self):
        v = PromotionGateEngine().evaluate(_ev("a", sig=None), promoted_signals={})
        assert REASON_CORR_NO_EVIDENCE not in v.reason_codes

    def test_both_fail_order(self):
        v = PromotionGateEngine().evaluate(_ev("a", NOISE), promoted_signals={"x": NOISE})
        assert v.reason_codes == [REASON_DSR, REASON_CORR]
        assert v.primary_reason == REASON_DSR

    def test_snapshot_feeds_n(self):
        snap = TrialLedger(batches=[TrialBatch.create(FAM, "r", "exhaustive", 10000)]).snapshot(FAM)
        rets = _rets(0.0015, seed=3)
        assert PromotionGateEngine().evaluate(_ev("a", rets)).passed is True
        v = PromotionGateEngine().evaluate(_ev("a", rets), snapshot=snap)
        assert not v.passed and v.dsr["n_trials"] == 10000
        assert v.ledger["snapshot_hash"] == snap.snapshot_hash

    def test_assumed_n_requires_review_even_if_passed(self):
        v = PromotionGateEngine().evaluate(_ev("a"))
        assert v.passed and v.review_required  # 台帳なし → N 仮定

    def test_provided_n_no_review_when_passed(self):
        snap = TrialLedger(batches=[TrialBatch.create(FAM, "r", "exhaustive", 3)]).snapshot(FAM)
        v = PromotionGateEngine().evaluate(_ev("a"), snapshot=snap)
        assert v.passed and not v.review_required

    def test_blocks_only_in_enforce(self):
        sh = PromotionGateEngine(mode=MODE_SHADOW).evaluate(_ev("a", NOISE))
        en = PromotionGateEngine(mode=MODE_ENFORCE).evaluate(_ev("a", NOISE))
        assert not sh.passed and not sh.blocks_promotion
        assert not en.passed and en.blocks_promotion

    def test_passed_never_blocks(self):
        assert not PromotionGateEngine(mode=MODE_ENFORCE).evaluate(_ev("a")).blocks_promotion

    def test_to_dict_json(self):
        d = PromotionGateEngine().evaluate(_ev("a", NOISE), promoted_signals={"x": NOISE}).to_dict()
        json.dumps(d)
        assert d["mode"] == "shadow" and d["blocks_promotion"] is False

    def test_thresholds_from_policy(self):
        e = PromotionGateEngine.from_config(PolicySpec(min_dsr=0.5, max_portfolio_corr=0.99))
        assert e.dsr_gate.threshold == 0.5 and e.corr_gate.threshold == 0.99

    def test_mode_from_policy(self):
        assert PromotionGateEngine.from_config(PolicySpec(promotion_gate_mode="enforce")).mode == MODE_ENFORCE
        assert PromotionGateEngine.from_config(PolicySpec(), mode="off").mode == MODE_OFF


# ===========================================================================
# バッチ (G2 逐次)
# ===========================================================================

class TestBatch:
    def test_intra_batch_duplicate_caught(self):
        """同一バッチ内で同じシグナルの 2 本目は相関ゲートで落ちる"""
        out = PromotionGateEngine().evaluate_batch([_ev("a"), _ev("b")])
        assert out["a"].passed
        assert REASON_CORR in out["b"].reason_codes

    def test_enforce_rejected_not_added(self):
        """enforce で落ちた候補は採用済み集合に加わらない"""
        e = PromotionGateEngine(mode=MODE_ENFORCE,
                                dsr_gate=DsrGate(DsrParams(min_dsr=0.999999)))
        out = e.evaluate_batch([_ev("a", NOISE), _ev("b", NOISE)])
        assert out["a"].blocks_promotion
        assert REASON_CORR not in out["b"].reason_codes

    def test_shadow_all_added(self):
        """shadow では全候補が昇格するので、全て採用済みとして扱う"""
        out = PromotionGateEngine(mode=MODE_SHADOW).evaluate_batch([_ev("a", NOISE), _ev("b", NOISE)])
        assert REASON_CORR in out["b"].reason_codes

    def test_existing_portfolio_used(self):
        out = PromotionGateEngine().evaluate_batch([_ev("a")], promoted_signals={"old": STRONG})
        assert REASON_CORR in out["a"].reason_codes

    def test_order_preserved(self):
        out = PromotionGateEngine().evaluate_batch([_ev("b"), _ev("a")])
        assert list(out) == ["b", "a"] and out["b"].passed

    def test_does_not_mutate_input(self):
        port = {"old": OTHER}
        PromotionGateEngine().evaluate_batch([_ev("a")], promoted_signals=port)
        assert list(port) == ["old"]


# ===========================================================================
# PolicySpec
# ===========================================================================

class TestPolicy:
    def test_default(self):
        assert PolicySpec().promotion_gate_mode == "shadow"

    def test_hash_changes(self):
        assert PolicySpec(promotion_gate_mode="enforce").policy_hash != PolicySpec().policy_hash

    def test_roundtrip(self):
        s = PolicySpec(promotion_gate_mode="enforce")
        assert PolicySpec.from_dict(s.to_dict()).promotion_gate_mode == "enforce"

    def test_validate(self):
        with pytest.raises(ValueError):
            PolicySpec(promotion_gate_mode="bogus").validate()

    def test_env(self, monkeypatch):
        monkeypatch.setenv("FROST_PROMOTION_GATE_MODE", "enforce")
        assert load_policy_spec().promotion_gate_mode == "enforce"


# ===========================================================================
# postgres_promotion_evidence (MagicMock)
# ===========================================================================

def _conn(rows, count=0):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = (count,)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=cur)
    ctx.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = ctx
    return conn, cur


class TestFetchPromotedSignals:
    def test_basic(self):
        conn, cur = _conn([("a1", [0.1, 0.2, 0.3], FAM), ("a2", json.dumps([1, 2, 3]), FAM)], count=4)
        r = fetch_promoted_signals(conn, family_key=FAM)
        assert r.signals == {"a1": [0.1, 0.2, 0.3], "a2": [1.0, 2.0, 3.0]}
        assert r.skipped_without_signal == 4
        sql = cur.execute.call_args_list[0][0][0]
        assert "status <> 'deprecated'" in sql

    def test_incomparable_family_excluded(self):
        conn, _ = _conn([("a1", [1, 2, 3], FAM), ("a2", [1, 2, 3], "fam:other"), ("a3", [1, 2, 3], None)])
        r = fetch_promoted_signals(conn, family_key=FAM)
        assert list(r.signals) == ["a1"] and r.skipped_incomparable == 2

    def test_family_none_returns_all(self):
        conn, _ = _conn([("a1", [1, 2, 3], FAM), ("a2", [1, 2, 3], None)])
        assert len(fetch_promoted_signals(conn, family_key=None).signals) == 2

    def test_invalid_skipped(self):
        conn, _ = _conn([("a1", [1, 2], FAM), ("a2", {"x": 1}, FAM), ("a3", ["x", 1, 2], FAM),
                         ("a4", "not json", FAM)])
        r = fetch_promoted_signals(conn, family_key=FAM)
        assert r.signals == {} and r.skipped_invalid == 4

    def test_nan_to_zero(self):
        conn, _ = _conn([("a1", [1.0, float("nan"), 3.0], FAM)])
        assert fetch_promoted_signals(conn, family_key=FAM).signals["a1"] == [1.0, 0.0, 3.0]

    def test_exclude_and_bytes(self):
        conn, _ = _conn([(b"a1", [1, 2, 3], FAM.encode()), ("a2", [1, 2, 3], FAM)])
        r = fetch_promoted_signals(conn, family_key=FAM, exclude_artifact_ids=["a2"])
        assert list(r.signals) == ["a1"]

    def test_to_dict(self):
        conn, _ = _conn([])
        d = fetch_promoted_signals(conn, family_key=FAM).to_dict()
        assert set(d) >= {"family_key", "promoted_with_signal", "skipped_incomparable"}

    def test_encode_and_basis(self):
        assert encode_signal([0.123456789123, float("inf"), "x"]) == [0.12345679, 0.0, 0.0]
        assert encode_signal(None) is None
        assert make_signal_basis(FAM, 3) == {"kind": "walk_forward_oos_net_returns",
                                             "family_key": FAM, "n": 3}


# ===========================================================================
# promotion_bridge (MagicMock)
# ===========================================================================

class _Ev:
    candidate_id = "c1"
    rank_ic = 0.1
    sharpe = 1.2
    max_drawdown = -0.05


def _cand():
    return EMLCandidate("c1", "run", "trace", build_leaf("r1"), "r1", 0.2)


def _bridge_conn():
    cur = MagicMock()
    cur.fetchone.return_value = ("audit-1",)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=cur)
    ctx.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = ctx
    return conn, cur


def _sqls(cur):
    return [c[0][0] for c in cur.execute.call_args_list]


class TestPromotionBridge:
    def test_format_bug_fixed(self):
        """旧実装は書式指定エラーで APPLIED に到達できなかった"""
        conn, _ = _bridge_conn()
        r = promote_alpha_candidate(conn, _cand(), _Ev(), dry_run=False)
        assert r["decision"] == "APPLIED", r.get("rejection_reason")

    def test_format_bug_fixed_without_eval(self):
        conn, _ = _bridge_conn()
        assert promote_alpha_candidate(conn, _cand(), None, dry_run=False)["decision"] == "APPLIED"

    def test_no_verdict_backward_compatible(self):
        conn, cur = _bridge_conn()
        r = promote_alpha_candidate(conn, _cand(), _Ev(), dry_run=True)
        assert r["decision"] == "DRY_RUN" and r["gate_verdict"] is None

    def test_shadow_fail_still_applied_and_audited(self):
        conn, cur = _bridge_conn()
        v = PromotionGateEngine(mode=MODE_SHADOW).evaluate(_ev("c1", NOISE))
        r = promote_alpha_candidate(conn, _cand(), _Ev(), gate_verdict=v,
                                    promotion_signal=NOISE, signal_family_key=FAM)
        assert r["decision"] == "APPLIED"
        ka = [c for c in cur.execute.call_args_list if "INSERT INTO knowledge_artifacts" in c[0][0]][0]
        meta = json.loads(ka[0][1][8])
        assert len(meta[SIGNAL_KEY]) == len(NOISE)
        assert meta[BASIS_KEY]["family_key"] == FAM
        assert meta["promotion_gates"]["passed"] is False
        au = [c for c in cur.execute.call_args_list if "INSERT INTO audit_events" in c[0][0]][0]
        assert json.loads(au[0][1][9])["promotion_gates"]["mode"] == "shadow"

    def test_enforce_fail_rejected(self):
        conn, cur = _bridge_conn()
        v = PromotionGateEngine(mode=MODE_ENFORCE).evaluate(_ev("c1", NOISE))
        r = promote_alpha_candidate(conn, _cand(), _Ev(), gate_verdict=v)
        assert r["decision"] == "REJECTED" and r["artifact_id"] is None
        assert REASON_DSR in r["rejection_reason"]
        sqls = _sqls(cur)
        assert not any("INSERT INTO knowledge_artifacts" in q for q in sqls)
        au = [c for c in cur.execute.call_args_list if "INSERT INTO audit_events" in c[0][0]][0]
        params = au[0][1]
        assert params[6] == "REJECTED" and params[7] == "PROMOTION_GATE_FAILED"
        assert params[8] == REASON_DSR

    def test_enforce_fail_dry_run_still_audited(self):
        conn, cur = _bridge_conn()
        v = PromotionGateEngine(mode=MODE_ENFORCE).evaluate(_ev("c1", NOISE))
        r = promote_alpha_candidate(conn, _cand(), _Ev(), dry_run=True, gate_verdict=v)
        assert r["decision"] == "REJECTED"
        sqls = _sqls(cur)
        assert any("INSERT INTO audit_events" in q for q in sqls)
        assert not any("eml_alpha_promotion_bridge" in q for q in sqls)

    def test_enforce_pass_applied(self):
        conn, _ = _bridge_conn()
        v = PromotionGateEngine(mode=MODE_ENFORCE).evaluate(_ev("c1"), promoted_signals={})
        assert promote_alpha_candidate(conn, _cand(), _Ev(), gate_verdict=v)["decision"] == "APPLIED"

    def test_batch_routes_by_candidate(self):
        conn, _ = _bridge_conn()
        v = PromotionGateEngine(mode=MODE_ENFORCE).evaluate(_ev("c1", NOISE))
        res = promote_batch(conn, [_cand()], [_Ev()], gate_verdicts={"c1": v})
        assert res[0]["decision"] == "REJECTED"

    def test_batch_without_gates_unchanged(self):
        conn, _ = _bridge_conn()
        res = promote_batch(conn, [_cand()], [_Ev()], dry_run=True)
        assert res[0]["decision"] == "DRY_RUN"
