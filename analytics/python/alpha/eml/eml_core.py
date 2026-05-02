"""
eml_core.py
-----------
EML (Evolved Machine Learning) の基本データ構造と定数。

文法:
  S -> 1 | t_i | eml(S, S)
  eml(a, b) = a if w_a >= 0.5 else b  (snap後)

depth 制約: 2 <= depth <= 4 (ハードガード)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any


def _safe_weight(v: float) -> float | None:
    """NaN / Inf を None に変換（JSON JSONB セーフ）。"""
    if v is None:
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None

# ハードガード: depth 範囲
EML_DEPTH_MIN = 2
EML_DEPTH_MAX = 4

# ノード種別
NODE_CONST    = "const"    # 定数 1
NODE_TERMINAL = "terminal" # terminal feature
NODE_EML      = "eml"      # eml(left, right)


@dataclass
class EMLNode:
    """EML 木のノード。"""
    kind: str                          # NODE_CONST / NODE_TERMINAL / NODE_EML
    terminal_name: str | None = None   # kind==terminal のとき
    const_value: float = 1.0           # kind==const のとき
    left: "EMLNode | None" = None      # kind==eml のとき
    right: "EMLNode | None" = None     # kind==eml のとき
    # soft weight: sigmoid(raw_w) → [0, 1]
    raw_weight: float = 0.0            # 学習パラメータ
    snapped_weight: float | None = None  # snap後 0 or 1

    def depth(self) -> int:
        if self.kind == NODE_EML:
            return 1 + max(self.left.depth(), self.right.depth())
        return 0

    def node_count(self) -> int:
        if self.kind == NODE_EML:
            return 1 + self.left.node_count() + self.right.node_count()
        return 1

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind}
        if self.kind == NODE_TERMINAL:
            d["terminal_name"] = self.terminal_name
        elif self.kind == NODE_CONST:
            d["const_value"] = self.const_value
        elif self.kind == NODE_EML:
            # NaN/Inf を None に変換 (PostgreSQL JSONB セーフ)
            d["raw_weight"]     = _safe_weight(self.raw_weight)
            d["snapped_weight"] = _safe_weight(self.snapped_weight)
            d["left"]  = self.left.to_dict()   # type: ignore[union-attr]
            d["right"] = self.right.to_dict()  # type: ignore[union-attr]
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EMLNode":
        kind = d["kind"]
        if kind == NODE_TERMINAL:
            return cls(kind=NODE_TERMINAL, terminal_name=d["terminal_name"])
        if kind == NODE_CONST:
            return cls(kind=NODE_CONST, const_value=d.get("const_value", 1.0))
        # NODE_EML
        return cls(
            kind=NODE_EML,
            raw_weight    = d.get("raw_weight", 0.0),
            snapped_weight = d.get("snapped_weight"),
            left  = cls.from_dict(d["left"]),
            right = cls.from_dict(d["right"]),
        )

    @classmethod
    def from_json(cls, s: str) -> "EMLNode":
        return cls.from_dict(json.loads(s))

    def terminals_used(self) -> set[str]:
        if self.kind == NODE_TERMINAL:
            return {self.terminal_name}  # type: ignore[return-value]
        if self.kind == NODE_EML:
            return self.left.terminals_used() | self.right.terminals_used()  # type: ignore
        return set()


def validate_depth(node: EMLNode, label: str = "") -> None:
    """depth 制約違反を例外で弾く（ハードガード）。"""
    d = node.depth()
    if d < EML_DEPTH_MIN or d > EML_DEPTH_MAX:
        raise ValueError(
            f"EML depth={d} は許可範囲 [{EML_DEPTH_MIN}, {EML_DEPTH_MAX}] 外です "
            f"({label}). depth 5 以上は禁止。"
        )
