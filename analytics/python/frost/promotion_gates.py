"""
promotion_gates.py
------------------
昇格前ゲートの統合判定 — G1 (Deflated Sharpe Ratio) + G2 (採用済みポートフォリオ相関)

## 役割

昇格 Bridge (analytics/python/alpha/promotion_bridge.py) が候補を Q.E.D. チェーンへ
登録する直前に、憲法ゲート 2 本をまとめて判定する純ロジック層。DB アクセスは持たない
(証拠の収集は呼び出し側 / pg_io が行う)。

    DSR (G1)   : OOS 日次リターン + 試行台帳スナップショット (ADR-002) → DSR >= min_dsr
    相関 (G2)  : 候補シグナル vs 採用済みアルファのシグナル → |r| < max_portfolio_corr

## モード

    off     : 評価しない (従来挙動)
    shadow  : 評価して結果を記録するが、昇格は止めない (既定。P8 ablation 前の観測期間)
    enforce : ゲート不合格の候補を REJECTED にする

shadow を既定にする理由: ゲート追加は機能追加であり、閾値の妥当性は P8 / gate-0 で
検証するまで未知。まず観測値を audit に蓄積し、enforce への切替は人間が判断する。

## バッチ内の逐次性 (G2)

同一バッチ内で先に昇格した候補は、後続候補にとって「採用済み」である。
evaluate_batch() は候補を与えられた順に評価し、昇格する候補のシグナルを
採用済み集合へ逐次追加する (enforce では合格した候補のみ、shadow では全候補)。

## 証拠不足の扱い

- OOS リターンが無い候補: DSR 判定不能 → 不合格 (INSUFFICIENT_EVIDENCE)。
  「検証を通っていないものは採用しない」原則に従う
- シグナルが無い候補: 相関判定不能 → 不合格 (INSUFFICIENT_EVIDENCE)
- 採用済みアルファが 0 本: G2 は合格 (比較対象なし)
- 台帳が空: DSR は N を仮定し review_required=True (frost_dsr の既存挙動)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from analytics.python.frost.frost_dsr import DsrGate, DsrGateResult
from analytics.python.frost.frost_lineage import TrialSnapshot
from analytics.python.frost.portfolio_correlation_gate import (
    PortfolioCorrelationGate,
    PortfolioGateResult,
)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)

REASON_PASSED = "PROMOTION_GATES_PASSED"
REASON_DSR = "DSR_BELOW_THRESHOLD"
REASON_DSR_NO_EVIDENCE = "DSR_INSUFFICIENT_EVIDENCE"
REASON_CORR = "PORTFOLIO_CORR_EXCEEDED"
REASON_CORR_NO_EVIDENCE = "PORTFOLIO_CORR_INSUFFICIENT_EVIDENCE"

#: 相関計算に必要な最小シグナル長 (portfolio_correlation_gate._MIN_SIGNAL_LEN と揃える)
MIN_SIGNAL_LEN = 3


def normalize_mode(mode: Optional[str]) -> str:
    """モード文字列を正規化する。不正値は ValueError (黙って off にしない)。"""
    m = (mode or MODE_SHADOW).strip().lower()
    if m not in VALID_MODES:
        raise ValueError(f"promotion gate mode は {VALID_MODES} のいずれか: {mode!r}")
    return m


# ---------------------------------------------------------------------------
# 入力 / 出力
# ---------------------------------------------------------------------------

@dataclass
class PromotionEvidence:
    """1 候補分の昇格判定材料。"""
    candidate_id: str
    oos_returns: Optional[Sequence[float]] = None   # 非年率 (日次) OOS ネットリターン
    signal: Optional[Sequence[float]] = None        # 相関判定用シグナル
    returns_source: str = ""                         # 例: "walk_forward_oos"


@dataclass
class PromotionGateVerdict:
    """1 候補分の統合判定。"""
    candidate_id: str
    mode: str
    passed: bool
    reason_codes: List[str]
    review_required: bool
    dsr: Optional[Dict[str, Any]] = None
    portfolio_corr: Optional[Dict[str, Any]] = None
    ledger: Optional[Dict[str, Any]] = None
    notes: List[str] = field(default_factory=list)

    @property
    def blocks_promotion(self) -> bool:
        """enforce モードで不合格のときのみ昇格を止める。"""
        return self.mode == MODE_ENFORCE and not self.passed

    @property
    def primary_reason(self) -> str:
        return self.reason_codes[0] if self.reason_codes else REASON_PASSED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "mode": self.mode,
            "passed": self.passed,
            "blocks_promotion": self.blocks_promotion,
            "reason_codes": list(self.reason_codes),
            "review_required": self.review_required,
            "dsr": self.dsr,
            "portfolio_corr": self.portfolio_corr,
            "ledger": self.ledger,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# エンジン
# ---------------------------------------------------------------------------

@dataclass
class PromotionGateEngine:
    """G1 + G2 を束ねる昇格前ゲート。"""
    dsr_gate: DsrGate = field(default_factory=DsrGate)
    corr_gate: PortfolioCorrelationGate = field(default_factory=PortfolioCorrelationGate)
    mode: str = MODE_SHADOW

    def __post_init__(self) -> None:
        self.mode = normalize_mode(self.mode)

    @classmethod
    def from_config(cls, config: Any, mode: Optional[str] = None) -> "PromotionGateEngine":
        return cls(
            dsr_gate=DsrGate.from_config(config),
            corr_gate=PortfolioCorrelationGate.from_config(config),
            mode=mode if mode is not None else getattr(config, "promotion_gate_mode", MODE_SHADOW),
        )

    # -- 単体 -------------------------------------------------------------
    def evaluate(
        self,
        evidence: PromotionEvidence,
        snapshot: Optional[TrialSnapshot] = None,
        promoted_signals: Optional[Dict[str, Sequence[float]]] = None,
    ) -> Optional[PromotionGateVerdict]:
        """1 候補を判定する。mode=off なら None。"""
        if self.mode == MODE_OFF:
            return None

        reasons: List[str] = []
        notes: List[str] = []
        review = False

        # ---- G1: DSR ----------------------------------------------------
        dsr_dict: Optional[Dict[str, Any]] = None
        rets = list(evidence.oos_returns) if evidence.oos_returns is not None else []
        if not rets:
            reasons.append(REASON_DSR_NO_EVIDENCE)
            notes.append("OOS リターンなし: DSR を算出できない")
            review = True
        else:
            kw = snapshot.to_dsr_kwargs() if snapshot is not None else {}
            res: DsrGateResult = self.dsr_gate.check(rets, **kw)
            dsr_dict = res.to_dict()
            dsr_dict["returns_source"] = evidence.returns_source
            review = review or res.review_required
            if not res.passed:
                reasons.append(
                    REASON_DSR_NO_EVIDENCE
                    if (res.failure_reason or "").startswith("INSUFFICIENT_OBS")
                    else REASON_DSR
                )

        # ---- G2: ポートフォリオ相関 --------------------------------------
        corr_dict: Optional[Dict[str, Any]] = None
        sig = list(evidence.signal) if evidence.signal is not None else []
        promoted = {k: list(v) for k, v in (promoted_signals or {}).items()}
        if promoted and len(sig) < MIN_SIGNAL_LEN:
            reasons.append(REASON_CORR_NO_EVIDENCE)
            notes.append("シグナルなし: 採用済みアルファとの相関を算出できない")
            review = True
        else:
            pres: PortfolioGateResult = self.corr_gate.check(sig, promoted)
            corr_dict = pres.to_dict()
            if not pres.passed:
                reasons.append(REASON_CORR)

        return PromotionGateVerdict(
            candidate_id=evidence.candidate_id,
            mode=self.mode,
            passed=not reasons,
            reason_codes=reasons,
            review_required=review or bool(reasons),
            dsr=dsr_dict,
            portfolio_corr=corr_dict,
            ledger=snapshot.to_dict() if snapshot is not None else None,
            notes=notes,
        )

    # -- バッチ (G2 逐次) --------------------------------------------------
    def evaluate_batch(
        self,
        evidences: Sequence[PromotionEvidence],
        snapshot: Optional[TrialSnapshot] = None,
        promoted_signals: Optional[Dict[str, Sequence[float]]] = None,
    ) -> Dict[str, PromotionGateVerdict]:
        """
        候補を与えられた順に判定し、昇格する候補を採用済み集合へ逐次追加する。

        Returns
        -------
        {candidate_id: verdict}。mode=off なら空辞書。
        """
        if self.mode == MODE_OFF:
            return {}
        portfolio: Dict[str, List[float]] = {
            k: list(v) for k, v in (promoted_signals or {}).items()
        }
        out: Dict[str, PromotionGateVerdict] = {}
        for ev in evidences:
            v = self.evaluate(ev, snapshot, portfolio)
            assert v is not None
            out[ev.candidate_id] = v
            will_promote = not v.blocks_promotion
            if will_promote and ev.signal is not None and len(ev.signal) >= MIN_SIGNAL_LEN:
                portfolio[f"batch:{ev.candidate_id}"] = list(ev.signal)
        return out
