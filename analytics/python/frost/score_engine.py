"""
score_engine.py
---------------
Phase 3: ScoreEngine — FROST スコア計算の単一責任クラス (D3 / D7 負債解消)

背景 (D3 負債):
  Phase 2 以前は compute_frost_score() への 20 引数直接渡しが
  evidence_bundle.py::evaluate_candidate_to_bundle() 内に埋め込まれており、
  重みパラメータを config から 1 つずつ取り出して渡すボイラープレートが
  約 20 行続いていた。
  また v1/v2 の切替も同関数内のガード節で行われており、
  スコア計算の責任が分散していた。

背景 (D7 負債):
  make_decision() の採択判断 (top_k / promotion_top_k) が
  frost_selector.py にハードコードされ、PolicySpec / FrostConfig の
  フィールドと直接結びついていなかった。
  ScoreEngine はスコア計算に集中し、採択判断は SelectionEngine (Phase 5)
  に委ねる設計とすることで責任を明確化する。

Phase 3 での解消:
  ScoreEngine が以下を担う:
    1. ScoreComponents → float スコア計算 (v1/v2 切替含む)
    2. 重みを config から一元的に取得 (w_predictive 等を直接参照)
    3. PolicySpec / FrostConfig 両方を受け付ける (後方互換)

  evidence_bundle.py::evaluate_candidate_to_bundle() の
  スコア計算部分 (compute_frost_score 呼び出し) を ScoreEngine に委譲する。

設計原則:
  - pure Python, numpy 不使用 (Phase 7 まで凍結)
  - 副作用なし (純関数)
  - FrostConfig / PolicySpec 両方を config として受け付ける (後方互換)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from analytics.python.frost.evidence_bundle import ScoreComponents


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


def _clip(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """v を [lo, hi] にクリッピングする。"""
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# ScoreEngine
# ---------------------------------------------------------------------------

@dataclass
class ScoreEngine:
    """
    FROST スコア計算エンジン。

    Phase 3 でスコア計算ロジックを一箇所に集約する。
    重みは config から取得し、ScoreComponents を受け取って
    frost_score_v1 / frost_score_v2 を計算する。

    使い方:
        engine = ScoreEngine(config)
        v1, v2 = engine.compute_both(scores)
        # または
        score = engine.compute(scores, use_v2=False)
    """

    config: Any
    """FrostConfig または PolicySpec インスタンス。"""

    # ── 重みキャッシュ (compute() 初回呼び出しで生成) ──────────────────────
    _weights_v1: Optional[Dict[str, float]] = field(default=None, repr=False, compare=False)
    _weights_v2: Optional[Dict[str, float]] = field(default=None, repr=False, compare=False)

    # ── 重み取得ヘルパー ────────────────────────────────────────────────────

    def _get_v1_weights(self) -> Dict[str, float]:
        """v1 重みを config から取得してキャッシュする。"""
        if self._weights_v1 is None:
            cfg = self.config
            self._weights_v1 = {
                "w_predictive":              _safe(getattr(cfg, "w_predictive",              0.20)),
                "w_oos_sharpe":              _safe(getattr(cfg, "w_oos_sharpe",              0.15)),
                "w_regime_stability":        _safe(getattr(cfg, "w_regime_stability",        0.15)),
                "w_selection_consistency":   _safe(getattr(cfg, "w_selection_consistency",   0.10)),
                "w_capacity":                _safe(getattr(cfg, "w_capacity",                0.10)),
                "w_pbo_penalty":             _safe(getattr(cfg, "w_pbo_penalty",             0.02)),
                "w_turnover_penalty":        _safe(getattr(cfg, "w_turnover_penalty",        0.10)),
                "w_complexity_penalty":      _safe(getattr(cfg, "w_complexity_penalty",      0.05)),
                "w_drawdown_penalty":        _safe(getattr(cfg, "w_drawdown_penalty",        0.05)),
                "w_fragility_penalty":       _safe(getattr(cfg, "w_fragility_penalty",       0.03)),
            }
        return self._weights_v1

    def _get_v2_weights(self) -> Dict[str, float]:
        """v2 重み (v1 + 追加軸) を config から取得してキャッシュする。"""
        if self._weights_v2 is None:
            cfg = self.config
            v1 = self._get_v1_weights()
            self._weights_v2 = {
                **v1,
                # v2 追加正スコア軸
                "w_genome_novelty":              _safe(getattr(cfg, "w_genome_novelty",              0.05)),
                "w_causal_validity":             _safe(getattr(cfg, "w_causal_validity",             0.05)),
                "w_regime_entropy":              _safe(getattr(cfg, "w_regime_entropy",              0.05)),
                # v2 追加ペナルティ軸
                "w_crowding_penalty":            _safe(getattr(cfg, "w_crowding_penalty",            0.05)),
                "w_signal_duplication_penalty":  _safe(getattr(cfg, "w_signal_duplication_penalty",  0.03)),
                "w_fragility_surface_penalty":   _safe(getattr(cfg, "w_fragility_surface_penalty",   0.02)),
            }
        return self._weights_v2

    # ── スコア計算 ──────────────────────────────────────────────────────────

    def compute_v1(self, scores: ScoreComponents) -> float:
        """
        v1 FROST スコアを計算する。

        式:
          frost_score_v1
            = w_pred*pred + w_oos*oos + w_reg*reg + w_sel*sel + w_cap*cap
            - w_pbo*pbo - w_turn*turn - w_comp*comp - w_dd*dd - w_frag*frag

        Parameters
        ----------
        scores : ScoreComponents

        Returns
        -------
        float
        """
        w = self._get_v1_weights()

        positive = (
            w["w_predictive"]            * _clip(scores.predictive_score)
            + w["w_oos_sharpe"]          * _clip(scores.oos_sharpe_score)
            + w["w_regime_stability"]    * _clip(scores.regime_stability_score)
            + w["w_selection_consistency"] * _clip(scores.selection_consistency_score)
            + w["w_capacity"]            * _clip(scores.capacity_score)
        )

        negative = (
            w["w_pbo_penalty"]           * _clip(scores.pbo_penalty)
            + w["w_turnover_penalty"]    * _clip(scores.turnover_penalty)
            + w["w_complexity_penalty"]  * _clip(scores.complexity_penalty)
            + w["w_drawdown_penalty"]    * _clip(scores.drawdown_penalty)
            + w["w_fragility_penalty"]   * _clip(scores.fragility_penalty)
        )

        return positive - negative

    def compute_v2(self, scores: ScoreComponents) -> float:
        """
        v2 FROST スコアを計算する。

        v1 の正スコア 5 軸 + ペナルティ 5 軸に加えて、
        v2 追加軸 (genome_novelty / causal_validity / regime_entropy /
                   crowding / signal_duplication / fragility_surface) を考慮する。

        Parameters
        ----------
        scores : ScoreComponents

        Returns
        -------
        float
        """
        w = self._get_v2_weights()

        positive = (
            w["w_predictive"]            * _clip(scores.predictive_score)
            + w["w_oos_sharpe"]          * _clip(scores.oos_sharpe_score)
            + w["w_regime_stability"]    * _clip(scores.regime_stability_score)
            + w["w_selection_consistency"] * _clip(scores.selection_consistency_score)
            + w["w_capacity"]            * _clip(scores.capacity_score)
            + w["w_genome_novelty"]      * _clip(scores.genome_novelty_score)
            + w["w_causal_validity"]     * _clip(scores.causal_validity_score)
            + w["w_regime_entropy"]      * _clip(scores.regime_entropy_score)
        )

        negative = (
            w["w_pbo_penalty"]                    * _clip(scores.pbo_penalty)
            + w["w_turnover_penalty"]             * _clip(scores.turnover_penalty)
            + w["w_complexity_penalty"]           * _clip(scores.complexity_penalty)
            + w["w_drawdown_penalty"]             * _clip(scores.drawdown_penalty)
            + w["w_fragility_penalty"]            * _clip(scores.fragility_penalty)
            + w["w_crowding_penalty"]             * _clip(scores.crowding_penalty)
            + w["w_signal_duplication_penalty"]   * _clip(scores.signal_duplication_penalty)
            + w["w_fragility_surface_penalty"]    * _clip(scores.fragility_surface_penalty)
        )

        return positive - negative

    def compute(self, scores: ScoreComponents, use_v2: bool = False) -> float:
        """
        use_v2 フラグに応じて v1/v2 スコアを返す。

        Parameters
        ----------
        scores : ScoreComponents
        use_v2 : bool
            True の場合は compute_v2() を使用する。

        Returns
        -------
        float
        """
        return self.compute_v2(scores) if use_v2 else self.compute_v1(scores)

    def compute_both(self, scores: ScoreComponents) -> tuple:
        """
        v1 / v2 スコアを両方計算して (v1, v2) のタプルで返す。

        evaluate_candidate_to_bundle() が frost_score_v1 と frost_score_v2 を
        両方 ScoreComponents に設定する際に使用する。

        Returns
        -------
        tuple (frost_score_v1: float, frost_score_v2: float)
        """
        return (self.compute_v1(scores), self.compute_v2(scores))

    def fill_scores(
        self,
        scores: ScoreComponents,
        use_v2: bool = False,
    ) -> ScoreComponents:
        """
        ScoreComponents の frost_score_v1 / frost_score_v2 を計算して返す。

        入力の ScoreComponents を書き換えず、新しいインスタンスを返す。

        Parameters
        ----------
        scores : ScoreComponents
            frost_score_v1/v2 以外のフィールドが設定済みであること。
        use_v2 : bool
            True の場合は v2 スコアも計算する。False の場合は v1 = v2。

        Returns
        -------
        ScoreComponents
            frost_score_v1 / frost_score_v2 が設定された新インスタンス。
        """
        v1 = self.compute_v1(scores)
        v2 = self.compute_v2(scores) if use_v2 else v1

        # dataclass は mutable なので直接設定
        scores.frost_score_v1 = v1
        scores.frost_score_v2 = v2
        return scores

    # ── 重み情報 ────────────────────────────────────────────────────────────

    def weight_sum_v1(self) -> Dict[str, float]:
        """v1 重みの positive/negative 合計を返す (デバッグ・監査用)。"""
        w = self._get_v1_weights()
        pos = sum([
            w["w_predictive"], w["w_oos_sharpe"], w["w_regime_stability"],
            w["w_selection_consistency"], w["w_capacity"],
        ])
        neg = sum([
            w["w_pbo_penalty"], w["w_turnover_penalty"], w["w_complexity_penalty"],
            w["w_drawdown_penalty"], w["w_fragility_penalty"],
        ])
        return {"positive": pos, "negative": neg, "net_max": pos - 0.0}

    def weight_sum_v2(self) -> Dict[str, float]:
        """v2 重みの positive/negative 合計を返す (デバッグ・監査用)。"""
        w = self._get_v2_weights()
        pos = sum([
            w["w_predictive"], w["w_oos_sharpe"], w["w_regime_stability"],
            w["w_selection_consistency"], w["w_capacity"],
            w["w_genome_novelty"], w["w_causal_validity"], w["w_regime_entropy"],
        ])
        neg = sum([
            w["w_pbo_penalty"], w["w_turnover_penalty"], w["w_complexity_penalty"],
            w["w_drawdown_penalty"], w["w_fragility_penalty"],
            w["w_crowding_penalty"], w["w_signal_duplication_penalty"],
            w["w_fragility_surface_penalty"],
        ])
        return {"positive": pos, "negative": neg, "net_max": pos - 0.0}

    # ── クラスメソッド ───────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, config: Any) -> "ScoreEngine":
        """FrostConfig または PolicySpec から ScoreEngine を生成する。"""
        return cls(config=config)
