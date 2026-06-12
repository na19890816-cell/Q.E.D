"""
alpha_genome_encoder.py
-----------------------
候補式（EML AST / formula_text）を因子DNA ベクトル（Genome Vector）に変換する。

Genome 軸（10次元）:
  1. Momentum         - トレンド追随・モメンタム系
  2. MeanReversion    - 平均回帰系
  3. Flow             - 資金フロー・注文フロー系
  4. Volatility       - ボラティリティ系
  5. Value            - バリュー系
  6. Event            - イベント系（決算・TOB等）
  7. Macro            - マクロ系（金利・FX等）
  8. Microstructure   - 板・VWAP等マイクロ構造
  9. Sentiment        - センチメント系
  10. CreditLeverage  - クレジット・レバレッジ系

設計原則:
  - pure Python（numpy不使用）
  - キーワードマッチング + 重み付きスコアリングによる簡易エンコーダ
  - より精緻な実装は将来の genome_v2 で行う
  - 環境変数: ALPHA_GENOME_ENABLED=1
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 定数・環境変数
# ---------------------------------------------------------------------------

_GENOME_ENABLED: bool = os.environ.get(
    "ALPHA_GENOME_ENABLED", "1"
).strip().lower() in ("1", "true", "yes", "on")

# Genome 軸の定義
GENOME_AXES = [
    "momentum",
    "mean_reversion",
    "flow",
    "volatility",
    "value",
    "event",
    "macro",
    "microstructure",
    "sentiment",
    "credit_leverage",
]

GENOME_DIM = len(GENOME_AXES)  # = 10


# ---------------------------------------------------------------------------
# キーワード辞書
# ---------------------------------------------------------------------------

# 各 Genome 軸に対応するキーワード（小文字）
# (keyword, weight) のリスト
_GENOME_KEYWORDS: Dict[str, List[Tuple[str, float]]] = {
    "momentum": [
        ("momentum", 2.0), ("trend", 1.5), ("return_", 1.0), ("roc", 1.5),
        ("ts_delta", 1.5), ("ts_returns", 2.0), ("price_change", 1.5),
        ("breakout", 1.0), ("drift", 1.0), ("12m", 1.2), ("6m", 1.0),
        ("3m", 0.8), ("ret_", 1.5), ("fwd_", 0.5),
    ],
    "mean_reversion": [
        ("zscore", 2.0), ("mean_rev", 2.0), ("reversal", 1.5), ("reversion", 1.5),
        ("deviation", 1.2), ("distance", 1.0), ("spread", 1.0),
        ("ts_rank", 1.5), ("percentile", 1.0), ("excess", 1.0),
        ("relative", 0.8), ("vs_", 0.8), ("norm_", 0.8),
    ],
    "flow": [
        ("volume", 1.5), ("turnover", 1.5), ("flow", 2.0), ("order", 1.5),
        ("imbalance", 2.0), ("buy", 1.2), ("sell", 1.2), ("net_buy", 2.0),
        ("large_order", 1.5), ("institutional", 1.5), ("vwap", 1.5),
        ("amihud", 1.5), ("kyle", 1.5), ("roll", 1.0),
    ],
    "volatility": [
        ("vol", 2.0), ("volatility", 2.0), ("variance", 1.5), ("std", 1.5),
        ("atr", 2.0), ("range_", 1.5), ("garch", 2.0), ("realized", 1.5),
        ("implied", 1.5), ("vix", 2.0), ("highlow", 1.5), ("parkinson", 1.5),
        ("garman", 1.5), ("beta", 1.0), ("skew", 1.2),
    ],
    "value": [
        ("value", 2.0), ("pb", 2.0), ("pe", 2.0), ("pcf", 1.5), ("ps", 1.5),
        ("book_", 1.5), ("earnings", 1.5), ("dividend", 1.5), ("yield", 1.5),
        ("fcf", 1.5), ("roe", 1.5), ("roa", 1.5), ("margin", 1.0),
        ("ev_", 1.5), ("enterprise", 1.0), ("fundamental", 1.5),
    ],
    "event": [
        ("event", 2.0), ("earnings_", 2.0), ("announcement", 2.0), ("shock", 1.5),
        ("news", 1.5), ("surprise", 2.0), ("guidance", 1.5), ("revision", 1.5),
        ("tob", 2.0), ("buyback", 1.5), ("split", 1.5), ("dividend_cut", 2.0),
        ("downgrade", 1.5), ("upgrade", 1.5), ("analyst", 1.0),
    ],
    "macro": [
        ("rate", 1.5), ("yield_", 1.5), ("fx", 2.0), ("currency", 1.5),
        ("gdp", 1.5), ("inflation", 1.5), ("cpi", 1.5), ("employment", 1.0),
        ("macro", 2.0), ("global", 1.0), ("usd", 1.5), ("jpy", 1.5),
        ("us10y", 2.0), ("tsy", 1.5), ("credit_spread", 1.5),
    ],
    "microstructure": [
        ("bid_ask", 2.0), ("spread_", 1.5), ("depth", 1.5), ("quote", 1.5),
        ("tick", 1.5), ("microstructure", 2.0), ("vwap", 1.5), ("twap", 1.5),
        ("market_impact", 2.0), ("resiliency", 1.5), ("adverse", 1.5),
        ("price_discovery", 1.5), ("execution", 1.0),
    ],
    "sentiment": [
        ("sentiment", 2.0), ("fear", 1.5), ("greed", 1.5), ("survey", 1.5),
        ("analyst_", 1.5), ("consensus", 1.5), ("short_", 1.5), ("put_call", 2.0),
        ("skew_", 1.5), ("net_long", 1.5), ("positioning", 1.5),
        ("bullish", 1.5), ("bearish", 1.5), ("retail_", 1.0),
    ],
    "credit_leverage": [
        ("credit", 2.0), ("leverage", 2.0), ("debt", 1.5), ("default", 1.5),
        ("cds", 2.0), ("hyg", 1.5), ("hy_", 1.5), ("ig_", 1.5), ("spread_credit", 2.0),
        ("margin_", 1.0), ("net_debt", 1.5), ("interest_cover", 1.5),
        ("financial_stress", 2.0), ("z_score_altman", 2.0),
    ],
}


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class GenomeVector:
    """
    候補式の因子 DNA ベクトル。

    Attributes
    ----------
    candidate_id : str
    formula_text : str
    vector : dict[str, float]
        axis名 → 重み（0〜1、合計は 1.0 に正規化）
    raw_scores : dict[str, float]
        正規化前のスコア
    dominant_axis : str
        最大重みの軸
    novelty_score : float
        ゲノム新規性スコア（後から外部設定、デフォルト 0.5）
    confidence : float
        エンコーダの信頼度（キーワードマッチ数に基づく 0〜1）
    """
    candidate_id: str
    formula_text: str
    vector: Dict[str, float]
    raw_scores: Dict[str, float]
    dominant_axis: str
    novelty_score: float = 0.5
    confidence: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "candidate_id": self.candidate_id,
            "formula_text": self.formula_text,
            "vector": self.vector,
            "raw_scores": self.raw_scores,
            "dominant_axis": self.dominant_axis,
            "novelty_score": self.novelty_score,
            "confidence": self.confidence,
        }

    def to_list(self) -> List[float]:
        """GENOME_AXES 順の数値リストとして返す（クラスタリング用）。"""
        return [self.vector.get(axis, 0.0) for axis in GENOME_AXES]


# ---------------------------------------------------------------------------
# エンコーダ
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    """formula_text を小文字トークンに分解する。"""
    text = text.lower()
    # アルファベット・数字・アンダースコアのみを残す
    tokens = re.findall(r"[a-z_][a-z0-9_]*", text)
    return tokens


def _score_axis(tokens: List[str], axis: str) -> float:
    """トークンリストと Genome 軸のマッチスコアを計算する。"""
    keywords = _GENOME_KEYWORDS.get(axis, [])
    score = 0.0
    for token in tokens:
        for kw, weight in keywords:
            if kw in token or token in kw:
                score += weight
    return score


def encode_formula(
    candidate_id: str,
    formula_text: str,
) -> GenomeVector:
    """
    formula_text から GenomeVector を生成する。

    Parameters
    ----------
    candidate_id : str
    formula_text : str

    Returns
    -------
    GenomeVector
    """
    tokens = _tokenize(formula_text)
    if not tokens:
        return _empty_genome(candidate_id, formula_text)

    # 各軸のスコア計算
    raw_scores: Dict[str, float] = {}
    for axis in GENOME_AXES:
        raw_scores[axis] = _score_axis(tokens, axis)

    total_score = sum(raw_scores.values())

    # 正規化（確率分布化）
    if total_score > 1e-10:
        vector = {axis: raw_scores[axis] / total_score for axis in GENOME_AXES}
    else:
        # マッチなし → 均等分布
        vector = {axis: 1.0 / GENOME_DIM for axis in GENOME_AXES}

    dominant_axis = max(vector, key=lambda k: vector[k])

    # confidence: マッチしたキーワード数 / トークン数
    match_count = sum(1 for axis in GENOME_AXES if raw_scores.get(axis, 0) > 0)
    confidence = min(1.0, match_count / max(1, GENOME_DIM / 2))

    return GenomeVector(
        candidate_id=candidate_id,
        formula_text=formula_text,
        vector=vector,
        raw_scores=raw_scores,
        dominant_axis=dominant_axis,
        novelty_score=0.5,  # 後から alpha_genome_similarity で更新
        confidence=confidence,
    )


def _empty_genome(candidate_id: str, formula_text: str) -> GenomeVector:
    """空のゲノムベクトル（キーワードマッチ不可の場合）。"""
    uniform = 1.0 / GENOME_DIM
    return GenomeVector(
        candidate_id=candidate_id,
        formula_text=formula_text,
        vector={axis: uniform for axis in GENOME_AXES},
        raw_scores={axis: 0.0 for axis in GENOME_AXES},
        dominant_axis=GENOME_AXES[0],
        novelty_score=0.5,
        confidence=0.0,
    )


def genome_cosine_similarity(g1: GenomeVector, g2: GenomeVector) -> float:
    """
    2つの GenomeVector のコサイン類似度を計算する（0〜1）。

    Returns
    -------
    float
        類似度（1.0 = 完全一致、0.0 = 完全直交）
    """
    v1 = g1.to_list()
    v2 = g2.to_list()

    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))

    if norm1 < 1e-15 or norm2 < 1e-15:
        return 0.0
    return min(1.0, dot / (norm1 * norm2))


def genome_l2_distance(g1: GenomeVector, g2: GenomeVector) -> float:
    """
    2つの GenomeVector の L2 距離を計算する（0〜√(GENOME_DIM)）。
    """
    v1 = g1.to_list()
    v2 = g2.to_list()
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(v1, v2)))
