"""
eml_compiler.py
---------------
EML 木 → 実行可能な Python 式文字列へコンパイルする。

- compile_to_expr(node)  : EMLNode → Python 式文字列 (snapped weight ベース)
- validate_safe_expr(expr, terminals) : 式の安全検証
"""
from __future__ import annotations

import re
from typing import Set

from .eml_core import NODE_CONST, NODE_EML, NODE_TERMINAL, EMLNode


# ------------------------------------------------------------------ #
# コンパイル
# ------------------------------------------------------------------ #

def compile_to_expr(node: EMLNode) -> str:
    """
    EMLNode を Python 式文字列に変換する。
    snapped_weight が設定されている場合はそれを使用、未設定は raw_weight で判定。

    eml(a, b) =
      w >= 0.5 → a
      w <  0.5 → b
    """
    if node.kind == NODE_TERMINAL:
        return str(node.terminal_name)

    if node.kind == NODE_CONST:
        return "1.0"

    # NODE_EML
    import math

    def _sigmoid(x: float) -> float:
        return 1.0 / (1.0 + math.exp(-x))

    if node.snapped_weight is not None:
        w = node.snapped_weight
    else:
        w = _sigmoid(node.raw_weight)

    left_expr  = compile_to_expr(node.left)   # type: ignore[arg-type]
    right_expr = compile_to_expr(node.right)  # type: ignore[arg-type]

    if w >= 0.5:
        return left_expr
    else:
        return right_expr


def compile_to_full_expr(node: EMLNode) -> str:
    """
    snap なしで全分岐を明示した式文字列を生成。
    eml(a, b) → (a if w>=0.5 else b) 形式。
    デバッグ・audit 用。
    """
    import math

    def _sigmoid(x: float) -> float:
        return 1.0 / (1.0 + math.exp(-x))

    if node.kind == NODE_TERMINAL:
        return str(node.terminal_name)
    if node.kind == NODE_CONST:
        return "1.0"

    w = _sigmoid(node.raw_weight)
    left_expr  = compile_to_full_expr(node.left)   # type: ignore[arg-type]
    right_expr = compile_to_full_expr(node.right)  # type: ignore[arg-type]
    return f"eml({left_expr!r}, {right_expr!r}, w={w:.4f})"


# ------------------------------------------------------------------ #
# 安全検証
# ------------------------------------------------------------------ #

# 許可トークン: 英数字・アンダースコアのみ (terminal 名として想定)
_SAFE_TOKEN_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
# 定数として許可
_SAFE_CONST_RE = re.compile(r'^[0-9]+(\.[0-9]+)?$')


def validate_safe_expr(expr: str, terminals: Set[str]) -> bool:
    """
    式が terminal 名・数値定数のみで構成されているか検証。
    look-ahead bias や任意コード実行の防止。
    """
    token = expr.strip()
    if _SAFE_CONST_RE.match(token):
        return True
    if _SAFE_TOKEN_RE.match(token) and token in terminals:
        return True
    return False


def assert_safe_expr(expr: str, terminals: Set[str], label: str = "") -> None:
    """安全でない式は ValueError を送出。"""
    if not validate_safe_expr(expr, terminals):
        raise ValueError(
            f"Unsafe compiled expression: {expr!r} (label={label!r}). "
            f"Allowed terminals: {sorted(terminals)}"
        )
