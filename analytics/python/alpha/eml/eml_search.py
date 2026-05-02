"""
eml_search.py
-------------
EML アルファ候補探索エンジン。

2つの探索モード:
1. exhaustive_search : depth <= max_depth の全木を列挙して評価
2. gradient_search   : Adam soft-training → temperature annealing → snap

両モードとも EMLCandidate リストを返す。
"""
from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import pandas as pd

from .eml_core import (
    EML_DEPTH_MAX,
    EML_DEPTH_MIN,
    EMLNode,
    validate_depth,
)
from .eml_compiler import compile_to_expr
from .eml_tree import (
    enumerate_trees,
    random_tree,
    snap_weights,
    copy_tree,
)


# ------------------------------------------------------------------ #
# データ構造
# ------------------------------------------------------------------ #

@dataclass
class EMLCandidate:
    """探索で生成された候補アルファ。"""
    candidate_id: str
    run_id: str
    trace_id: str
    node: EMLNode
    compiled_expr: str          # compile_to_expr() 結果
    fitness_score: float = 0.0
    status: str = "candidate"   # candidate / promoted / rejected
    rejection_reason: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def tree_depth(self) -> int:
        return self.node.depth()

    def node_count(self) -> int:
        return self.node.node_count()


# ------------------------------------------------------------------ #
# 探索エンジン
# ------------------------------------------------------------------ #

FitnessFunc = Callable[[EMLNode, pd.DataFrame, pd.Series], float]


def exhaustive_search(
    terminals: List[str],
    max_depth: int,
    run_id: str,
    trace_id: str,
    feature_df: pd.DataFrame,
    target: pd.Series,
    fitness_fn: FitnessFunc,
    top_k: int = 20,
) -> List[EMLCandidate]:
    """
    depth <= max_depth の全 EML 木を列挙し、上位 top_k を返す。
    max_depth は EML_DEPTH_MAX(=4) にクランプ。
    """
    max_depth = min(max(max_depth, EML_DEPTH_MIN), EML_DEPTH_MAX)
    trees = enumerate_trees(max_depth, terminals)

    candidates: List[EMLCandidate] = []
    for tree in trees:
        try:
            validate_depth(tree, label="exhaustive_search")
        except ValueError:
            continue

        expr = compile_to_expr(snap_weights(tree))
        try:
            score = fitness_fn(tree, feature_df, target)
        except Exception:
            score = -999.0

        cid = str(uuid.uuid4())
        candidates.append(
            EMLCandidate(
                candidate_id=cid,
                run_id=run_id,
                trace_id=trace_id,
                node=tree,
                compiled_expr=expr,
                fitness_score=score,
                metadata={"search_mode": "exhaustive"},
            )
        )

    # 上位 top_k
    candidates.sort(key=lambda c: c.fitness_score, reverse=True)
    return candidates[:top_k]


def gradient_search(
    terminals: List[str],
    max_depth: int,
    run_id: str,
    trace_id: str,
    feature_df: pd.DataFrame,
    target: pd.Series,
    fitness_fn: FitnessFunc,
    n_init: int = 10,
    adam_steps: int = 50,
    lr: float = 0.1,
    temperature_start: float = 2.0,
    temperature_end: float = 0.1,
    top_k: int = 10,
    rng_seed: Optional[int] = None,
) -> List[EMLCandidate]:
    """
    勾配ベース探索 (Adam soft-training + temperature annealing + snap)。

    各初期木に対して:
      1. Adam で raw_weight を更新
      2. temperature annealing でソフト重みを収束
      3. snap → compile → fitness 評価
    """
    rng = random.Random(rng_seed)
    max_depth = min(max(max_depth, EML_DEPTH_MIN), EML_DEPTH_MAX)
    candidates: List[EMLCandidate] = []

    for i in range(n_init):
        tree = random_tree(max_depth, terminals, rng=rng)
        trained = _adam_train(
            tree, feature_df, target, fitness_fn,
            steps=adam_steps, lr=lr,
            temp_start=temperature_start, temp_end=temperature_end,
        )
        snapped = snap_weights(trained)

        try:
            validate_depth(snapped, label=f"gradient_search init={i}")
        except ValueError:
            continue

        expr = compile_to_expr(snapped)
        try:
            score = fitness_fn(snapped, feature_df, target)
        except Exception:
            score = -999.0

        cid = str(uuid.uuid4())
        candidates.append(
            EMLCandidate(
                candidate_id=cid,
                run_id=run_id,
                trace_id=trace_id,
                node=snapped,
                compiled_expr=expr,
                fitness_score=score,
                metadata={"search_mode": "gradient", "init_idx": i},
            )
        )

    candidates.sort(key=lambda c: c.fitness_score, reverse=True)
    return candidates[:top_k]


def _adam_train(
    node: EMLNode,
    feature_df: pd.DataFrame,
    target: pd.Series,
    fitness_fn: FitnessFunc,
    steps: int,
    lr: float,
    temp_start: float,
    temp_end: float,
    beta1: float = 0.9,
    beta2: float = 0.999,
    eps: float = 1e-8,
) -> EMLNode:
    """
    EML 木の raw_weight を Adam で更新。
    勾配は有限差分で近似。
    """
    node = copy_tree(node)
    eml_nodes = _collect_eml_nodes(node)
    if not eml_nodes:
        return node

    # Adam state
    m = [0.0] * len(eml_nodes)
    v = [0.0] * len(eml_nodes)

    for step in range(1, steps + 1):
        temp = temp_start * math.exp(
            math.log(temp_end / temp_start) * step / steps
        )
        delta = 0.01 * temp

        for idx, n in enumerate(eml_nodes):
            orig = n.raw_weight
            # f(w + delta)
            n.raw_weight = orig + delta
            score_p = _safe_fitness(fitness_fn, node, feature_df, target)
            # f(w - delta)
            n.raw_weight = orig - delta
            score_m = _safe_fitness(fitness_fn, node, feature_df, target)
            # 勾配: 最大化方向
            grad = (score_p - score_m) / (2 * delta)

            # Adam 更新
            m[idx] = beta1 * m[idx] + (1 - beta1) * grad
            v[idx] = beta2 * v[idx] + (1 - beta2) * grad ** 2
            m_hat = m[idx] / (1 - beta1 ** step)
            v_hat = v[idx] / (1 - beta2 ** step)

            n.raw_weight = orig + lr * m_hat / (math.sqrt(v_hat) + eps)

    return node


def _collect_eml_nodes(node: EMLNode) -> List[EMLNode]:
    """EML ノードを再帰収集。"""
    from .eml_core import NODE_EML
    if node.kind != NODE_EML:
        return []
    result = [node]
    if node.left:
        result += _collect_eml_nodes(node.left)
    if node.right:
        result += _collect_eml_nodes(node.right)
    return result


def _safe_fitness(
    fn: FitnessFunc,
    node: EMLNode,
    feature_df: pd.DataFrame,
    target: pd.Series,
) -> float:
    try:
        return float(fn(node, feature_df, target))
    except Exception:
        return -999.0
