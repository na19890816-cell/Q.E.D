"""
policy_spec.py
--------------
Phase 1: PolicySpec — 単一 PolicySpec に統合されたポリシー定義。

背景 (D1 / D10 負債解消):
  Phase 0 時点では「ポリシー」が 3 箇所に散在していた:
    D1: 50 超の環境変数 (FROST_W_*, FROST_MIN_*, ...)
    D1: FrostConfig dataclass (frost_config.py)
    D1: コード内ハードコード重み (frost_metrics.py, frost_selector.py)
    D10: frost_runs には config_json があるが policy_hash がなく
         「どのポリシーで実行したか」を機械的に照合できなかった

Phase 1 で解消する方法:
  1. PolicySpec に全パラメータを集約
  2. canonical_dict() → SHA-256 で policy_hash を生成
  3. to_dict() / from_dict() で完全シリアライズ/デシリアライズ
  4. from_frost_config() で既存 FrostConfig から変換 (後方互換)
  5. to_frost_config() で FrostConfig に戻す (後方互換)
  6. qed_policies テーブルへの upsert は postgres_policy_bridge.py で行う

設計原則:
  - pure Python, 標準ライブラリのみ (Phase 7 まで numpy 禁止)
  - dataclass frozen=True → ポリシーは不変値オブジェクト
  - policy_hash は "sha256:" プレフィックス付き 64 文字 hex
  - canonical_dict() はキーをソートして JSON を安定化
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


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
        raise ValueError(f"[policy_spec] {key}={v!r} は float に変換できません")


def _env_int(key: str, default: int) -> int:
    v = os.environ.get(key)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        raise ValueError(f"[policy_spec] {key}={v!r} は int に変換できません")


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# PolicySpec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PolicySpec:
    """
    FROST Meta-Fitness Engine の完全ポリシー仕様。

    frozen=True により不変値オブジェクト。
    policy_hash は to_dict() → canonical JSON → SHA-256 で導出される。

    全パラメータは以下の 5 セクションに整理:
      weights      : スコア軸の重み (正方向 + ペナルティ)
      hard_gates   : Hard Gate 閾値
      selection    : 選抜制御パラメータ
      backtest     : バックテスト品質制御
      meta         : エンジン識別・実験管理メタデータ
    """

    # ── [weights] 正方向スコア重み ────────────────────────────────────────
    w_predictive: float = 0.20
    w_oos_sharpe: float = 0.15
    w_regime_stability: float = 0.15
    w_selection_consistency: float = 0.10
    w_capacity: float = 0.10
    w_diversification: float = 0.05
    # v2 追加軸
    w_genome_novelty: float = 0.05
    w_causal_validity: float = 0.05
    w_regime_entropy: float = 0.05

    # ── [weights] ペナルティ重み ──────────────────────────────────────────
    w_pbo_penalty: float = 0.02
    w_turnover_penalty: float = 0.10
    w_complexity_penalty: float = 0.05
    w_drawdown_penalty: float = 0.05
    w_fragility_penalty: float = 0.03
    # v2 追加ペナルティ
    w_crowding_penalty: float = 0.05
    w_signal_duplication_penalty: float = 0.03
    w_fragility_surface_penalty: float = 0.02

    # ── [hard_gates] v1 ───────────────────────────────────────────────────
    pbo_threshold: float = 0.20
    min_oos_sharpe: float = 0.50
    min_rank_ic: float = 0.02
    max_turnover: float = 4.0
    max_drawdown: float = 0.20
    min_regime_pass_ratio: float = 0.75
    max_complexity_score: float = 0.60
    min_selection_stability: float = 0.60

    # ── [hard_gates] v2 追加閾値 ─────────────────────────────────────────
    min_causal_direction_score: float = 0.60
    min_invariance_pass_ratio: float = 0.70
    min_genome_novelty_score: float = 0.20
    max_crowding_r2: float = 0.80
    max_fsi: float = 0.40
    min_regime_entropy: float = 0.60
    max_signal_corr: float = 0.90

    # ── [selection] 選抜制御 ──────────────────────────────────────────────
    top_k: int = 25
    promotion_top_k: int = 5
    require_audit_pass: bool = True
    near_duplicate_threshold: float = 0.95
    max_same_family: int = 3
    review_required_default: bool = True
    auto_approve_low_risk: bool = False

    # ── [backtest] バックテスト品質 ───────────────────────────────────────
    min_backtest_folds: int = 5
    min_train_years: float = 2.0

    # ── [meta] エンジン識別・正規化・フラグ ─────────────────────────────
    engine_version: str = "frost_v1"
    batch_label: str = "frost_v1"
    use_v2_score: bool = False
    score_clip_min: float = -3.0
    score_clip_max: float = 3.0
    phase_tag: str = "phase1"
    description: str = ""

    # ── [meta] ロード時参照環境変数名リスト（値は含まない） ──────────────
    # frozen dataclass に list は使えないため tuple で保持
    source_env_vars: tuple = field(default_factory=tuple)

    # =========================================================================
    # シリアライズ
    # =========================================================================

    def to_dict(self) -> Dict[str, Any]:
        """
        PolicySpec を辞書に変換する。

        セクション構造:
          weights / hard_gates / selection / backtest / meta

        Returns
        -------
        dict
            完全なポリシー辞書。
        """
        return {
            "weights": {
                "predictive":                self.w_predictive,
                "oos_sharpe":                self.w_oos_sharpe,
                "regime_stability":          self.w_regime_stability,
                "selection_consistency":     self.w_selection_consistency,
                "capacity":                  self.w_capacity,
                "diversification":           self.w_diversification,
                "genome_novelty":            self.w_genome_novelty,
                "causal_validity":           self.w_causal_validity,
                "regime_entropy":            self.w_regime_entropy,
                "pbo_penalty":               self.w_pbo_penalty,
                "turnover_penalty":          self.w_turnover_penalty,
                "complexity_penalty":        self.w_complexity_penalty,
                "drawdown_penalty":          self.w_drawdown_penalty,
                "fragility_penalty":         self.w_fragility_penalty,
                "crowding_penalty":          self.w_crowding_penalty,
                "signal_duplication_penalty": self.w_signal_duplication_penalty,
                "fragility_surface_penalty": self.w_fragility_surface_penalty,
            },
            "hard_gates": {
                "pbo_threshold":             self.pbo_threshold,
                "min_oos_sharpe":            self.min_oos_sharpe,
                "min_rank_ic":               self.min_rank_ic,
                "max_turnover":              self.max_turnover,
                "max_drawdown":              self.max_drawdown,
                "min_regime_pass_ratio":     self.min_regime_pass_ratio,
                "max_complexity_score":      self.max_complexity_score,
                "min_selection_stability":   self.min_selection_stability,
                "min_causal_direction_score": self.min_causal_direction_score,
                "min_invariance_pass_ratio": self.min_invariance_pass_ratio,
                "min_genome_novelty_score":  self.min_genome_novelty_score,
                "max_crowding_r2":           self.max_crowding_r2,
                "max_fsi":                   self.max_fsi,
                "min_regime_entropy":        self.min_regime_entropy,
                "max_signal_corr":           self.max_signal_corr,
            },
            "selection": {
                "top_k":                     self.top_k,
                "promotion_top_k":           self.promotion_top_k,
                "require_audit_pass":        self.require_audit_pass,
                "near_duplicate_threshold":  self.near_duplicate_threshold,
                "max_same_family":           self.max_same_family,
                "review_required_default":   self.review_required_default,
                "auto_approve_low_risk":     self.auto_approve_low_risk,
            },
            "backtest": {
                "min_backtest_folds":        self.min_backtest_folds,
                "min_train_years":           self.min_train_years,
            },
            "meta": {
                "engine_version":            self.engine_version,
                "batch_label":               self.batch_label,
                "use_v2_score":              self.use_v2_score,
                "score_clip_min":            self.score_clip_min,
                "score_clip_max":            self.score_clip_max,
                "phase_tag":                 self.phase_tag,
                "description":               self.description,
                "source_env_vars":           list(self.source_env_vars),
            },
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PolicySpec":
        """
        to_dict() の逆操作。辞書から PolicySpec を復元する。

        Parameters
        ----------
        d : dict
            to_dict() 形式のネスト辞書。フラット辞書は非対応。

        Returns
        -------
        PolicySpec
        """
        w = d.get("weights", {})
        g = d.get("hard_gates", {})
        s = d.get("selection", {})
        b = d.get("backtest", {})
        m = d.get("meta", {})

        return cls(
            # weights
            w_predictive=               float(w.get("predictive",                0.20)),
            w_oos_sharpe=               float(w.get("oos_sharpe",                0.15)),
            w_regime_stability=         float(w.get("regime_stability",          0.15)),
            w_selection_consistency=    float(w.get("selection_consistency",     0.10)),
            w_capacity=                 float(w.get("capacity",                  0.10)),
            w_diversification=          float(w.get("diversification",           0.05)),
            w_genome_novelty=           float(w.get("genome_novelty",            0.05)),
            w_causal_validity=          float(w.get("causal_validity",           0.05)),
            w_regime_entropy=           float(w.get("regime_entropy",            0.05)),
            w_pbo_penalty=              float(w.get("pbo_penalty",               0.02)),
            w_turnover_penalty=         float(w.get("turnover_penalty",          0.10)),
            w_complexity_penalty=       float(w.get("complexity_penalty",        0.05)),
            w_drawdown_penalty=         float(w.get("drawdown_penalty",          0.05)),
            w_fragility_penalty=        float(w.get("fragility_penalty",         0.03)),
            w_crowding_penalty=         float(w.get("crowding_penalty",          0.05)),
            w_signal_duplication_penalty=float(w.get("signal_duplication_penalty",0.03)),
            w_fragility_surface_penalty=float(w.get("fragility_surface_penalty", 0.02)),
            # hard_gates
            pbo_threshold=              float(g.get("pbo_threshold",             0.20)),
            min_oos_sharpe=             float(g.get("min_oos_sharpe",            0.50)),
            min_rank_ic=                float(g.get("min_rank_ic",               0.02)),
            max_turnover=               float(g.get("max_turnover",              4.0 )),
            max_drawdown=               float(g.get("max_drawdown",              0.20)),
            min_regime_pass_ratio=      float(g.get("min_regime_pass_ratio",     0.75)),
            max_complexity_score=       float(g.get("max_complexity_score",      0.60)),
            min_selection_stability=    float(g.get("min_selection_stability",   0.60)),
            min_causal_direction_score= float(g.get("min_causal_direction_score",0.60)),
            min_invariance_pass_ratio=  float(g.get("min_invariance_pass_ratio", 0.70)),
            min_genome_novelty_score=   float(g.get("min_genome_novelty_score",  0.20)),
            max_crowding_r2=            float(g.get("max_crowding_r2",           0.80)),
            max_fsi=                    float(g.get("max_fsi",                   0.40)),
            min_regime_entropy=         float(g.get("min_regime_entropy",        0.60)),
            max_signal_corr=            float(g.get("max_signal_corr",           0.90)),
            # selection
            top_k=                      int(  s.get("top_k",                     25  )),
            promotion_top_k=            int(  s.get("promotion_top_k",           5   )),
            require_audit_pass=         bool( s.get("require_audit_pass",        True)),
            near_duplicate_threshold=   float(s.get("near_duplicate_threshold",  0.95)),
            max_same_family=            int(  s.get("max_same_family",           3   )),
            review_required_default=    bool( s.get("review_required_default",   True)),
            auto_approve_low_risk=      bool( s.get("auto_approve_low_risk",     False)),
            # backtest
            min_backtest_folds=         int(  b.get("min_backtest_folds",        5   )),
            min_train_years=            float(b.get("min_train_years",           2.0 )),
            # meta
            engine_version=             str(  m.get("engine_version",   "frost_v1")),
            batch_label=                str(  m.get("batch_label",       "frost_v1")),
            use_v2_score=               bool( m.get("use_v2_score",      False)),
            score_clip_min=             float(m.get("score_clip_min",    -3.0)),
            score_clip_max=             float(m.get("score_clip_max",     3.0)),
            phase_tag=                  str(  m.get("phase_tag",         "phase1")),
            description=                str(  m.get("description",       "")),
            source_env_vars=            tuple(m.get("source_env_vars",   [])),
        )

    # =========================================================================
    # ハッシュ計算
    # =========================================================================

    def canonical_dict(self) -> Dict[str, Any]:
        """
        policy_hash 計算に使う正規化辞書を返す。

        source_env_vars / description は実行環境依存の揮発情報のため
        ハッシュから除外する。これにより「同じパラメータで説明文だけ異なる
        ポリシー」は同一ハッシュとして扱われる。

        Returns
        -------
        dict
            キーがソートされた安定 JSON シリアライズ可能な辞書。
        """
        d = self.to_dict()
        # 揮発フィールドを除外
        meta = dict(d["meta"])
        meta.pop("description", None)
        meta.pop("source_env_vars", None)
        d = dict(d)
        d["meta"] = meta
        return d

    def compute_hash(self) -> str:
        """
        PolicySpec の SHA-256 ハッシュを計算して返す。

        Returns
        -------
        str
            "sha256:<64 hex chars>" 形式の文字列。
        """
        canonical = json.dumps(
            self.canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        hex_digest = hashlib.sha256(canonical).hexdigest()
        return f"sha256:{hex_digest}"

    @property
    def policy_hash(self) -> str:
        """compute_hash() のプロパティ糖衣。"""
        return self.compute_hash()

    # =========================================================================
    # バリデーション
    # =========================================================================

    def validate(self) -> None:
        """
        設定値の妥当性を検証する。問題があれば ValueError を送出。
        FrostConfig.validate() と同等の検証を実施。
        """
        errors: list = []

        weight_fields = [
            ("w_predictive",                self.w_predictive),
            ("w_oos_sharpe",                self.w_oos_sharpe),
            ("w_regime_stability",          self.w_regime_stability),
            ("w_selection_consistency",     self.w_selection_consistency),
            ("w_capacity",                  self.w_capacity),
            ("w_diversification",           self.w_diversification),
            ("w_genome_novelty",            self.w_genome_novelty),
            ("w_causal_validity",           self.w_causal_validity),
            ("w_regime_entropy",            self.w_regime_entropy),
            ("w_pbo_penalty",               self.w_pbo_penalty),
            ("w_turnover_penalty",          self.w_turnover_penalty),
            ("w_complexity_penalty",        self.w_complexity_penalty),
            ("w_drawdown_penalty",          self.w_drawdown_penalty),
            ("w_fragility_penalty",         self.w_fragility_penalty),
            ("w_crowding_penalty",          self.w_crowding_penalty),
            ("w_signal_duplication_penalty",self.w_signal_duplication_penalty),
            ("w_fragility_surface_penalty", self.w_fragility_surface_penalty),
        ]
        for name, val in weight_fields:
            if val < 0.0:
                errors.append(f"{name}={val} は 0 以上である必要があります")

        if not (0.0 <= self.pbo_threshold <= 1.0):
            errors.append(f"pbo_threshold={self.pbo_threshold} は [0,1] である必要があります")
        if not (0.0 <= self.max_drawdown <= 1.0):
            errors.append(f"max_drawdown={self.max_drawdown} は [0,1] である必要があります")
        if not (0.0 <= self.min_regime_pass_ratio <= 1.0):
            errors.append(f"min_regime_pass_ratio={self.min_regime_pass_ratio} は [0,1] である必要があります")
        if not (0.0 <= self.max_complexity_score <= 1.0):
            errors.append(f"max_complexity_score={self.max_complexity_score} は [0,1] である必要があります")
        if not (0.0 <= self.min_selection_stability <= 1.0):
            errors.append(f"min_selection_stability={self.min_selection_stability} は [0,1] である必要があります")

        if self.top_k <= 0:
            errors.append(f"top_k={self.top_k} は 1 以上である必要があります")
        if self.promotion_top_k <= 0:
            errors.append(f"promotion_top_k={self.promotion_top_k} は 1 以上である必要があります")
        if self.promotion_top_k > self.top_k:
            errors.append(
                f"promotion_top_k={self.promotion_top_k} は top_k={self.top_k} 以下である必要があります"
            )

        if errors:
            raise ValueError("PolicySpec 検証エラー:\n" + "\n".join(f"  - {e}" for e in errors))

    # =========================================================================
    # 重み集計ヘルパー
    # =========================================================================

    def positive_weight_sum(self) -> float:
        """正方向重みの合計（v1 軸のみ）。"""
        return (
            self.w_predictive
            + self.w_oos_sharpe
            + self.w_regime_stability
            + self.w_selection_consistency
            + self.w_capacity
            + self.w_diversification
        )

    def positive_weight_sum_v2(self) -> float:
        """正方向重みの合計（v2: genome/causal/entropy 追加）。"""
        return (
            self.positive_weight_sum()
            + self.w_genome_novelty
            + self.w_causal_validity
            + self.w_regime_entropy
        )

    def penalty_weight_sum(self) -> float:
        """ペナルティ重みの合計（v1 軸のみ）。"""
        return (
            self.w_pbo_penalty
            + self.w_turnover_penalty
            + self.w_complexity_penalty
            + self.w_drawdown_penalty
            + self.w_fragility_penalty
        )

    def penalty_weight_sum_v2(self) -> float:
        """ペナルティ重みの合計（v2: crowding/dedup/fsi 追加）。"""
        return (
            self.penalty_weight_sum()
            + self.w_crowding_penalty
            + self.w_signal_duplication_penalty
            + self.w_fragility_surface_penalty
        )

    # =========================================================================
    # __repr__ 補助
    # =========================================================================

    def short_repr(self) -> str:
        """ログ出力向け短縮表現。"""
        h = self.policy_hash[:16]  # sha256: + 先頭 9 chars
        return (
            f"PolicySpec({h}… engine={self.engine_version!r} "
            f"phase={self.phase_tag!r} v2={self.use_v2_score})"
        )


# ---------------------------------------------------------------------------
# FrostConfig ブリッジ
# ---------------------------------------------------------------------------

def policy_spec_from_frost_config(cfg: Any) -> PolicySpec:
    """
    既存 FrostConfig から PolicySpec を生成するブリッジ関数。

    後方互換性のため FrostConfig → PolicySpec 方向の変換を提供する。
    Phase 1 以降は PolicySpec を直接使用することを推奨する。

    Parameters
    ----------
    cfg : FrostConfig
        変換元の FrostConfig オブジェクト。

    Returns
    -------
    PolicySpec
        等価な PolicySpec。policy_hash は自動計算される。
    """
    return PolicySpec(
        # weights
        w_predictive=               cfg.w_predictive,
        w_oos_sharpe=               cfg.w_oos_sharpe,
        w_regime_stability=         cfg.w_regime_stability,
        w_selection_consistency=    cfg.w_selection_consistency,
        w_capacity=                 cfg.w_capacity,
        w_diversification=          cfg.w_diversification,
        w_genome_novelty=           cfg.w_genome_novelty,
        w_causal_validity=          cfg.w_causal_validity,
        w_regime_entropy=           cfg.w_regime_entropy,
        w_pbo_penalty=              cfg.w_pbo_penalty,
        w_turnover_penalty=         cfg.w_turnover_penalty,
        w_complexity_penalty=       cfg.w_complexity_penalty,
        w_drawdown_penalty=         cfg.w_drawdown_penalty,
        w_fragility_penalty=        cfg.w_fragility_penalty,
        w_crowding_penalty=         cfg.w_crowding_penalty,
        w_signal_duplication_penalty=cfg.w_signal_duplication_penalty,
        w_fragility_surface_penalty=cfg.w_fragility_surface_penalty,
        # hard_gates
        pbo_threshold=              cfg.pbo_threshold,
        min_oos_sharpe=             cfg.min_oos_sharpe,
        min_rank_ic=                cfg.min_rank_ic,
        max_turnover=               cfg.max_turnover,
        max_drawdown=               cfg.max_drawdown,
        min_regime_pass_ratio=      cfg.min_regime_pass_ratio,
        max_complexity_score=       cfg.max_complexity_score,
        min_selection_stability=    cfg.min_selection_stability,
        min_causal_direction_score= cfg.min_causal_direction_score,
        min_invariance_pass_ratio=  cfg.min_invariance_pass_ratio,
        min_genome_novelty_score=   cfg.min_genome_novelty_score,
        max_crowding_r2=            cfg.max_crowding_r2,
        max_fsi=                    cfg.max_fsi,
        min_regime_entropy=         cfg.min_regime_entropy,
        max_signal_corr=            cfg.max_signal_corr,
        # selection
        top_k=                      cfg.top_k,
        promotion_top_k=            cfg.promotion_top_k,
        require_audit_pass=         cfg.require_audit_pass,
        near_duplicate_threshold=   cfg.near_duplicate_threshold,
        max_same_family=            cfg.max_same_family,
        review_required_default=    cfg.review_required_default,
        auto_approve_low_risk=      cfg.auto_approve_low_risk,
        # backtest
        min_backtest_folds=         cfg.min_backtest_folds,
        min_train_years=            cfg.min_train_years,
        # meta
        engine_version=             cfg.engine_version,
        batch_label=                cfg.batch_label,
        use_v2_score=               cfg.use_v2_score,
        score_clip_min=             cfg.score_clip_min,
        score_clip_max=             cfg.score_clip_max,
        phase_tag=                  "phase1",
        description=                "",
        source_env_vars=            tuple(),
    )


def policy_spec_to_frost_config(spec: PolicySpec) -> Any:
    """
    PolicySpec から FrostConfig を生成するブリッジ関数。

    既存コードとの後方互換性のため PolicySpec → FrostConfig 方向の変換を提供する。

    Parameters
    ----------
    spec : PolicySpec
        変換元の PolicySpec。

    Returns
    -------
    FrostConfig
        等価な FrostConfig。validate() 済み。
    """
    from analytics.python.frost.frost_config import FrostConfig

    cfg = FrostConfig(
        # weights
        w_predictive=               spec.w_predictive,
        w_oos_sharpe=               spec.w_oos_sharpe,
        w_regime_stability=         spec.w_regime_stability,
        w_selection_consistency=    spec.w_selection_consistency,
        w_capacity=                 spec.w_capacity,
        w_diversification=          spec.w_diversification,
        w_genome_novelty=           spec.w_genome_novelty,
        w_causal_validity=          spec.w_causal_validity,
        w_regime_entropy=           spec.w_regime_entropy,
        w_pbo_penalty=              spec.w_pbo_penalty,
        w_turnover_penalty=         spec.w_turnover_penalty,
        w_complexity_penalty=       spec.w_complexity_penalty,
        w_drawdown_penalty=         spec.w_drawdown_penalty,
        w_fragility_penalty=        spec.w_fragility_penalty,
        w_crowding_penalty=         spec.w_crowding_penalty,
        w_signal_duplication_penalty=spec.w_signal_duplication_penalty,
        w_fragility_surface_penalty=spec.w_fragility_surface_penalty,
        # hard_gates
        pbo_threshold=              spec.pbo_threshold,
        min_oos_sharpe=             spec.min_oos_sharpe,
        min_rank_ic=                spec.min_rank_ic,
        max_turnover=               spec.max_turnover,
        max_drawdown=               spec.max_drawdown,
        min_regime_pass_ratio=      spec.min_regime_pass_ratio,
        max_complexity_score=       spec.max_complexity_score,
        min_selection_stability=    spec.min_selection_stability,
        min_causal_direction_score= spec.min_causal_direction_score,
        min_invariance_pass_ratio=  spec.min_invariance_pass_ratio,
        min_genome_novelty_score=   spec.min_genome_novelty_score,
        max_crowding_r2=            spec.max_crowding_r2,
        max_fsi=                    spec.max_fsi,
        min_regime_entropy=         spec.min_regime_entropy,
        max_signal_corr=            spec.max_signal_corr,
        # selection
        top_k=                      spec.top_k,
        promotion_top_k=            spec.promotion_top_k,
        require_audit_pass=         spec.require_audit_pass,
        near_duplicate_threshold=   spec.near_duplicate_threshold,
        max_same_family=            spec.max_same_family,
        review_required_default=    spec.review_required_default,
        auto_approve_low_risk=      spec.auto_approve_low_risk,
        # backtest
        min_backtest_folds=         spec.min_backtest_folds,
        min_train_years=            spec.min_train_years,
        # meta
        engine_version=             spec.engine_version,
        batch_label=                spec.batch_label,
        use_v2_score=               spec.use_v2_score,
        score_clip_min=             spec.score_clip_min,
        score_clip_max=             spec.score_clip_max,
    )
    cfg.validate()
    return cfg


# ---------------------------------------------------------------------------
# ファクトリ: 環境変数から PolicySpec を構築
# ---------------------------------------------------------------------------

# 環境変数名の完全リスト (source_env_vars として記録するため)
_POLICY_ENV_VARS: List[str] = [
    "FROST_W_PREDICTIVE", "FROST_W_OOS_SHARPE", "FROST_W_REGIME_STABILITY",
    "FROST_W_SELECTION_CONSISTENCY", "FROST_W_CAPACITY", "FROST_W_DIVERSIFICATION",
    "FROST_W_GENOME_NOVELTY", "FROST_W_CAUSAL_VALIDITY", "FROST_W_REGIME_ENTROPY",
    "FROST_W_PBO_PENALTY", "FROST_W_TURNOVER_PENALTY", "FROST_W_COMPLEXITY_PENALTY",
    "FROST_W_DRAWDOWN_PENALTY", "FROST_W_FRAGILITY_PENALTY",
    "FROST_W_CROWDING_PENALTY", "FROST_W_SIGNAL_DUPLICATION_PENALTY",
    "FROST_W_FRAGILITY_SURFACE_PENALTY",
    "FROST_PBO_THRESHOLD", "FROST_MIN_OOS_SHARPE", "FROST_MIN_RANK_IC",
    "FROST_MAX_TURNOVER", "FROST_MAX_DRAWDOWN", "FROST_MIN_REGIME_PASS_RATIO",
    "FROST_MAX_COMPLEXITY_SCORE", "FROST_MIN_SELECTION_STABILITY",
    "CAUSAL_DIRECTION_MIN_SCORE", "CAUSAL_INVARIANCE_MIN_PASS_RATIO",
    "ALPHA_GENOME_MIN_NOVELTY_SCORE", "FROST_CROWDING_R2_MAX",
    "FROST_FSI_MAX", "FROST_REGIME_ENTROPY_MIN", "FROST_SIGNAL_CORR_MAX",
    "FROST_TOP_K", "FROST_PROMOTION_TOP_K", "FROST_REQUIRE_AUDIT_PASS",
    "FROST_NEAR_DUPLICATE_THRESHOLD", "FROST_MAX_SAME_FAMILY",
    "FROST_REVIEW_REQUIRED_DEFAULT", "FROST_AUTO_APPROVE_LOW_RISK",
    "FROST_MIN_BACKTEST_FOLDS", "FROST_MIN_TRAIN_YEARS",
    "FROST_ENGINE_VERSION", "FROST_BATCH_LABEL", "FROST_USE_V2_SCORE",
    "FROST_SCORE_CLIP_MIN", "FROST_SCORE_CLIP_MAX",
]


def load_policy_spec(
    overrides: Optional[Dict[str, Any]] = None,
    phase_tag: str = "phase1",
    description: str = "",
) -> PolicySpec:
    """
    環境変数から PolicySpec を構築して返す。

    Parameters
    ----------
    overrides : dict, optional
        環境変数より優先する直接上書き値（テスト用）。
        PolicySpec のフィールド名をキーとする flat dict。
    phase_tag : str
        QED フェーズタグ（デフォルト "phase1"）。
    description : str
        人間可読メモ。

    Returns
    -------
    PolicySpec
        validate() 済みの PolicySpec。
    """
    # 参照した環境変数名を記録（値は含まない）
    used_envs = tuple(k for k in _POLICY_ENV_VARS if os.environ.get(k) is not None)

    spec = PolicySpec(
        # weights
        w_predictive=               _env_float("FROST_W_PREDICTIVE",               0.20),
        w_oos_sharpe=               _env_float("FROST_W_OOS_SHARPE",               0.15),
        w_regime_stability=         _env_float("FROST_W_REGIME_STABILITY",         0.15),
        w_selection_consistency=    _env_float("FROST_W_SELECTION_CONSISTENCY",    0.10),
        w_capacity=                 _env_float("FROST_W_CAPACITY",                 0.10),
        w_diversification=          _env_float("FROST_W_DIVERSIFICATION",          0.05),
        w_genome_novelty=           _env_float("FROST_W_GENOME_NOVELTY",           0.05),
        w_causal_validity=          _env_float("FROST_W_CAUSAL_VALIDITY",          0.05),
        w_regime_entropy=           _env_float("FROST_W_REGIME_ENTROPY",           0.05),
        w_pbo_penalty=              _env_float("FROST_W_PBO_PENALTY",              0.02),
        w_turnover_penalty=         _env_float("FROST_W_TURNOVER_PENALTY",         0.10),
        w_complexity_penalty=       _env_float("FROST_W_COMPLEXITY_PENALTY",       0.05),
        w_drawdown_penalty=         _env_float("FROST_W_DRAWDOWN_PENALTY",         0.05),
        w_fragility_penalty=        _env_float("FROST_W_FRAGILITY_PENALTY",        0.03),
        w_crowding_penalty=         _env_float("FROST_W_CROWDING_PENALTY",         0.05),
        w_signal_duplication_penalty=_env_float("FROST_W_SIGNAL_DUPLICATION_PENALTY",0.03),
        w_fragility_surface_penalty=_env_float("FROST_W_FRAGILITY_SURFACE_PENALTY",0.02),
        # hard_gates
        pbo_threshold=              _env_float("FROST_PBO_THRESHOLD",              0.20),
        min_oos_sharpe=             _env_float("FROST_MIN_OOS_SHARPE",             0.50),
        min_rank_ic=                _env_float("FROST_MIN_RANK_IC",                0.02),
        max_turnover=               _env_float("FROST_MAX_TURNOVER",               4.0 ),
        max_drawdown=               _env_float("FROST_MAX_DRAWDOWN",               0.20),
        min_regime_pass_ratio=      _env_float("FROST_MIN_REGIME_PASS_RATIO",      0.75),
        max_complexity_score=       _env_float("FROST_MAX_COMPLEXITY_SCORE",       0.60),
        min_selection_stability=    _env_float("FROST_MIN_SELECTION_STABILITY",    0.60),
        min_causal_direction_score= _env_float("CAUSAL_DIRECTION_MIN_SCORE",       0.60),
        min_invariance_pass_ratio=  _env_float("CAUSAL_INVARIANCE_MIN_PASS_RATIO", 0.70),
        min_genome_novelty_score=   _env_float("ALPHA_GENOME_MIN_NOVELTY_SCORE",   0.20),
        max_crowding_r2=            _env_float("FROST_CROWDING_R2_MAX",            0.80),
        max_fsi=                    _env_float("FROST_FSI_MAX",                    0.40),
        min_regime_entropy=         _env_float("FROST_REGIME_ENTROPY_MIN",         0.60),
        max_signal_corr=            _env_float("FROST_SIGNAL_CORR_MAX",            0.90),
        # selection
        top_k=                      _env_int(  "FROST_TOP_K",                      25  ),
        promotion_top_k=            _env_int(  "FROST_PROMOTION_TOP_K",            5   ),
        require_audit_pass=         _env_bool( "FROST_REQUIRE_AUDIT_PASS",         True),
        near_duplicate_threshold=   _env_float("FROST_NEAR_DUPLICATE_THRESHOLD",   0.95),
        max_same_family=            _env_int(  "FROST_MAX_SAME_FAMILY",            3   ),
        review_required_default=    _env_bool( "FROST_REVIEW_REQUIRED_DEFAULT",    True),
        auto_approve_low_risk=      _env_bool( "FROST_AUTO_APPROVE_LOW_RISK",      False),
        # backtest
        min_backtest_folds=         _env_int(  "FROST_MIN_BACKTEST_FOLDS",         5   ),
        min_train_years=            _env_float("FROST_MIN_TRAIN_YEARS",            2.0 ),
        # meta
        engine_version=             _env_str(  "FROST_ENGINE_VERSION",  "frost_v1"),
        batch_label=                _env_str(  "FROST_BATCH_LABEL",      "frost_v1"),
        use_v2_score=               _env_bool( "FROST_USE_V2_SCORE",     False),
        score_clip_min=             _env_float("FROST_SCORE_CLIP_MIN",   -3.0),
        score_clip_max=             _env_float("FROST_SCORE_CLIP_MAX",    3.0),
        phase_tag=                  phase_tag,
        description=                description,
        source_env_vars=            used_envs,
    )

    # overrides 適用 (frozen=True のため object.__setattr__ は使えない → 再構築)
    if overrides:
        d = spec.to_dict()
        # flat overrides を to_dict 構造に展開してマージ
        flat_fields = {f.name: getattr(spec, f.name) for f in spec.__dataclass_fields__.values()}
        flat_fields.update(overrides)
        spec = PolicySpec(**{
            k: flat_fields[k]
            for k in flat_fields
            if k in spec.__dataclass_fields__
        })

    spec.validate()
    return spec
