"""
frost_known_factor_library.py
------------------------------
Crowding Detector で使用する既知因子ライブラリ定義。

各因子は「典型的なリターン特性のプロキシ」として
keyword ベースの因子ローディング辞書で表現される。

実運用では、バックテスト済み実際のリターン系列に差し替えることを推奨する。
本モジュールは Keyword ベースの軽量プロキシを提供する。

環境変数: FROST_CROWDING_ENABLED=1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 既知因子定義
# ---------------------------------------------------------------------------

@dataclass
class KnownFactor:
    """
    既知市場因子の定義。

    Attributes
    ----------
    factor_id : str
        因子 ID（例: "MOM_12M"）
    factor_name : str
        表示名
    factor_family : str
        因子グループ（"momentum" / "value" / "quality" 等）
    description : str
    keywords : list[str]
        この因子に関連するキーワード（formula_text マッチング用）
    typical_crowding_level : float
        典型的な crowding 水準（0=低, 1=高）
    """
    factor_id: str
    factor_name: str
    factor_family: str
    description: str
    keywords: List[str]
    typical_crowding_level: float = 0.5


# ---------------------------------------------------------------------------
# 既知因子ライブラリ（日本株 + 米国株 共通主要因子）
# ---------------------------------------------------------------------------

KNOWN_FACTOR_LIBRARY: List[KnownFactor] = [
    # ── Momentum ──────────────────────────────────────────────────────────
    KnownFactor(
        factor_id="MOM_12M",
        factor_name="12-Month Momentum",
        factor_family="momentum",
        description="過去12ヶ月リターン（最終1ヶ月除外）",
        keywords=["momentum", "12m", "return_12", "ts_returns", "trend"],
        typical_crowding_level=0.75,
    ),
    KnownFactor(
        factor_id="MOM_6M",
        factor_name="6-Month Momentum",
        factor_family="momentum",
        description="過去6ヶ月リターン",
        keywords=["momentum", "6m", "return_6", "roc6"],
        typical_crowding_level=0.70,
    ),
    KnownFactor(
        factor_id="STR_1M",
        factor_name="Short-term Reversal",
        factor_family="mean_reversion",
        description="過去1ヶ月リターンの逆張り",
        keywords=["reversal", "1m", "short_term", "mean_rev"],
        typical_crowding_level=0.55,
    ),
    # ── Value ────────────────────────────────────────────────────────────
    KnownFactor(
        factor_id="BM_RATIO",
        factor_name="Book-to-Market",
        factor_family="value",
        description="簿価時価比率（PBR の逆数）",
        keywords=["bm", "pb", "book_to_market", "pbr", "book_value"],
        typical_crowding_level=0.60,
    ),
    KnownFactor(
        factor_id="EP_RATIO",
        factor_name="Earnings Yield",
        factor_family="value",
        description="利益利回り（PER の逆数）",
        keywords=["ep", "pe", "earnings_yield", "earning", "per"],
        typical_crowding_level=0.60,
    ),
    KnownFactor(
        factor_id="CF_YIELD",
        factor_name="Cash Flow Yield",
        factor_family="value",
        description="キャッシュフロー利回り",
        keywords=["cf", "cashflow", "cash_flow", "fcf", "pcf"],
        typical_crowding_level=0.55,
    ),
    # ── Quality ──────────────────────────────────────────────────────────
    KnownFactor(
        factor_id="ROE",
        factor_name="Return on Equity",
        factor_family="quality",
        description="自己資本利益率",
        keywords=["roe", "return_equity", "equity_return", "profitability"],
        typical_crowding_level=0.65,
    ),
    KnownFactor(
        factor_id="ROA",
        factor_name="Return on Assets",
        factor_family="quality",
        description="総資産利益率",
        keywords=["roa", "return_assets", "asset_return"],
        typical_crowding_level=0.60,
    ),
    KnownFactor(
        factor_id="GROSS_MARGIN",
        factor_name="Gross Profit Margin",
        factor_family="quality",
        description="売上総利益率",
        keywords=["gross_margin", "margin", "profitability"],
        typical_crowding_level=0.55,
    ),
    # ── Size ─────────────────────────────────────────────────────────────
    KnownFactor(
        factor_id="SIZE_MKT_CAP",
        factor_name="Market Cap (Size)",
        factor_family="size",
        description="時価総額（小型株効果）",
        keywords=["market_cap", "size", "small_cap", "mktcap"],
        typical_crowding_level=0.50,
    ),
    # ── Low Vol ──────────────────────────────────────────────────────────
    KnownFactor(
        factor_id="LOW_VOL",
        factor_name="Low Volatility",
        factor_family="low_vol",
        description="低ボラティリティ効果",
        keywords=["low_vol", "min_vol", "volatility_inverse", "stability"],
        typical_crowding_level=0.70,
    ),
    # ── Flow ─────────────────────────────────────────────────────────────
    KnownFactor(
        factor_id="ORDER_IMBALANCE",
        factor_name="Order Flow Imbalance",
        factor_family="flow",
        description="注文フロー不均衡",
        keywords=["order_imbalance", "net_buy", "flow", "buy_sell"],
        typical_crowding_level=0.45,
    ),
    KnownFactor(
        factor_id="VOLUME_SURPRISE",
        factor_name="Volume Surprise",
        factor_family="flow",
        description="出来高のサプライズ（平均比）",
        keywords=["volume_surprise", "abnormal_volume", "volume_ratio"],
        typical_crowding_level=0.40,
    ),
    # ── Event ────────────────────────────────────────────────────────────
    KnownFactor(
        factor_id="EARNINGS_SURPRISE",
        factor_name="Earnings Surprise",
        factor_family="event",
        description="決算サプライズ（実績 vs 予想）",
        keywords=["earnings_surprise", "sue", "forecast_error", "beat"],
        typical_crowding_level=0.50,
    ),
    KnownFactor(
        factor_id="ANALYST_REVISION",
        factor_name="Analyst Forecast Revision",
        factor_family="event",
        description="アナリスト予想修正",
        keywords=["revision", "upgrade", "downgrade", "forecast_change"],
        typical_crowding_level=0.55,
    ),
    # ── Sentiment ────────────────────────────────────────────────────────
    KnownFactor(
        factor_id="SHORT_INTEREST",
        factor_name="Short Interest",
        factor_family="sentiment",
        description="空売り比率（逆張りシグナル）",
        keywords=["short_interest", "short_ratio", "put_call", "bearish"],
        typical_crowding_level=0.60,
    ),
]

# factor_id → KnownFactor のルックアップ
FACTOR_LOOKUP: Dict[str, KnownFactor] = {f.factor_id: f for f in KNOWN_FACTOR_LIBRARY}


# ---------------------------------------------------------------------------
# ファクターマッチング
# ---------------------------------------------------------------------------

def match_formula_to_factors(
    formula_text: str,
    min_match_score: float = 0.0,
) -> List[Tuple[str, float]]:
    """
    formula_text を既知因子ライブラリにキーワードマッチし、
    各因子とのマッチスコアを計算する。

    Parameters
    ----------
    formula_text : str
    min_match_score : float
        この値以上のスコアを持つ因子のみ返す

    Returns
    -------
    list[(factor_id, score)]  score 降順
    """
    text_lower = formula_text.lower()
    scores: List[Tuple[str, float]] = []

    for factor in KNOWN_FACTOR_LIBRARY:
        score = 0.0
        for kw in factor.keywords:
            if kw in text_lower:
                score += 1.0
        if score >= min_match_score:
            scores.append((factor.factor_id, score))

    return sorted(scores, key=lambda x: -x[1])


def get_factor_families() -> List[str]:
    """ライブラリ内の全因子ファミリーリストを返す（重複なし）。"""
    return sorted(set(f.factor_family for f in KNOWN_FACTOR_LIBRARY))
