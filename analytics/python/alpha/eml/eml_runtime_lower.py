"""
eml_runtime_lower.py
--------------------
コンパイル済み式 (terminal 名) を pandas Series 演算に lowering する。

- lower_expr(expr, feature_df) → pd.Series
- lower_expr はサポートされた terminal 名のみ参照可能 (look-ahead bias ガード)
"""
from __future__ import annotations

from typing import Set

import pandas as pd


# ------------------------------------------------------------------ #
# lowering
# ------------------------------------------------------------------ #

def lower_expr(
    expr: str,
    feature_df: pd.DataFrame,
    allowed_terminals: Set[str] | None = None,
) -> pd.Series:
    """
    コンパイル済み式 (単一 terminal 名 or 定数) を pd.Series に変換。

    expr が terminal 名 → feature_df[terminal_name] を返す
    expr が定数        → 定数値の Series を返す
    それ以外           → ValueError (safety ガード)

    Parameters
    ----------
    expr              : compile_to_expr() の出力
    feature_df        : terminal Series を列として持つ DataFrame
    allowed_terminals : None のとき feature_df.columns を使用
    """
    expr = expr.strip()

    if allowed_terminals is None:
        allowed_terminals = set(feature_df.columns.tolist())

    # 定数チェック
    try:
        val = float(expr)
        return pd.Series(val, index=feature_df.index, dtype=float)
    except ValueError:
        pass

    # terminal 参照
    if expr not in allowed_terminals:
        raise ValueError(
            f"lower_expr: '{expr}' は allowed_terminals に含まれていません。"
            f" allowed={sorted(allowed_terminals)[:10]}..."
        )

    if expr not in feature_df.columns:
        raise KeyError(
            f"lower_expr: terminal '{expr}' が feature_df に存在しません。"
            f" columns={list(feature_df.columns)[:10]}..."
        )

    return feature_df[expr].astype(float)


def lower_and_rank_normalize(
    expr: str,
    feature_df: pd.DataFrame,
    allowed_terminals: Set[str] | None = None,
) -> pd.Series:
    """
    lower_expr の結果を cross-sectional ランク正規化 [-0.5, +0.5] に変換。
    IC 計算等に使用。
    """
    s = lower_expr(expr, feature_df, allowed_terminals)
    ranked = s.rank(method="average", na_option="keep")
    n = ranked.notna().sum()
    if n <= 1:
        return s.fillna(0.0)
    normalized = (ranked - 1.0) / (n - 1.0) - 0.5
    return normalized
