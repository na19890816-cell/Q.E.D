"""
eml_lag_analyzer.py
--------------------
Formal Lag Analyzer — EML AST の各ノードに lag 情報を付与し、
ルックアヘッドバイアス（future leakage）を形式的に検出する。

主要関数
--------
analyze_lag(node, terminal_lag_map)
    EMLNode ツリーを再帰的に解析し、LagAnnotation を返す。

check_future_leakage(node, terminal_lag_map)
    ツリー全体に future_dependency_flag が立つノードがあれば True を返す。

build_safety_proof(node, terminal_lag_map, formula_text)
    LagSafetyProof dataclass を構築して返す。
    この proof を DB に保存することで no-lookahead を監査可能にする。

設計原則
--------
- terminal_lag_map: {terminal_name: min_required_lag}
  例: {"r1": 1, "r5": 5, "r20": 20, "gap": 1, "vol": 1}
  0 以下は「将来値を参照している = 危険」、1 以上は安全。
- EML ノードの lag = 子ノードの lag の min（最も危険な側に合わせる）
- CONST ノードは lag = +inf（常に安全）
- future_dependency_flag: min_required_lag <= 0 のとき True
- 環境変数 EML_FORMAL_LAG_ANALYZER_ENABLED=0 で全チェックをスキップ可能
  ただし EML_FUTURE_LEAKAGE_STRICT=1 のときは ENABLED に関わらず検査する
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .eml_core import EMLNode, NODE_CONST, NODE_EML, NODE_TERMINAL

# --------------------------------------------------------------------------- #
# 設定
# --------------------------------------------------------------------------- #

_ENABLED: bool = os.environ.get("EML_FORMAL_LAG_ANALYZER_ENABLED", "1") == "1"
_STRICT: bool  = os.environ.get("EML_FUTURE_LEAKAGE_STRICT", "1") == "1"

# デフォルト lag マップ（terminal が未登録の場合のフォールバック）
DEFAULT_TERMINAL_LAG_MAP: Dict[str, int] = {
    "r1":   1,
    "r5":   5,
    "r20":  20,
    "gap":  1,
    "vol":  1,
    "mom":  1,
    "rev":  1,
}

# +inf の代替（CONST は無限に安全）
_INF_LAG = 999_999


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #

@dataclass
class LagAnnotation:
    """1 ノードの lag 解析結果"""
    node_kind: str                      # 'terminal' | 'const' | 'eml'
    terminal_name: Optional[str]        # terminal の場合のみ
    min_required_lag: int               # このノード以下で必要な最小 lag
    max_required_lag: int               # このノード以下で必要な最大 lag
    future_dependency_flag: bool        # True = ルックアヘッドの疑いあり
    violations: List[str]               # 違反ターミナル名リスト
    children: List["LagAnnotation"] = field(default_factory=list)


@dataclass
class LagSafetyProof:
    """
    ツリー全体の no-lookahead 安全証明。
    DB (eml_alpha_candidates.safety_proof_json など) に保存して監査可能にする。
    """
    formula_text: str
    is_safe: bool                       # True = future leakage なし
    future_dependency_flag: bool        # True = ルックアヘッド検出
    min_lag_overall: int
    max_lag_overall: int
    violation_terminals: List[str]
    terminal_lag_map_used: Dict[str, int]
    annotation_summary: Dict[str, object]  # 軽量サマリー
    strict_mode: bool


# --------------------------------------------------------------------------- #
# コア解析
# --------------------------------------------------------------------------- #

def _get_terminal_lag(name: str, terminal_lag_map: Dict[str, int]) -> int:
    """
    terminal の登録済み lag を返す。
    未登録の場合は DEFAULT に照合し、それもなければ lag=1（安全側仮定）とする。
    ただし名前が "future_" で始まる場合は lag=0（危険）にする。
    """
    if name in terminal_lag_map:
        return terminal_lag_map[name]
    if name in DEFAULT_TERMINAL_LAG_MAP:
        return DEFAULT_TERMINAL_LAG_MAP[name]
    if name.startswith("future_"):
        return 0
    # 未知 terminal は保守的に lag=1 とする
    return 1


def analyze_lag(
    node: EMLNode,
    terminal_lag_map: Optional[Dict[str, int]] = None,
) -> LagAnnotation:
    """
    EMLNode ツリーを再帰的に解析し、LagAnnotation を構築する。

    Parameters
    ----------
    node : EMLNode
    terminal_lag_map : {terminal_name: min_required_lag}
        None の場合は DEFAULT_TERMINAL_LAG_MAP を使用。

    Returns
    -------
    LagAnnotation
    """
    if terminal_lag_map is None:
        terminal_lag_map = DEFAULT_TERMINAL_LAG_MAP

    if node.kind == NODE_CONST:
        return LagAnnotation(
            node_kind="const",
            terminal_name=None,
            min_required_lag=_INF_LAG,
            max_required_lag=_INF_LAG,
            future_dependency_flag=False,
            violations=[],
        )

    if node.kind == NODE_TERMINAL:
        lag = _get_terminal_lag(str(node.terminal_name), terminal_lag_map)
        future_flag = lag <= 0
        return LagAnnotation(
            node_kind="terminal",
            terminal_name=str(node.terminal_name),
            min_required_lag=lag,
            max_required_lag=lag,
            future_dependency_flag=future_flag,
            violations=[str(node.terminal_name)] if future_flag else [],
        )

    # NODE_EML — 子ノードを再帰解析
    left_ann  = analyze_lag(node.left,  terminal_lag_map)   # type: ignore[arg-type]
    right_ann = analyze_lag(node.right, terminal_lag_map)   # type: ignore[arg-type]

    min_lag = min(left_ann.min_required_lag, right_ann.min_required_lag)
    max_lag = max(
        left_ann.max_required_lag  if left_ann.max_required_lag  != _INF_LAG else 0,
        right_ann.max_required_lag if right_ann.max_required_lag != _INF_LAG else 0,
    )
    violations = left_ann.violations + right_ann.violations
    future_flag = bool(violations)

    return LagAnnotation(
        node_kind="eml",
        terminal_name=None,
        min_required_lag=min_lag,
        max_required_lag=max_lag,
        future_dependency_flag=future_flag,
        violations=violations,
        children=[left_ann, right_ann],
    )


# --------------------------------------------------------------------------- #
# ハイレベル API
# --------------------------------------------------------------------------- #

def check_future_leakage(
    node: EMLNode,
    terminal_lag_map: Optional[Dict[str, int]] = None,
) -> Tuple[bool, List[str]]:
    """
    ツリー全体にルックアヘッドがあれば (True, violation_list) を返す。
    EML_FUTURE_LEAKAGE_STRICT=1 または EML_FORMAL_LAG_ANALYZER_ENABLED=1 の場合に実行。
    どちらも 0 の場合は (False, []) を返す（スキップ）。

    Returns
    -------
    (future_leakage_detected: bool, violation_terminals: List[str])
    """
    if not (_ENABLED or _STRICT):
        return False, []

    ann = analyze_lag(node, terminal_lag_map)
    return ann.future_dependency_flag, ann.violations


def build_safety_proof(
    node: EMLNode,
    formula_text: str,
    terminal_lag_map: Optional[Dict[str, int]] = None,
) -> LagSafetyProof:
    """
    ツリー全体の安全証明を構築して返す。
    DB に JSON 保存することで no-lookahead を監査可能にする。

    Parameters
    ----------
    node : EMLNode
    formula_text : str  — compile_to_expr(node) の結果など
    terminal_lag_map : dict | None

    Returns
    -------
    LagSafetyProof
    """
    if terminal_lag_map is None:
        terminal_lag_map = DEFAULT_TERMINAL_LAG_MAP

    ann = analyze_lag(node, terminal_lag_map)

    is_safe = not ann.future_dependency_flag
    max_lag = ann.max_required_lag if ann.max_required_lag != _INF_LAG else 0

    return LagSafetyProof(
        formula_text=formula_text,
        is_safe=is_safe,
        future_dependency_flag=ann.future_dependency_flag,
        min_lag_overall=ann.min_required_lag if ann.min_required_lag != _INF_LAG else 0,
        max_lag_overall=max_lag,
        violation_terminals=list(set(ann.violations)),
        terminal_lag_map_used=dict(terminal_lag_map),
        annotation_summary={
            "node_kind": ann.node_kind,
            "min_required_lag": ann.min_required_lag if ann.min_required_lag != _INF_LAG else None,
            "max_required_lag": max_lag,
            "future_dependency_flag": ann.future_dependency_flag,
            "violation_count": len(ann.violations),
        },
        strict_mode=_STRICT,
    )


def proof_to_dict(proof: LagSafetyProof) -> dict:
    """LagSafetyProof を JSON 保存可能な dict に変換する。"""
    return {
        "formula_text": proof.formula_text,
        "is_safe": proof.is_safe,
        "future_dependency_flag": proof.future_dependency_flag,
        "min_lag_overall": proof.min_lag_overall,
        "max_lag_overall": proof.max_lag_overall,
        "violation_terminals": proof.violation_terminals,
        "terminal_lag_map_used": proof.terminal_lag_map_used,
        "annotation_summary": proof.annotation_summary,
        "strict_mode": proof.strict_mode,
    }
