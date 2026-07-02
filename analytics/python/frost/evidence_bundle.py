"""
evidence_bundle.py
------------------
Phase 2: EvidenceBundle — 型付き境界 (D5 負債解消)
Phase 3: GateEngine / ScoreEngine への委譲 (D2 / D3 負債解消)

背景 (D5 負債):
  Phase 1 以前は frost_selector.py の evaluate_candidate() が
  内部で 20 個超のキーワード引数を compute_frost_score() に渡す
  stringly-typed な結合になっていた。特徴量 dict のキーミス・型誤りが
  実行時まで検出されず、テストも困難だった。

Phase 2 で解消:
  1. ScoreComponents  — 個別スコア軸の型付きコンテナ
  2. GateVerdict      — Hard Gate の判定結果コンテナ
  3. EvidenceBundle   — 1 候補の全証拠を集約した中間表現
     - RawFeatures (特徴量 dict のラッパ)
     - ScoreComponents
     - GateVerdict
     - stability / pbo 中間結果
  4. evaluate_candidate_to_bundle() — EvidenceBundle を返す純関数
  5. evaluation_from_bundle()       — EvidenceBundle → FrostEvaluation 変換

Phase 3 で解消:
  - _evaluate_gates() を GateEngine.evaluate() に委譲 (D2: ゲート重複解消)
  - compute_frost_score/v2 直接呼び出しを ScoreEngine.fill_scores() に委譲 (D3 解消)
  - _evaluate_gates() は後方互換のためシムとして残す

設計原則:
  - frozen=True にしない (後工程で段階的に埋めるため)
  - pure Python, numpy 不使用 (Phase 7 まで凍結)
  - FrostEvaluation との後方互換は evaluation_from_bundle() が保証
  - PolicySpec を受け付け FrostConfig も引き続き受け付ける (後方互換)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


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
# ScoreComponents — 個別スコア軸
# ---------------------------------------------------------------------------

@dataclass
class ScoreComponents:
    """
    FROST スコア計算に必要な個別軸スコア。

    正方向スコア (0〜1) とペナルティ (0〜1) を分けて保持する。
    compute_frost_score() への 20 引数渡しを型安全に置き換える。
    """

    # ── 正方向スコア (v1 + v2) ────────────────────────────────────────────
    predictive_score:            float = 0.0
    oos_sharpe_score:            float = 0.0
    regime_stability_score:      float = 0.0
    selection_consistency_score: float = 0.0
    capacity_score:              float = 0.0
    diversification_score:       float = 0.5   # デフォルト中立値
    # v2 追加軸
    genome_novelty_score:        float = 0.5   # 未評価時は中立値
    causal_validity_score:       float = 0.5
    regime_entropy_score:        float = 0.5

    # ── ペナルティ (高いほど悪い) ──────────────────────────────────────────
    pbo_penalty:                 float = 0.0
    turnover_penalty:            float = 0.0
    complexity_penalty:          float = 0.0
    drawdown_penalty:            float = 0.0
    fragility_penalty:           float = 0.0
    # v2 追加ペナルティ
    crowding_penalty:            float = 0.0
    signal_duplication_penalty:  float = 0.0
    fragility_surface_penalty:   float = 0.0

    # ── 総合スコア ─────────────────────────────────────────────────────────
    frost_score_v1: float = 0.0
    frost_score_v2: float = 0.0

    def effective_frost_score(self, use_v2: bool = False) -> float:
        """use_v2 フラグに応じて適切なスコアを返す。"""
        return self.frost_score_v2 if use_v2 else self.frost_score_v1

    def to_dict(self) -> Dict[str, float]:
        """score_breakdown dict として diagnostics_json に埋め込む用。"""
        return {
            "predictive":               self.predictive_score,
            "oos_sharpe":               self.oos_sharpe_score,
            "regime_stability":         self.regime_stability_score,
            "selection_consistency":    self.selection_consistency_score,
            "capacity":                 self.capacity_score,
            "diversification":          self.diversification_score,
            "genome_novelty":           self.genome_novelty_score,
            "causal_validity":          self.causal_validity_score,
            "regime_entropy":           self.regime_entropy_score,
            "pbo_penalty":              self.pbo_penalty,
            "turnover_penalty":         self.turnover_penalty,
            "complexity_penalty":       self.complexity_penalty,
            "drawdown_penalty":         self.drawdown_penalty,
            "fragility_penalty":        self.fragility_penalty,
            "crowding_penalty":         self.crowding_penalty,
            "signal_duplication_penalty": self.signal_duplication_penalty,
            "fragility_surface_penalty":  self.fragility_surface_penalty,
            "frost_score_v1":           self.frost_score_v1,
            "frost_score_v2":           self.frost_score_v2,
        }


# ---------------------------------------------------------------------------
# GateVerdict — Hard Gate 判定結果
# ---------------------------------------------------------------------------

@dataclass
class GateVerdict:
    """
    Hard Gate の判定結果コンテナ。

    v1 ゲート (8 個) と v2 ゲート (7 個) の両方を保持する。
    ゲート名は人間可読な文字列で記録する。
    """

    # 総合判定
    passed: bool = True
    """全ゲートを通過した場合 True。"""

    # 失敗ゲート一覧
    failures: List[str] = field(default_factory=list)
    """失敗したゲートの詳細メッセージ一覧。"""

    # v1 個別ゲート判定 (True = PASS)
    gate_pbo:                bool = True
    gate_rank_ic:            bool = True
    gate_oos_sharpe:         bool = True
    gate_turnover:           bool = True
    gate_max_drawdown:       bool = True
    gate_regime_pass_ratio:  bool = True
    gate_complexity:         bool = True
    gate_selection_stability:bool = True

    # v2 追加ゲート
    gate_causal_direction:   bool = True
    gate_invariance:         bool = True
    gate_genome_novelty:     bool = True
    gate_crowding_r2:        bool = True
    gate_fsi:                bool = True
    gate_regime_entropy:     bool = True
    gate_signal_corr:        bool = True

    @classmethod
    def all_pass(cls) -> "GateVerdict":
        """全ゲート PASS の GateVerdict を生成する。"""
        return cls(passed=True, failures=[])

    def add_failure(self, gate_name: str, message: str) -> None:
        """ゲート失敗を追記する。"""
        self.failures.append(message)
        # 個別ゲートフラグを更新
        attr = f"gate_{gate_name}"
        if hasattr(self, attr):
            object.__setattr__(self, attr, False) if False else setattr(self, attr, False)
        self.passed = False

    def to_list(self) -> List[str]:
        """FrostEvaluation.hard_gate_failures と互換の文字列リスト。"""
        return list(self.failures)


# ---------------------------------------------------------------------------
# StabilityEvidence — 安定性中間結果
# ---------------------------------------------------------------------------

@dataclass
class StabilityEvidence:
    """
    compute_all_stability() の結果を型付きで保持する。
    """
    selection_consistency_score: float = 0.0
    top_k_stability:             Optional[float] = None
    sign_stability:              Optional[float] = None
    fold_sharpe_std:             float = 0.0
    fold_sharpe_mean:            float = 0.0
    fold_ic_mean:                float = 0.0
    n_folds:                     int   = 0


# ---------------------------------------------------------------------------
# PBOEvidence — PBO 中間結果
# ---------------------------------------------------------------------------

@dataclass
class PBOEvidence:
    """
    compute_pbo_all() の結果を型付きで保持する。
    """
    pbo_score:          float = 0.0
    pbo_raw:            float = 0.0
    selection_fragility:float = 0.0
    n_folds:            int   = 0


# ---------------------------------------------------------------------------
# RawFeatures — 特徴量 dict のラッパ
# ---------------------------------------------------------------------------

@dataclass
class RawFeatures:
    """
    extract_all_features() の戻り値を型付きで包む薄いラッパ。

    direct access: feat.get("rank_ic") の代わりに feat_obj.rank_ic を使える。
    Phase 5 以降で stringly-typed key を段階的に排除するための布石。
    """
    # 生 dict (後方互換用)
    _raw: Dict[str, Any] = field(default_factory=dict)

    # よく参照されるフィールドのみ明示 (残りは _raw 経由)
    rank_ic:             Optional[float] = None
    ic:                  Optional[float] = None
    ic_t_stat:           Optional[float] = None
    hit_rate:            Optional[float] = None
    oos_sharpe:          Optional[float] = None
    oos_sortino:         Optional[float] = None
    oos_calmar:          Optional[float] = None
    oos_max_drawdown:    Optional[float] = None
    regime_pass_ratio_raw: Optional[float] = None
    crisis_sharpe:       Optional[float] = None
    bull_sharpe:         Optional[float] = None
    turnover:            Optional[float] = None
    avg_hold_days:       Optional[float] = None
    var_5:               Optional[float] = None
    cvar_5:              Optional[float] = None
    downside_vol:        Optional[float] = None
    complexity_score:    float = 0.0
    fold_sharpes:        List[float] = field(default_factory=list)
    fold_ics:            List[float] = field(default_factory=list)
    fold_rank_ics:       List[float] = field(default_factory=list)
    regime_sharpes:      List[float] = field(default_factory=list)

    def get(self, key: str, default: Any = None) -> Any:
        """dict 互換インターフェース: 後方互換用。"""
        # 明示フィールドを優先
        if hasattr(self, key) and key != "_raw":
            val = getattr(self, key)
            if val is not None:
                return val
        return self._raw.get(key, default)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RawFeatures":
        """extract_all_features() の戻り値 dict から RawFeatures を生成する。"""
        return cls(
            _raw=d,
            rank_ic=            _safe_opt(d.get("rank_ic")),
            ic=                 _safe_opt(d.get("ic")),
            ic_t_stat=          _safe_opt(d.get("ic_t_stat")),
            hit_rate=           _safe_opt(d.get("hit_rate")),
            oos_sharpe=         _safe_opt(d.get("oos_sharpe")),
            oos_sortino=        _safe_opt(d.get("oos_sortino")),
            oos_calmar=         _safe_opt(d.get("oos_calmar")),
            oos_max_drawdown=   _safe_opt(d.get("oos_max_drawdown")),
            regime_pass_ratio_raw=_safe_opt(d.get("regime_pass_ratio_raw")),
            crisis_sharpe=      _safe_opt(d.get("crisis_sharpe")),
            bull_sharpe=        _safe_opt(d.get("bull_sharpe")),
            turnover=           _safe_opt(d.get("turnover")),
            avg_hold_days=      _safe_opt(d.get("avg_hold_days")),
            var_5=              _safe_opt(d.get("var_5")),
            cvar_5=             _safe_opt(d.get("cvar_5")),
            downside_vol=       _safe_opt(d.get("downside_vol")),
            complexity_score=   _safe(d.get("complexity_score", 0.0)),
            fold_sharpes=       list(d.get("fold_sharpes", [])),
            fold_ics=           list(d.get("fold_ics", [])),
            fold_rank_ics=      list(d.get("fold_rank_ics", [])),
            regime_sharpes=     list(d.get("regime_sharpes", [])),
        )


# ---------------------------------------------------------------------------
# EvidenceBundle — 1 候補の全証拠を集約した中間表現
# ---------------------------------------------------------------------------

@dataclass
class EvidenceBundle:
    """
    1 候補の評価に必要な全証拠をまとめた中間表現。

    evaluate_candidate_to_bundle() が生成し、
    evaluation_from_bundle() が FrostEvaluation に変換する。

    [フロー]
      FrostCandidate
        → extract_all_features()
        → RawFeatures          (特徴量の型付きラッパ)
        → compute_pbo_all()
        → PBOEvidence          (PBO 中間結果)
        → compute_all_stability()
        → StabilityEvidence    (安定性中間結果)
        → compute_*_score()
        → ScoreComponents      (個別スコア)
        → check_hard_gates()
        → GateVerdict          (Gate 判定結果)
        → EvidenceBundle       (全証拠の集約)
        → FrostEvaluation      (永続化形式)
    """

    # 識別子
    candidate_id: str = ""
    run_id:       str = ""
    trace_id:     str = ""

    # 証拠の各層
    features:   RawFeatures      = field(default_factory=RawFeatures)
    pbo:        PBOEvidence       = field(default_factory=PBOEvidence)
    stability:  StabilityEvidence = field(default_factory=StabilityEvidence)
    scores:     ScoreComponents   = field(default_factory=ScoreComponents)
    gate:       GateVerdict       = field(default_factory=GateVerdict.all_pass)

    # 入力 JSON (永続化用の参照コピー)
    metrics_json:   Dict[str, Any] = field(default_factory=dict)
    backtest_json:  Dict[str, Any] = field(default_factory=dict)
    regime_json:    Dict[str, Any] = field(default_factory=dict)

    def is_gate_passed(self) -> bool:
        """Hard Gate を全て通過したか。"""
        return self.gate.passed

    def effective_frost_score(self, use_v2: bool = False) -> float:
        """use_v2 フラグに応じた総合スコアを返す。"""
        return self.scores.effective_frost_score(use_v2)

    def to_diagnostics_dict(self) -> Dict[str, Any]:
        """
        FrostEvaluation.diagnostics_json に埋め込む辞書を生成する。

        後方互換: Phase 0 以前の diagnostics_json と同じキー構造を維持する。
        """
        return {
            "pbo_raw":             self.pbo.pbo_raw,
            "selection_fragility": self.pbo.selection_fragility,
            "fold_sharpe_std":     self.stability.fold_sharpe_std,
            "fold_sharpe_mean":    self.stability.fold_sharpe_mean,
            "fold_ic_mean":        self.stability.fold_ic_mean,
            "n_folds":             self.pbo.n_folds,
            "gate_failures":       self.gate.to_list(),
            "score_breakdown":     self.scores.to_dict(),
        }


# ---------------------------------------------------------------------------
# evaluate_candidate_to_bundle() — EvidenceBundle を返す純関数
# ---------------------------------------------------------------------------

def evaluate_candidate_to_bundle(
    candidate: Any,
    run_id: str,
    trace_id: str,
    config: Any,
) -> EvidenceBundle:
    """
    FrostCandidate を評価して EvidenceBundle を返す純関数。

    Phase 2 のコアロジック。
    既存の evaluate_candidate() と同等の計算を行うが、
    全中間結果を EvidenceBundle に型安全に格納する。

    Parameters
    ----------
    candidate : FrostCandidate
        評価対象の候補。
    run_id : str
    trace_id : str
    config : FrostConfig or PolicySpec
        FrostConfig / PolicySpec 両方を受け付ける (後方互換)。

    Returns
    -------
    EvidenceBundle
    """
    from analytics.python.frost.frost_features import extract_all_features
    from analytics.python.frost.frost_metrics import (
        compute_frost_score,
        compute_predictive_score,
        compute_oos_sharpe_score,
        compute_regime_stability_score,
        compute_capacity_score,
        compute_pbo_penalty,
        compute_turnover_penalty,
        compute_complexity_penalty,
        compute_drawdown_penalty,
        compute_fragility_penalty,
    )
    from analytics.python.frost.frost_pbo import compute_pbo_all
    from analytics.python.frost.frost_stability import compute_all_stability

    bundle = EvidenceBundle(
        candidate_id=candidate.candidate_id,
        run_id=run_id,
        trace_id=trace_id,
        metrics_json=dict(candidate.metrics or {}),
        backtest_json=dict(candidate.backtest_summary or {}),
        regime_json=dict(candidate.regime_breakdown or {}),
    )

    # ── Step 1: 特徴量抽出 ────────────────────────────────────────────────
    feat_dict = extract_all_features(candidate)
    bundle.features = RawFeatures.from_dict(feat_dict)

    # ── Step 2: PBO 計算 ──────────────────────────────────────────────────
    pbo_result = compute_pbo_all(
        candidate.fold_results,
        config.min_backtest_folds,
    )
    bundle.pbo = PBOEvidence(
        pbo_score=          pbo_result["pbo_score"],
        pbo_raw=            pbo_result.get("pbo_raw", 0.0),
        selection_fragility=pbo_result.get("selection_fragility", 0.0),
        n_folds=            pbo_result.get("n_folds", 0),
    )

    # ── Step 3: 安定性計算 ────────────────────────────────────────────────
    stability_result = compute_all_stability(
        bundle.features.fold_sharpes,
        bundle.features.fold_ics,
        bundle.features.fold_rank_ics,
        bundle.features.regime_sharpes,
        config.min_backtest_folds,
    )
    bundle.stability = StabilityEvidence(
        selection_consistency_score=stability_result["selection_consistency_score"],
        top_k_stability=            stability_result.get("top_k_stability"),
        sign_stability=             stability_result.get("sign_stability"),
        fold_sharpe_std=            stability_result.get("fold_sharpe_std", 0.0),
        fold_sharpe_mean=           stability_result.get("fold_sharpe_mean", 0.0),
        fold_ic_mean=               stability_result.get("fold_ic_mean", 0.0),
        n_folds=                    pbo_result.get("n_folds", 0),
    )

    # ── Step 4: 個別スコア計算 + ScoreEngine 委譲 (Phase 3: D3 解消) ──────
    predictive_score  = compute_predictive_score(feat_dict)
    oos_sharpe_score  = compute_oos_sharpe_score(feat_dict, config.min_oos_sharpe)
    regime_stab_score = compute_regime_stability_score(feat_dict)
    capacity_score    = compute_capacity_score(feat_dict)

    pbo_pen        = compute_pbo_penalty(feat_dict, bundle.pbo.pbo_score)
    turnover_pen   = compute_turnover_penalty(feat_dict, config.max_turnover)
    complexity_pen = compute_complexity_penalty(
        _safe(feat_dict.get("complexity_score", 0.0))
    )
    drawdown_pen   = compute_drawdown_penalty(feat_dict, config.max_drawdown)
    fragility_pen  = compute_fragility_penalty(
        feat_dict, bundle.stability.fold_sharpe_std
    )

    # ScoreComponents を組み立て (frost_score_v1/v2 は ScoreEngine が設定)
    raw_scores = ScoreComponents(
        predictive_score=            predictive_score,
        oos_sharpe_score=            oos_sharpe_score,
        regime_stability_score=      regime_stab_score,
        selection_consistency_score= bundle.stability.selection_consistency_score,
        capacity_score=              capacity_score,
        pbo_penalty=                 pbo_pen,
        turnover_penalty=            turnover_pen,
        complexity_penalty=          complexity_pen,
        drawdown_penalty=            drawdown_pen,
        fragility_penalty=           fragility_pen,
    )

    # Phase 3: ScoreEngine に委譲して frost_score_v1/v2 を設定 (D3 解消)
    from analytics.python.frost.score_engine import ScoreEngine
    use_v2 = getattr(config, "use_v2_score", False)
    bundle.scores = ScoreEngine.from_config(config).fill_scores(raw_scores, use_v2=use_v2)

    # ── Step 5: Hard Gate 判定 (Phase 3: GateEngine に委譲, D2 解消) ─────
    from analytics.python.frost.gate_engine import GateEngine
    bundle.gate = GateEngine.from_config(config).evaluate(
        bundle.features, bundle.pbo, bundle.stability
    )

    return bundle


# ---------------------------------------------------------------------------
# _evaluate_gates() — 後方互換シム (Phase 3: GateEngine に委譲)
# ---------------------------------------------------------------------------

def _evaluate_gates(
    features:   RawFeatures,
    pbo:        PBOEvidence,
    stability:  StabilityEvidence,
    config:     Any,
) -> GateVerdict:
    """
    全 Hard Gate を評価して GateVerdict を返す。

    Phase 3: このシムは後方互換のためにインターフェースを維持するが、
    内部実装は GateEngine.evaluate() に完全委譲している。
    直接呼び出しは非推奨。代わりに GateEngine.evaluate() を使用すること。

    Parameters
    ----------
    features : RawFeatures
    pbo : PBOEvidence
    stability : StabilityEvidence
    config : FrostConfig or PolicySpec

    Returns
    -------
    GateVerdict
    """
    from analytics.python.frost.gate_engine import GateEngine
    return GateEngine.from_config(config).evaluate(features, pbo, stability)


# ---------------------------------------------------------------------------
# evaluation_from_bundle() — EvidenceBundle → FrostEvaluation 変換
# ---------------------------------------------------------------------------

def evaluation_from_bundle(
    bundle: EvidenceBundle,
    use_v2_score: bool = False,
) -> Any:
    """
    EvidenceBundle を FrostEvaluation に変換する。

    後方互換性を完全に保ちながら EvidenceBundle の型安全な中間表現を
    永続化形式 (FrostEvaluation) に変換する。

    Parameters
    ----------
    bundle : EvidenceBundle
    use_v2_score : bool
        True の場合は frost_score_v2 を FrostEvaluation.frost_score に設定。

    Returns
    -------
    FrostEvaluation
    """
    from analytics.python.frost.frost_contracts import FrostEvaluation

    frost_score = bundle.effective_frost_score(use_v2_score)

    return FrostEvaluation(
        run_id=       bundle.run_id,
        candidate_id= bundle.candidate_id,
        trace_id=     bundle.trace_id,
        # 予測力
        predictive_score= bundle.scores.predictive_score,
        rank_ic=          bundle.features.rank_ic,
        ic=               bundle.features.ic,
        ic_t_stat=        bundle.features.ic_t_stat,
        hit_rate=         bundle.features.hit_rate,
        # OOS
        oos_sharpe=       bundle.features.oos_sharpe,
        oos_sortino=      bundle.features.oos_sortino,
        oos_calmar=       bundle.features.oos_calmar,
        oos_max_drawdown= bundle.features.oos_max_drawdown,
        # レジーム
        regime_stability_score= bundle.scores.regime_stability_score,
        regime_pass_ratio=      bundle.features.regime_pass_ratio_raw,
        crisis_sharpe=          bundle.features.crisis_sharpe,
        bull_sharpe=            bundle.features.bull_sharpe,
        # 選抜整合性
        selection_consistency_score= bundle.stability.selection_consistency_score,
        top_k_stability=             bundle.stability.top_k_stability,
        sign_stability=              bundle.stability.sign_stability,
        # キャパシティ
        capacity_score=  bundle.scores.capacity_score,
        turnover=        bundle.features.turnover,
        avg_hold_days=   bundle.features.avg_hold_days,
        # リスク
        tail_risk_score= bundle.features.cvar_5,
        var_5=           bundle.features.var_5,
        cvar_5=          bundle.features.cvar_5,
        downside_vol=    bundle.features.downside_vol,
        # ペナルティ
        pbo_score=          bundle.scores.pbo_penalty,
        turnover_penalty=   bundle.scores.turnover_penalty,
        complexity_penalty= bundle.scores.complexity_penalty,
        drawdown_penalty=   bundle.scores.drawdown_penalty,
        fragility_penalty=  bundle.scores.fragility_penalty,
        # 総合
        frost_score= frost_score,
        # JSON
        metrics_json=    bundle.metrics_json,
        backtest_json=   bundle.backtest_json,
        regime_json=     bundle.regime_json,
        diagnostics_json=bundle.to_diagnostics_dict(),
        # Gate
        hard_gate_passed=  bundle.gate.passed,
        hard_gate_failures=bundle.gate.to_list(),
    )
