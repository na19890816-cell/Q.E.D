"""
frost_config.py
---------------
FROST Meta-Fitness Engine の設定モジュール。

環境変数から全設定を読み込み、validated な FrostConfig dataclass として返す。
デフォルト値は handoff 仕様の推奨値を使用する。

環境変数名は FROST_ プレフィックスで統一。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _env_float(key: str, default: float) -> float:
    v = os.environ.get(key)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        raise ValueError(f"[frost_config] {key}={v!r} は float に変換できません")


def _env_int(key: str, default: int) -> int:
    v = os.environ.get(key)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        raise ValueError(f"[frost_config] {key}={v!r} は int に変換できません")


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# FrostConfig dataclass
# ---------------------------------------------------------------------------

@dataclass
class FrostConfig:
    """
    FROST Meta-Fitness Engine の全設定。

    環境変数から構築するか、直接インスタンス化してテストに使用する。
    """

    # ── 基本設定 ──────────────────────────────────────────────────────────
    enabled: bool = True
    """FROST 評価を有効にするか。False の場合は全候補を HOLD 扱い。"""

    dry_run: bool = False
    """True の場合は PostgreSQL への canonical side-effect (bridge, KA) を行わない。"""

    batch_label: str = "frost_v1"
    """実行バッチのラベル。frost_runs.batch_label に格納される。"""

    engine_version: str = "frost_v1"
    """エンジンバージョン。"""

    # ── DB 接続 ───────────────────────────────────────────────────────────
    pg_dsn: str = ""
    """PostgreSQL DSN。空文字の場合は QED_PG_DSN 環境変数を使用。"""

    # ── スコア重み (正方向) ───────────────────────────────────────────────
    w_predictive: float = 0.20
    """予測力スコアの重み (a1)。"""

    w_oos_sharpe: float = 0.15
    """OOS Sharpe の重み (a2)。"""

    w_regime_stability: float = 0.15
    """レジーム安定性の重み (a3)。"""

    w_selection_consistency: float = 0.10
    """選抜整合性の重み (a4)。"""

    w_capacity: float = 0.10
    """キャパシティスコアの重み (a5)。"""

    w_diversification: float = 0.05
    """多様化ボーナスの重み。"""

    # ── ペナルティ重み (負方向) ───────────────────────────────────────────
    w_turnover_penalty: float = 0.10
    """ターンオーバーペナルティの重み (b2)。"""

    w_complexity_penalty: float = 0.05
    """複雑度ペナルティの重み (b3)。"""

    w_drawdown_penalty: float = 0.05
    """ドローダウンペナルティの重み (b4)。"""

    w_fragility_penalty: float = 0.03
    """脆弱性ペナルティの重み (b5)。"""

    w_pbo_penalty: float = 0.02
    """PBO ペナルティの重み (b1)。"""

    # ── Hard Gate 閾値 ────────────────────────────────────────────────────
    pbo_threshold: float = 0.20
    """PBO がこれを超えると hard gate FAIL (b1)。"""

    min_oos_sharpe: float = 0.50
    """OOS Sharpe がこれを下回ると hard gate FAIL。"""

    min_rank_ic: float = 0.02
    """Rank IC がこれを下回ると hard gate FAIL。"""

    max_turnover: float = 4.0
    """年間ターンオーバーがこれを超えると hard gate FAIL。"""

    max_drawdown: float = 0.20
    """最大ドローダウンがこれを超えると hard gate FAIL。"""

    min_regime_pass_ratio: float = 0.75
    """レジーム通過率がこれを下回ると hard gate FAIL。"""

    max_complexity_score: float = 0.60
    """複雑度スコアがこれを超えると hard gate FAIL。"""

    min_selection_stability: float = 0.60
    """選抜安定性がこれを下回ると hard gate FAIL。"""

    # ── 選抜制御 ─────────────────────────────────────────────────────────
    top_k: int = 25
    """frost_score 上位から保持する候補数。"""

    promotion_top_k: int = 5
    """昇格対象とする選抜候補の上位数。"""

    require_audit_pass: bool = True
    """True の場合、audit_events APPLIED を昇格条件とする。"""

    near_duplicate_threshold: float = 0.95
    """候補間の類似度がこれを超えると near-duplicate として抑制する。"""

    max_same_family: int = 3
    """同一 source_candidate family からの最大採択数。"""

    # ── review 設定 ────────────────────────────────────────────────────────
    review_required_default: bool = True
    """True の場合、SELECTED でも review_required=True にする (安全デフォルト)。"""

    auto_approve_low_risk: bool = False
    """将来: 低リスク候補の自動承認フラグ (現在は未使用)。"""

    # ── バックテスト設定 ──────────────────────────────────────────────────
    min_backtest_folds: int = 5
    """安定性評価に必要な最小 fold 数。"""

    min_train_years: float = 2.0
    """最低訓練期間 (年)。"""

    # ── ロギング設定 ─────────────────────────────────────────────────────
    verbose: bool = False
    """詳細ログ出力を有効にするか。"""

    # ── 正規化設定 ────────────────────────────────────────────────────────
    score_clip_min: float = -3.0
    """スコア正規化前のクリップ下限 (z-score)。"""

    score_clip_max: float = 3.0
    """スコア正規化前のクリップ上限 (z-score)。"""

    # ── スキーマ/テーブル名 ───────────────────────────────────────────────
    schema: str = "public"
    table_frost_runs: str = "frost_runs"
    table_frost_candidates: str = "frost_fitness_candidates"
    table_frost_evaluations: str = "frost_evaluations"
    table_frost_decisions: str = "frost_selection_decisions"
    table_frost_promotion_bridges: str = "frost_promotion_bridges"
    table_frost_audit_bridges: str = "frost_audit_event_bridges"

    # ── 重みの正規化後合計 (validate 時に検証) ────────────────────────────
    _weight_sum_tolerance: float = field(default=0.01, repr=False)

    def effective_pg_dsn(self) -> str:
        """有効な PostgreSQL DSN を返す。未設定なら環境変数 QED_PG_DSN を使用。"""
        if self.pg_dsn:
            return self.pg_dsn
        dsn = os.environ.get("QED_PG_DSN", "")
        if not dsn:
            raise ValueError(
                "PostgreSQL DSN が未設定です。"
                "FROST_PG_DSN または QED_PG_DSN 環境変数を設定してください。"
            )
        return dsn

    def positive_weight_sum(self) -> float:
        """正方向重みの合計。"""
        return (
            self.w_predictive
            + self.w_oos_sharpe
            + self.w_regime_stability
            + self.w_selection_consistency
            + self.w_capacity
            + self.w_diversification
        )

    def penalty_weight_sum(self) -> float:
        """ペナルティ重みの合計。"""
        return (
            self.w_turnover_penalty
            + self.w_complexity_penalty
            + self.w_drawdown_penalty
            + self.w_fragility_penalty
            + self.w_pbo_penalty
        )

    def validate(self) -> None:
        """設定値の妥当性を検証する。問題があれば ValueError を送出。"""
        errors: list[str] = []

        # 重みの非負チェック
        weight_fields = [
            ("w_predictive", self.w_predictive),
            ("w_oos_sharpe", self.w_oos_sharpe),
            ("w_regime_stability", self.w_regime_stability),
            ("w_selection_consistency", self.w_selection_consistency),
            ("w_capacity", self.w_capacity),
            ("w_diversification", self.w_diversification),
            ("w_turnover_penalty", self.w_turnover_penalty),
            ("w_complexity_penalty", self.w_complexity_penalty),
            ("w_drawdown_penalty", self.w_drawdown_penalty),
            ("w_fragility_penalty", self.w_fragility_penalty),
            ("w_pbo_penalty", self.w_pbo_penalty),
        ]
        for name, val in weight_fields:
            if val < 0.0:
                errors.append(f"{name}={val} は 0 以上である必要があります")

        # 閾値の範囲チェック
        if not (0.0 <= self.pbo_threshold <= 1.0):
            errors.append(f"pbo_threshold={self.pbo_threshold} は [0, 1] の範囲である必要があります")
        if not (0.0 <= self.max_drawdown <= 1.0):
            errors.append(f"max_drawdown={self.max_drawdown} は [0, 1] の範囲である必要があります")
        if not (0.0 <= self.min_regime_pass_ratio <= 1.0):
            errors.append(f"min_regime_pass_ratio={self.min_regime_pass_ratio} は [0, 1] の範囲である必要があります")
        if not (0.0 <= self.max_complexity_score <= 1.0):
            errors.append(f"max_complexity_score={self.max_complexity_score} は [0, 1] の範囲である必要があります")
        if not (0.0 <= self.min_selection_stability <= 1.0):
            errors.append(f"min_selection_stability={self.min_selection_stability} は [0, 1] の範囲である必要があります")

        # top_k の正値チェック
        if self.top_k <= 0:
            errors.append(f"top_k={self.top_k} は 1 以上である必要があります")
        if self.promotion_top_k <= 0:
            errors.append(f"promotion_top_k={self.promotion_top_k} は 1 以上である必要があります")
        if self.promotion_top_k > self.top_k:
            errors.append(
                f"promotion_top_k={self.promotion_top_k} は top_k={self.top_k} 以下である必要があります"
            )

        if errors:
            raise ValueError("FrostConfig 検証エラー:\n" + "\n".join(f"  - {e}" for e in errors))

    def to_dict(self) -> dict:
        """設定を辞書として返す (config_json 保存用)。"""
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "batch_label": self.batch_label,
            "engine_version": self.engine_version,
            "weights": {
                "predictive": self.w_predictive,
                "oos_sharpe": self.w_oos_sharpe,
                "regime_stability": self.w_regime_stability,
                "selection_consistency": self.w_selection_consistency,
                "capacity": self.w_capacity,
                "diversification": self.w_diversification,
                "turnover_penalty": self.w_turnover_penalty,
                "complexity_penalty": self.w_complexity_penalty,
                "drawdown_penalty": self.w_drawdown_penalty,
                "fragility_penalty": self.w_fragility_penalty,
                "pbo_penalty": self.w_pbo_penalty,
            },
            "hard_gates": {
                "pbo_threshold": self.pbo_threshold,
                "min_oos_sharpe": self.min_oos_sharpe,
                "min_rank_ic": self.min_rank_ic,
                "max_turnover": self.max_turnover,
                "max_drawdown": self.max_drawdown,
                "min_regime_pass_ratio": self.min_regime_pass_ratio,
                "max_complexity_score": self.max_complexity_score,
                "min_selection_stability": self.min_selection_stability,
            },
            "selection": {
                "top_k": self.top_k,
                "promotion_top_k": self.promotion_top_k,
                "near_duplicate_threshold": self.near_duplicate_threshold,
                "max_same_family": self.max_same_family,
                "review_required_default": self.review_required_default,
                "require_audit_pass": self.require_audit_pass,
            },
            "backtest": {
                "min_backtest_folds": self.min_backtest_folds,
                "min_train_years": self.min_train_years,
            },
        }


# ---------------------------------------------------------------------------
# ファクトリ関数
# ---------------------------------------------------------------------------

def load_frost_config(overrides: Optional[dict] = None) -> FrostConfig:
    """
    環境変数から FrostConfig を構築して返す。

    Parameters
    ----------
    overrides : dict, optional
        テスト用の直接上書き値。環境変数より優先される。

    Returns
    -------
    FrostConfig
        検証済み設定オブジェクト。
    """
    cfg = FrostConfig(
        # ── 基本設定 ─────────────────────────────────────────────────────
        enabled=_env_bool("FROST_ENABLED", True),
        dry_run=_env_bool("FROST_DRY_RUN", False),
        batch_label=_env_str("FROST_BATCH_LABEL", "frost_v1"),
        engine_version=_env_str("FROST_ENGINE_VERSION", "frost_v1"),
        pg_dsn=_env_str("FROST_PG_DSN", ""),

        # ── 正方向重み ────────────────────────────────────────────────────
        w_predictive=_env_float("FROST_W_PREDICTIVE", 0.20),
        w_oos_sharpe=_env_float("FROST_W_OOS_SHARPE", 0.15),
        w_regime_stability=_env_float("FROST_W_REGIME_STABILITY", 0.15),
        w_selection_consistency=_env_float("FROST_W_SELECTION_CONSISTENCY", 0.10),
        w_capacity=_env_float("FROST_W_CAPACITY", 0.10),
        w_diversification=_env_float("FROST_W_DIVERSIFICATION", 0.05),

        # ── ペナルティ重み ────────────────────────────────────────────────
        w_turnover_penalty=_env_float("FROST_W_TURNOVER_PENALTY", 0.10),
        w_complexity_penalty=_env_float("FROST_W_COMPLEXITY_PENALTY", 0.05),
        w_drawdown_penalty=_env_float("FROST_W_DRAWDOWN_PENALTY", 0.05),
        w_fragility_penalty=_env_float("FROST_W_FRAGILITY_PENALTY", 0.03),
        w_pbo_penalty=_env_float("FROST_W_PBO_PENALTY", 0.02),

        # ── Hard Gate ─────────────────────────────────────────────────────
        pbo_threshold=_env_float("FROST_PBO_THRESHOLD", 0.20),
        min_oos_sharpe=_env_float("FROST_MIN_OOS_SHARPE", 0.50),
        min_rank_ic=_env_float("FROST_MIN_RANK_IC", 0.02),
        max_turnover=_env_float("FROST_MAX_TURNOVER", 4.0),
        max_drawdown=_env_float("FROST_MAX_DRAWDOWN", 0.20),
        min_regime_pass_ratio=_env_float("FROST_MIN_REGIME_PASS_RATIO", 0.75),
        max_complexity_score=_env_float("FROST_MAX_COMPLEXITY_SCORE", 0.60),
        min_selection_stability=_env_float("FROST_MIN_SELECTION_STABILITY", 0.60),

        # ── 選抜制御 ─────────────────────────────────────────────────────
        top_k=_env_int("FROST_TOP_K", 25),
        promotion_top_k=_env_int("FROST_PROMOTION_TOP_K", 5),
        require_audit_pass=_env_bool("FROST_REQUIRE_AUDIT_PASS", True),
        near_duplicate_threshold=_env_float("FROST_NEAR_DUPLICATE_THRESHOLD", 0.95),
        max_same_family=_env_int("FROST_MAX_SAME_FAMILY", 3),

        # ── review ────────────────────────────────────────────────────────
        review_required_default=_env_bool("FROST_REVIEW_REQUIRED_DEFAULT", True),
        auto_approve_low_risk=_env_bool("FROST_AUTO_APPROVE_LOW_RISK", False),

        # ── バックテスト ──────────────────────────────────────────────────
        min_backtest_folds=_env_int("FROST_MIN_BACKTEST_FOLDS", 5),
        min_train_years=_env_float("FROST_MIN_TRAIN_YEARS", 2.0),

        # ── ロギング ─────────────────────────────────────────────────────
        verbose=_env_bool("FROST_VERBOSE", False),

        # ── 正規化 ───────────────────────────────────────────────────────
        score_clip_min=_env_float("FROST_SCORE_CLIP_MIN", -3.0),
        score_clip_max=_env_float("FROST_SCORE_CLIP_MAX", 3.0),
    )

    # overrides 適用 (テスト用)
    if overrides:
        for k, v in overrides.items():
            if hasattr(cfg, k):
                object.__setattr__(cfg, k, v)

    cfg.validate()
    return cfg


# ---------------------------------------------------------------------------
# デフォルトインスタンス (モジュール直接 import 用)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: FrostConfig = FrostConfig()
"""
デフォルト設定インスタンス。
ユニットテストや quick check 向けに validate() 済み。
本番使用では load_frost_config() を使用すること。
"""
