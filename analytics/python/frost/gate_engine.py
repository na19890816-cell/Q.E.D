"""
gate_engine.py
--------------
Phase 3: GateEngine — Hard Gate 判定の単一責任クラス (D2 負債解消)

背景 (D2 負債):
  Phase 2 以前はゲート判定ロジックが 2 箇所に散在していた:
    1. frost_selector.py::check_hard_gates()  — dict-based, 旧 API
    2. evidence_bundle.py::_evaluate_gates()  — RawFeatures/PBOEvidence-based, 新 API

  両者は実質同じロジックを重複して保持しており、一方を変更すると
  もう一方との整合性が崩れるリスクがあった。

Phase 3 での解消:
  GateEngine を単一の「ゲート判定エンジン」として定義し、
  すべてのゲート判定ロジックをここに集約する。

  - _evaluate_gates() (evidence_bundle.py) → GateEngine.evaluate() に委譲
  - check_hard_gates() (frost_selector.py)  → GateEngine.evaluate_from_dict() に委譲
  - v2 ゲート (frost_selector.py 未実装部分) も GateEngine が担う

設計原則:
  - pure Python, numpy 不使用 (Phase 7 まで凍結)
  - 副作用なし (純関数)
  - FrostConfig / PolicySpec 両方を config として受け付ける (後方互換)
  - GateVerdict との型境界を明確に保つ
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from analytics.python.frost.evidence_bundle import (
    GateVerdict,
    PBOEvidence,
    RawFeatures,
    StabilityEvidence,
)


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _safe(v: Any, default: float = 0.0) -> float:
    """NaN/Inf/None を安全に float に変換する。"""
    try:
        f = float(v)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


def _safe_opt(v: Any) -> Optional[float]:
    """NaN/Inf → None に変換する Optional float ラッパ。"""
    if v is None:
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# ゲート定義レジストリ
# ---------------------------------------------------------------------------

#: v1 ゲート名一覧 (GateVerdict.gate_{name} と対応)
V1_GATE_NAMES: List[str] = [
    "pbo",
    "rank_ic",
    "oos_sharpe",
    "turnover",
    "max_drawdown",
    "regime_pass_ratio",
    "complexity",
    "selection_stability",
]

#: v2 追加ゲート名一覧
V2_GATE_NAMES: List[str] = [
    "causal_direction",
    "invariance",
    "genome_novelty",
    "crowding_r2",
    "fsi",
    "regime_entropy",
    "signal_corr",
]


# ---------------------------------------------------------------------------
# GateEngine
# ---------------------------------------------------------------------------

@dataclass
class GateEngine:
    """
    Hard Gate 判定エンジン。

    Phase 3 でゲート判定ロジックを一箇所に集約する。
    FrostConfig / PolicySpec 両方を受け付けるよう config を Any 型で扱う。

    使い方:
        engine = GateEngine(config)
        verdict = engine.evaluate(features, pbo, stability)
        # または dict-based API (後方互換)
        passed, failures = engine.evaluate_from_dict(feat_dict, pbo_score, scs_score)
    """

    config: Any
    """FrostConfig または PolicySpec インスタンス。"""

    # ── v2 ゲート有効フラグ ────────────────────────────────────────────
    enable_v2_gates: bool = False
    """True の場合、v2 追加ゲートも評価する。"""

    def evaluate(
        self,
        features: RawFeatures,
        pbo: PBOEvidence,
        stability: StabilityEvidence,
    ) -> GateVerdict:
        """
        型付き証拠から GateVerdict を返す。

        evidence_bundle.py::_evaluate_gates() の代替。
        Phase 3 以降はこちらを使用する。

        Parameters
        ----------
        features : RawFeatures
        pbo : PBOEvidence
        stability : StabilityEvidence

        Returns
        -------
        GateVerdict
        """
        verdict = GateVerdict.all_pass()
        cfg = self.config

        # ── Gate 1: PBO ──────────────────────────────────────────────────
        if pbo.pbo_score > cfg.pbo_threshold:
            verdict.add_failure(
                "pbo",
                f"pbo={pbo.pbo_score:.4f} > threshold={cfg.pbo_threshold:.4f}",
            )

        # ── Gate 2: Rank IC ───────────────────────────────────────────────
        rank_ic = features.rank_ic
        if rank_ic is not None and abs(rank_ic) < cfg.min_rank_ic:
            verdict.add_failure(
                "rank_ic",
                f"rank_ic={rank_ic:.4f} < min={cfg.min_rank_ic:.4f}",
            )

        # ── Gate 3: OOS Sharpe ────────────────────────────────────────────
        oos_sharpe = features.oos_sharpe
        if oos_sharpe is not None and oos_sharpe < cfg.min_oos_sharpe:
            verdict.add_failure(
                "oos_sharpe",
                f"oos_sharpe={oos_sharpe:.4f} < min={cfg.min_oos_sharpe:.4f}",
            )

        # ── Gate 4: Turnover ──────────────────────────────────────────────
        turnover = features.turnover
        if turnover is not None and turnover > cfg.max_turnover:
            verdict.add_failure(
                "turnover",
                f"turnover={turnover:.2f} > max={cfg.max_turnover:.2f}",
            )

        # ── Gate 5: Max Drawdown ──────────────────────────────────────────
        oos_mdd = features.oos_max_drawdown
        if oos_mdd is not None:
            abs_mdd = abs(oos_mdd)
            if abs_mdd > cfg.max_drawdown:
                verdict.add_failure(
                    "max_drawdown",
                    f"max_drawdown={abs_mdd:.4f} > max={cfg.max_drawdown:.4f}",
                )

        # ── Gate 6: Regime pass ratio ─────────────────────────────────────
        regime_pass = features.regime_pass_ratio_raw
        if regime_pass is not None and regime_pass < cfg.min_regime_pass_ratio:
            verdict.add_failure(
                "regime_pass_ratio",
                f"regime_pass_ratio={regime_pass:.4f} < min={cfg.min_regime_pass_ratio:.4f}",
            )

        # ── Gate 7: Complexity ────────────────────────────────────────────
        complexity = features.complexity_score
        if complexity > cfg.max_complexity_score:
            verdict.add_failure(
                "complexity",
                f"complexity={complexity:.4f} > max={cfg.max_complexity_score:.4f}",
            )

        # ── Gate 8: Selection stability ───────────────────────────────────
        if stability.selection_consistency_score < cfg.min_selection_stability:
            verdict.add_failure(
                "selection_stability",
                f"selection_stability={stability.selection_consistency_score:.4f} "
                f"< min={cfg.min_selection_stability:.4f}",
            )

        # ── v2 追加ゲート (enable_v2_gates=True 時のみ) ────────────────────
        if self.enable_v2_gates:
            self._evaluate_v2_gates(verdict, features)

        return verdict

    def evaluate_from_dict(
        self,
        feat: Dict[str, Any],
        pbo_score: float,
        selection_consistency_score: float,
    ) -> Tuple[bool, List[str]]:
        """
        dict ベースの後方互換 API。

        frost_selector.py::check_hard_gates() の代替。
        内部で RawFeatures / PBOEvidence / StabilityEvidence に変換して
        evaluate() を呼び出す。

        Parameters
        ----------
        feat : dict
            extract_all_features() の戻り値
        pbo_score : float
            compute_pbo_all() の pbo_score
        selection_consistency_score : float
            compute_all_stability() の selection_consistency_score

        Returns
        -------
        tuple (hard_gate_passed: bool, gate_failures: list of str)
        """
        features = RawFeatures.from_dict(feat)
        pbo = PBOEvidence(pbo_score=pbo_score)
        stability = StabilityEvidence(
            selection_consistency_score=selection_consistency_score
        )
        verdict = self.evaluate(features, pbo, stability)
        return (verdict.passed, verdict.to_list())

    # ── v2 ゲート評価 ─────────────────────────────────────────────────────

    def _evaluate_v2_gates(
        self,
        verdict: GateVerdict,
        features: RawFeatures,
    ) -> None:
        """
        v2 追加ゲートを評価して verdict を更新する。

        v2 ゲートは _raw dict 経由で取得する
        (RawFeatures の明示フィールドにはまだ含まれていないため)。
        """
        cfg = self.config
        raw = features._raw

        # Gate v2-1: Causal direction score
        causal_dir = _safe_opt(raw.get("causal_direction_score"))
        min_causal = _safe(getattr(cfg, "min_causal_direction_score", 0.60))
        if causal_dir is not None and causal_dir < min_causal:
            verdict.add_failure(
                "causal_direction",
                f"causal_direction_score={causal_dir:.4f} < min={min_causal:.4f}",
            )

        # Gate v2-2: Invariance pass ratio
        invariance = _safe_opt(raw.get("invariance_pass_ratio"))
        min_invariance = _safe(getattr(cfg, "min_invariance_pass_ratio", 0.70))
        if invariance is not None and invariance < min_invariance:
            verdict.add_failure(
                "invariance",
                f"invariance_pass_ratio={invariance:.4f} < min={min_invariance:.4f}",
            )

        # Gate v2-3: Genome novelty
        novelty = _safe_opt(raw.get("genome_novelty_score"))
        min_novelty = _safe(getattr(cfg, "min_genome_novelty_score", 0.20))
        if novelty is not None and novelty < min_novelty:
            verdict.add_failure(
                "genome_novelty",
                f"genome_novelty_score={novelty:.4f} < min={min_novelty:.4f}",
            )

        # Gate v2-4: Crowding R²
        crowding_r2 = _safe_opt(raw.get("crowding_r2"))
        max_r2 = _safe(getattr(cfg, "max_crowding_r2", 0.80))
        if crowding_r2 is not None and crowding_r2 > max_r2:
            verdict.add_failure(
                "crowding_r2",
                f"crowding_r2={crowding_r2:.4f} > max={max_r2:.4f}",
            )

        # Gate v2-5: FSI (Fragility Surface Index)
        fsi = _safe_opt(raw.get("fsi"))
        max_fsi = _safe(getattr(cfg, "max_fsi", 0.40))
        if fsi is not None and fsi > max_fsi:
            verdict.add_failure(
                "fsi",
                f"fsi={fsi:.4f} > max={max_fsi:.4f}",
            )

        # Gate v2-6: Regime entropy
        regime_entropy = _safe_opt(raw.get("regime_entropy"))
        min_entropy = _safe(getattr(cfg, "min_regime_entropy", 0.60))
        if regime_entropy is not None and regime_entropy < min_entropy:
            verdict.add_failure(
                "regime_entropy",
                f"regime_entropy={regime_entropy:.4f} < min={min_entropy:.4f}",
            )

        # Gate v2-7: Signal correlation (dedup gate)
        signal_corr = _safe_opt(raw.get("signal_corr"))
        max_corr = _safe(getattr(cfg, "max_signal_corr", 0.90))
        if signal_corr is not None and signal_corr > max_corr:
            verdict.add_failure(
                "signal_corr",
                f"signal_corr={signal_corr:.4f} > max={max_corr:.4f}",
            )

    # ── クラスメソッド ───────────────────────────────────────────────────

    @classmethod
    def from_config(cls, config: Any, enable_v2_gates: bool = False) -> "GateEngine":
        """FrostConfig または PolicySpec から GateEngine を生成する。"""
        return cls(config=config, enable_v2_gates=enable_v2_gates)
