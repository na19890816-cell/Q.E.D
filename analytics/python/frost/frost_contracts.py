"""
frost_contracts.py
------------------
FROST Meta-Fitness Engine の全 dataclass 定義。

モジュール間の型境界を明確にするため、全データ転送オブジェクトをここに集約する。
PostgreSQL スキーマ (062_frost_evaluations.sql 等) と 1:1 対応させる。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 入力: 候補
# ---------------------------------------------------------------------------

@dataclass
class FrostCandidate:
    """
    FROST に投入する評価候補。

    EML / event-derived / technical 等の上流から受け取るデータ。
    frost_fitness_candidates テーブルの 1 行に対応する。
    """

    # 識別子
    candidate_id: str = field(default_factory=_new_uuid)
    run_id: str = ""
    trace_id: str = ""

    # ソース情報
    source_type: str = "eml"
    """eml / technical / event_derived / regime_aware / scrapling / existing_alpha"""
    source_candidate_id: Optional[str] = None
    """上流 candidate_id (EML の場合は eml_alpha_candidates.candidate_id)。"""

    # 候補定義
    formula_text: Optional[str] = None
    real_safe_formula_text: Optional[str] = None
    feature_spec_json: Dict[str, Any] = field(default_factory=dict)
    complexity_score: float = 0.0
    horizon: str = "5d"
    candidate_hash: str = ""

    # バックテスト入力 (上流から受け取る生データ)
    backtest_summary: Dict[str, Any] = field(default_factory=dict)
    """backtest_summary_json: oos_sharpe, max_dd, turnover 等を含む辞書。"""

    metrics: Dict[str, Any] = field(default_factory=dict)
    """metrics_json: rank_ic, ic, hit_rate 等を含む辞書。"""

    cost_model: Dict[str, Any] = field(default_factory=dict)
    """cost_model_json: コスト設定。"""

    regime_breakdown: Dict[str, Any] = field(default_factory=dict)
    """regime_breakdown_json: bull/bear/crisis ごとの性能。"""

    fold_results: List[Dict[str, Any]] = field(default_factory=list)
    """fold ごとの backtest 結果リスト。安定性評価に使用。"""

    # メタ
    status: str = "pending"
    created_at: datetime = field(default_factory=_now_utc)
    updated_at: datetime = field(default_factory=_now_utc)


# ---------------------------------------------------------------------------
# 評価結果
# ---------------------------------------------------------------------------

@dataclass
class FrostEvaluation:
    """
    候補ごとの FROST 評価結果。

    frost_evaluations テーブルの 1 行に対応する。
    """

    # 識別子
    evaluation_id: str = field(default_factory=_new_uuid)
    run_id: str = ""
    candidate_id: str = ""
    trace_id: str = ""

    # ── 予測力 ──────────────────────────────────────────────────────────
    predictive_score: float = 0.0
    rank_ic: Optional[float] = None
    ic: Optional[float] = None
    ic_t_stat: Optional[float] = None
    hit_rate: Optional[float] = None

    # ── OOS 性能 ─────────────────────────────────────────────────────────
    oos_sharpe: Optional[float] = None
    oos_sortino: Optional[float] = None
    oos_calmar: Optional[float] = None
    oos_max_drawdown: Optional[float] = None

    # ── レジーム安定性 ────────────────────────────────────────────────────
    regime_stability_score: Optional[float] = None
    regime_pass_ratio: Optional[float] = None
    crisis_sharpe: Optional[float] = None
    bull_sharpe: Optional[float] = None

    # ── 選抜整合性 ────────────────────────────────────────────────────────
    selection_consistency_score: Optional[float] = None
    top_k_stability: Optional[float] = None
    sign_stability: Optional[float] = None

    # ── キャパシティ・ターンオーバー ─────────────────────────────────────
    capacity_score: Optional[float] = None
    turnover: Optional[float] = None
    avg_hold_days: Optional[float] = None

    # ── リスク ───────────────────────────────────────────────────────────
    tail_risk_score: Optional[float] = None
    var_5: Optional[float] = None
    cvar_5: Optional[float] = None
    downside_vol: Optional[float] = None

    # ── ペナルティ ────────────────────────────────────────────────────────
    pbo_score: float = 0.0
    turnover_penalty: float = 0.0
    complexity_penalty: float = 0.0
    drawdown_penalty: float = 0.0
    fragility_penalty: float = 0.0

    # ── 総合スコア ────────────────────────────────────────────────────────
    frost_score: float = 0.0

    # ── 詳細 JSON ─────────────────────────────────────────────────────────
    metrics_json: Dict[str, Any] = field(default_factory=dict)
    backtest_json: Dict[str, Any] = field(default_factory=dict)
    regime_json: Dict[str, Any] = field(default_factory=dict)
    diagnostics_json: Dict[str, Any] = field(default_factory=dict)

    # ── Hard Gate ─────────────────────────────────────────────────────────
    hard_gate_passed: bool = True
    hard_gate_failures: List[str] = field(default_factory=list)

    # メタ
    created_at: datetime = field(default_factory=_now_utc)
    updated_at: datetime = field(default_factory=_now_utc)


# ---------------------------------------------------------------------------
# 採択決定
# ---------------------------------------------------------------------------

@dataclass
class FrostDecision:
    """
    候補ごとの採択判断。

    frost_selection_decisions テーブルの 1 行に対応する。
    decision は SELECTED / HOLD / REJECTED / REVIEW_REQUIRED の 4 値。
    """

    # 識別子
    decision_id: str = field(default_factory=_new_uuid)
    run_id: str = ""
    candidate_id: str = ""
    trace_id: str = ""

    # 判断
    decision: str = "REJECTED"
    """SELECTED / HOLD / REJECTED / REVIEW_REQUIRED"""
    decision_reason: str = ""
    decision_rank: Optional[int] = None

    # スコア (decision と同時に保存)
    frost_score: float = 0.0

    # 昇格
    promotion_eligible: bool = False
    review_required: bool = True
    review_status: str = "pending"
    """pending / approved / rejected / deferred"""
    reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None

    # 詳細
    rejection_reasons: List[str] = field(default_factory=list)
    gate_failures: List[str] = field(default_factory=list)

    # 近似重複
    near_duplicate_of: Optional[str] = None
    suppressed_by_dedup: bool = False

    # メタ
    created_at: datetime = field(default_factory=_now_utc)
    updated_at: datetime = field(default_factory=_now_utc)


# ---------------------------------------------------------------------------
# 実行出力
# ---------------------------------------------------------------------------

@dataclass
class FrostRunOutput:
    """
    FROST 1 実行の最終出力。

    frost_runner.py から返され、postgres_frost_writer.py に渡される。
    """

    # 実行 ID
    run_id: str = field(default_factory=_new_uuid)
    trace_id: str = ""
    batch_label: str = "frost_v1"
    engine_version: str = "frost_v1"

    # 結果リスト
    candidates: List[FrostCandidate] = field(default_factory=list)
    evaluations: List[FrostEvaluation] = field(default_factory=list)
    decisions: List[FrostDecision] = field(default_factory=list)

    # 統計
    candidate_count: int = 0
    evaluated_count: int = 0
    selected_count: int = 0
    hold_count: int = 0
    rejected_count: int = 0
    promotion_count: int = 0

    # 設定スナップショット
    config_snapshot: Dict[str, Any] = field(default_factory=dict)

    # Phase 1: PolicySpec ハッシュ (qed_policies への参照キー)
    policy_hash: Optional[str] = None
    """
    PolicySpec の SHA-256 ハッシュ。"sha256:<64 hex chars>" 形式。
    frost_runs.policy_hash に書き込まれる。
    Phase 0 以前の run では None になる場合がある（後方互換）。
    """

    # 実行状態
    status: str = "completed"
    """running / completed / failed / skipped / dry_run"""
    dry_run: bool = False
    error_message: Optional[str] = None

    # タイムスタンプ
    started_at: datetime = field(default_factory=_now_utc)
    ended_at: Optional[datetime] = None

    def selected_candidates(self) -> List[FrostDecision]:
        """SELECTED 判定の決定一覧を返す。"""
        return [d for d in self.decisions if d.decision == "SELECTED"]

    def promotion_eligible_decisions(self) -> List[FrostDecision]:
        """昇格適格な決定一覧を返す。"""
        return [d for d in self.decisions if d.promotion_eligible]

    def rejected_decisions(self) -> List[FrostDecision]:
        """REJECTED 判定の決定一覧を返す。"""
        return [d for d in self.decisions if d.decision == "REJECTED"]

    def get_evaluation(self, candidate_id: str) -> Optional[FrostEvaluation]:
        """candidate_id から評価結果を取得する。"""
        for ev in self.evaluations:
            if ev.candidate_id == candidate_id:
                return ev
        return None


# ---------------------------------------------------------------------------
# Audit event レコード
# ---------------------------------------------------------------------------

@dataclass
class FrostAuditRecord:
    """
    frost_audit_event_bridges に書き込む 1 イベント。

    postgres_frost_audit_bridge.py が生成し、emit する。
    """

    audit_bridge_id: str = field(default_factory=_new_uuid)
    run_id: str = ""
    candidate_id: Optional[str] = None
    trace_id: str = ""

    event_name: str = ""
    """frost.run.started / frost.candidate.evaluated / frost.candidate.selected / etc."""

    event_status: str = "emitted"
    """emitted / failed / skipped"""

    decision: str = "APPLIED"
    """APPLIED / DRY_RUN / REJECTED / CONFLICTED"""

    audit_event_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    occurred_at: datetime = field(default_factory=_now_utc)
    created_at: datetime = field(default_factory=_now_utc)


# ---------------------------------------------------------------------------
# Promotion bridge レコード
# ---------------------------------------------------------------------------

@dataclass
class FrostPromotionRecord:
    """
    frost_promotion_bridges に書き込む 1 レコード。

    postgres_frost_promotion_bridge.py が生成し、upsert する。
    """

    bridge_id: str = field(default_factory=_new_uuid)
    run_id: str = ""
    candidate_id: str = ""
    trace_id: str = ""

    target_entity_type: str = "candidate"
    """candidate / hypothesis / knowledge_artifact / experiment_report"""
    target_entity_id: Optional[str] = None
    artifact_id: Optional[str] = None
    link_id: Optional[str] = None

    promotion_status: str = "pending"
    """pending / applied / dry_run / rejected / conflicted / error"""
    promotion_payload: Dict[str, Any] = field(default_factory=dict)
    frost_score: Optional[float] = None
    decision_rank: Optional[int] = None

    promoted_at: Optional[datetime] = None
    error_message: Optional[str] = None

    created_at: datetime = field(default_factory=_now_utc)
    updated_at: datetime = field(default_factory=_now_utc)
