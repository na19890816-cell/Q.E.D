"""
eml_master_formula.py
---------------------
EML alpha discovery の全フローを統括するマスター関数。

run_eml_discovery(config) を呼ぶだけで:
  1. terminal set 読み込み
  2. exhaustive + gradient 探索
  3. 評価 (EMLEvaluationRunner)
  4. フィットネスによる選別
  5. EMLCandidate リストを返す

DB 書き込みは io 層に委譲。
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import List, Optional, Set

import pandas as pd

from .eml_core import EML_DEPTH_MAX, EML_DEPTH_MIN
from .eml_search import (
    EMLCandidate,
    exhaustive_search,
    gradient_search,
)
from .eml_fitness import compute_fitness, simple_rank_ic_fitness
from .eml_evaluation_runner import EMLEvaluationRunner, EMLEvaluationResult
from .eml_compiler import assert_safe_expr
from .eml_tree import snap_weights


# ------------------------------------------------------------------ #
# 設定
# ------------------------------------------------------------------ #

@dataclass
class EMLDiscoveryConfig:
    """EML discovery 実行設定。環境変数からデフォルト値を取得。"""
    run_id: str                     = field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: str                   = field(default_factory=lambda: str(uuid.uuid4()))
    batch_label: str                = field(default_factory=lambda: os.environ.get("EML_BATCH_LABEL", "eml_v1"))
    target_horizon: str             = field(default_factory=lambda: os.environ.get("EML_ALPHA_TARGET_HORIZON", "5d"))
    max_depth: int                  = field(default_factory=lambda: int(os.environ.get("EML_ALPHA_MAX_DEPTH", "3")))
    terminal_set: List[str]         = field(default_factory=list)

    # 探索モード
    use_exhaustive: bool            = True
    use_gradient: bool              = True
    exhaustive_max_depth: int       = 2   # 列挙は depth<=2 に制限
    gradient_n_init: int            = field(default_factory=lambda: int(os.environ.get("EML_GRADIENT_N_INIT", "10")))
    gradient_steps: int             = field(default_factory=lambda: int(os.environ.get("EML_GRADIENT_STEPS", "50")))
    top_k_exhaustive: int           = 20
    top_k_gradient: int             = 10

    # フィットネス閾値
    min_fitness_for_promotion: float = field(
        default_factory=lambda: float(os.environ.get("EML_MIN_FITNESS_PROMOTION", "0.05"))
    )
    min_rank_ic: float              = field(
        default_factory=lambda: float(os.environ.get("EML_MIN_RANK_IC", "0.03"))
    )

    # コスト
    cost_bps: float                 = field(
        default_factory=lambda: float(os.environ.get("EML_BACKTEST_COST_BPS", "2.0"))
    )

    # 乱数 seed(golden run の決定論性確保用。None = 非固定)
    # 環境変数 EML_SEED が設定されている場合はその値を使用する
    rng_seed: Optional[int]         = field(
        default_factory=lambda: (
            int(os.environ["EML_SEED"])
            if os.environ.get("EML_SEED") else None
        )
    )


# ------------------------------------------------------------------ #
# マスター実行
# ------------------------------------------------------------------ #

@dataclass
class EMLDiscoveryOutput:
    run_id: str
    trace_id: str
    batch_label: str
    candidates: List[EMLCandidate]
    eval_results: List[EMLEvaluationResult]
    promoted: List[EMLCandidate]
    rejected: List[EMLCandidate]
    total_searched: int
    terminal_set_hash: str


def run_eml_discovery(
    config: EMLDiscoveryConfig,
    feature_df: pd.DataFrame,
    target: pd.Series,
    regime_mask: Optional[pd.Series] = None,
    event_mask: Optional[pd.Series] = None,
) -> EMLDiscoveryOutput:
    """
    EML alpha discovery フル実行。

    Parameters
    ----------
    config      : EMLDiscoveryConfig
    feature_df  : terminal feature DataFrame (row=date/bar, col=terminal_name)
    target      : 予測対象リターン Series
    regime_mask : crisis regime マスク (True = crisis)
    event_mask  : event window マスク (True = event)
    """
    import hashlib
    import json

    terminals = config.terminal_set
    if not terminals:
        raise ValueError("terminal_set が空です。EML_ALPHA_TERMINAL_SET 等で設定してください。")

    # terminal set ハッシュ
    ts_hash = hashlib.md5(
        json.dumps(sorted(terminals)).encode()
    ).hexdigest()[:16]

    # depth ガード
    max_depth = min(max(config.max_depth, EML_DEPTH_MIN), EML_DEPTH_MAX)

    # ------------------------------------------------------------------ #
    # 探索
    # ------------------------------------------------------------------ #
    candidates: List[EMLCandidate] = []

    if config.use_exhaustive:
        ex_depth = min(config.exhaustive_max_depth, max_depth)
        ex_cands = exhaustive_search(
            terminals=terminals,
            max_depth=ex_depth,
            run_id=config.run_id,
            trace_id=config.trace_id,
            feature_df=feature_df,
            target=target,
            fitness_fn=simple_rank_ic_fitness,
            top_k=config.top_k_exhaustive,
        )
        candidates.extend(ex_cands)

    if config.use_gradient:
        gr_cands = gradient_search(
            terminals=terminals,
            max_depth=max_depth,
            run_id=config.run_id,
            trace_id=config.trace_id,
            feature_df=feature_df,
            target=target,
            fitness_fn=simple_rank_ic_fitness,
            n_init=config.gradient_n_init,
            adam_steps=config.gradient_steps,
            top_k=config.top_k_gradient,
            rng_seed=config.rng_seed,  # golden run の決定論性確保
        )
        candidates.extend(gr_cands)

    total_searched = len(candidates)

    # ------------------------------------------------------------------ #
    # 重複除去 (compiled_expr が同一のものは最高スコアを残す)
    # ------------------------------------------------------------------ #
    seen: dict[str, EMLCandidate] = {}
    for c in candidates:
        if c.compiled_expr not in seen or c.fitness_score > seen[c.compiled_expr].fitness_score:
            seen[c.compiled_expr] = c
    candidates = list(seen.values())

    # ------------------------------------------------------------------ #
    # 安全式検証 (look-ahead bias ガード)
    # ------------------------------------------------------------------ #
    terminal_set: Set[str] = set(terminals)
    safe_candidates: List[EMLCandidate] = []
    for c in candidates:
        try:
            assert_safe_expr(c.compiled_expr, terminal_set, label=c.candidate_id)
            safe_candidates.append(c)
        except ValueError as e:
            c.status = "rejected"
            c.rejection_reason = f"UNSAFE_EXPR: {e}"
    candidates = safe_candidates

    # ------------------------------------------------------------------ #
    # 評価 (5 指標グループ)
    # ------------------------------------------------------------------ #
    runner = EMLEvaluationRunner(
        feature_df=feature_df,
        target=target,
        regime_mask=regime_mask,
        event_mask=event_mask,
        cost_bps=config.cost_bps,
        horizon=config.target_horizon,
    )

    eval_results: List[EMLEvaluationResult] = []
    for c in candidates:
        ev = runner.run(c)
        eval_results.append(ev)

        # promotion fitness 再計算 (全指標使用)
        full_fitness = compute_fitness(
            c.node, feature_df, target, regime_mask, config.cost_bps
        )
        c.fitness_score = full_fitness
        c.metadata["rank_ic"]              = ev.rank_ic
        c.metadata["cost_adjusted_sharpe"] = ev.sharpe
        c.metadata["regime_consistency"]   = ev.regime_consistency_score

    # ------------------------------------------------------------------ #
    # フィットネス閾値による促進/却下
    # ------------------------------------------------------------------ #
    promoted: List[EMLCandidate] = []
    rejected: List[EMLCandidate] = []

    for c in candidates:
        ev = next((e for e in eval_results if e.candidate_id == c.candidate_id), None)
        rank_ic = ev.rank_ic if ev else 0.0

        if (
            c.fitness_score >= config.min_fitness_for_promotion
            and rank_ic >= config.min_rank_ic
        ):
            c.status = "promoted"
            promoted.append(c)
        else:
            c.status = "rejected"
            reason_parts = []
            if c.fitness_score < config.min_fitness_for_promotion:
                reason_parts.append(
                    f"fitness={c.fitness_score:.4f} < {config.min_fitness_for_promotion}"
                )
            if rank_ic < config.min_rank_ic:
                reason_parts.append(
                    f"rank_ic={rank_ic:.4f} < {config.min_rank_ic}"
                )
            c.rejection_reason = "; ".join(reason_parts) or "BELOW_THRESHOLD"
            rejected.append(c)

    return EMLDiscoveryOutput(
        run_id=config.run_id,
        trace_id=config.trace_id,
        batch_label=config.batch_label,
        candidates=candidates,
        eval_results=eval_results,
        promoted=promoted,
        rejected=rejected,
        total_searched=total_searched,
        terminal_set_hash=ts_hash,
    )
