"""
eml_tree.py
-----------
EML 木の生成・操作ユーティリティ。

- build_leaf / build_const / build_eml_node : 木ノード生成
- enumerate_trees(depth, terminals)         : depth 以下の全木を列挙
- random_tree(max_depth, terminals, rng)    : ランダム木生成
"""
from __future__ import annotations

import random
from typing import List, Optional

from .eml_core import (
    EML_DEPTH_MAX,
    EML_DEPTH_MIN,
    NODE_CONST,
    NODE_EML,
    NODE_TERMINAL,
    EMLNode,
    validate_depth,
)


# ------------------------------------------------------------------ #
# 基本ファクトリ
# ------------------------------------------------------------------ #

def build_leaf(terminal_name: str) -> EMLNode:
    return EMLNode(kind=NODE_TERMINAL, terminal_name=terminal_name)


def build_const() -> EMLNode:
    return EMLNode(kind=NODE_CONST, const_value=1.0)


def build_eml_node(left: EMLNode, right: EMLNode, raw_weight: float = 0.0) -> EMLNode:
    return EMLNode(kind=NODE_EML, left=left, right=right, raw_weight=raw_weight)


# ------------------------------------------------------------------ #
# 全木列挙 (exhaust enumerate, depth <= max_depth)
# ------------------------------------------------------------------ #

def _leaves(terminals: List[str]) -> List[EMLNode]:
    """depth=0 のリーフノード一覧。"""
    nodes: List[EMLNode] = [build_const()]
    for t in terminals:
        nodes.append(build_leaf(t))
    return nodes


def enumerate_trees(max_depth: int, terminals: List[str]) -> List[EMLNode]:
    """
    depth 1..max_depth の EML 木を全列挙。
    max_depth は EML_DEPTH_MAX 以下にクランプ。
    """
    max_depth = min(max_depth, EML_DEPTH_MAX)
    if max_depth < 1:
        return _leaves(terminals)

    # 列挙上限: ターミナル数 × 2^depth が爆発しないよう深さ2に制限
    # depth=3 以上は random_tree で代替する（exhaustive はdepth<=2 のみ）
    effective_depth = min(max_depth, 2)

    # dp[d] = depth == d のノードリスト (厳密に depth==d のみ)
    exact: List[List[EMLNode]] = [[] for _ in range(effective_depth + 1)]
    exact[0] = _leaves(terminals)

    for d in range(1, effective_depth + 1):
        # left が depth ld、right が depth rd で max(ld,rd)+1 == d
        for ld in range(d):
            for rd in range(d):
                if max(ld, rd) + 1 != d:
                    continue
                for left in exact[ld]:
                    for right in exact[rd]:
                        node = build_eml_node(left, right)
                        exact[d].append(node)

    # depth >= EML_DEPTH_MIN のもののみ収集
    result: List[EMLNode] = []
    for d in range(EML_DEPTH_MIN, effective_depth + 1):
        result.extend(exact[d])
    return result


# ------------------------------------------------------------------ #
# ランダム木生成
# ------------------------------------------------------------------ #

def random_tree(
    max_depth: int,
    terminals: List[str],
    rng: Optional[random.Random] = None,
    force_min_depth: bool = True,
) -> EMLNode:
    """
    ランダムな EML 木を生成。depth は [EML_DEPTH_MIN, max_depth] に収まる。

    force_min_depth=True のとき、depth < EML_DEPTH_MIN の場合は再試行。
    """
    if rng is None:
        rng = random.Random()
    max_depth = min(max(max_depth, EML_DEPTH_MIN), EML_DEPTH_MAX)

    for _ in range(100):
        node = _random_node(max_depth, terminals, rng)
        d = node.depth()
        if d >= EML_DEPTH_MIN and d <= EML_DEPTH_MAX:
            return node
        if not force_min_depth:
            return node

    # fallback: depth=EML_DEPTH_MIN の最小木
    t1 = rng.choice(terminals)
    t2 = rng.choice(terminals)
    node = build_eml_node(build_leaf(t1), build_leaf(t2))
    return node


def _random_node(max_depth: int, terminals: List[str], rng: random.Random) -> EMLNode:
    if max_depth == 0:
        # リーフのみ
        if rng.random() < 0.1:
            return build_const()
        return build_leaf(rng.choice(terminals))

    # eml ノードを生成する確率 (depth が深いほど低下)
    p_eml = 0.7 if max_depth >= 2 else 0.4
    if rng.random() < p_eml:
        left = _random_node(max_depth - 1, terminals, rng)
        right = _random_node(max_depth - 1, terminals, rng)
        return build_eml_node(left, right, raw_weight=rng.gauss(0, 1))
    else:
        if rng.random() < 0.1:
            return build_const()
        return build_leaf(rng.choice(terminals))


# ------------------------------------------------------------------ #
# 木のコピー・スナップ
# ------------------------------------------------------------------ #

def copy_tree(node: EMLNode) -> EMLNode:
    """深コピー。"""
    import copy
    return copy.deepcopy(node)


def snap_weights(node: EMLNode, threshold: float = 0.5) -> EMLNode:
    """
    raw_weight をスナップし snapped_weight を 0/1 に設定する。
    softmax weight > threshold → 1 (left 選択), else 0 (right 選択)。
    """
    import math

    def _sigmoid(x: float) -> float:
        return 1.0 / (1.0 + math.exp(-x))

    def _snap(n: EMLNode) -> EMLNode:
        if n.kind == NODE_EML:
            w = _sigmoid(n.raw_weight)
            n.snapped_weight = 1.0 if w >= threshold else 0.0
            _snap(n.left)   # type: ignore[arg-type]
            _snap(n.right)  # type: ignore[arg-type]
        return n

    return _snap(copy_tree(node))


def prune_snapped(node: EMLNode) -> EMLNode:
    """
    snapped_weight に従って木を刈り込む。
    snapped_weight==1 → left, ==0 → right を再帰的に選択。
    """
    if node.kind != NODE_EML:
        return node
    if node.snapped_weight is None:
        # snap 済みでない場合はそのまま
        return node
    if node.snapped_weight >= 0.5:
        return prune_snapped(node.left)   # type: ignore[arg-type]
    else:
        return prune_snapped(node.right)  # type: ignore[arg-type]
