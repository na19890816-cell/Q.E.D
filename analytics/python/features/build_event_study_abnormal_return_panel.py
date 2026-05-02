"""
build_event_study_abnormal_return_panel.py
-------------------------------------------
DuckDB を使った abnormal return / CAR panel 生成スクリプト。

想定 Parquet スキーマ (入力):
  date DATE, ticker TEXT, close NUMERIC, benchmark_close NUMERIC

出力 DataFrame 列:
  benchmark_id TEXT, event_date DATE, event_offset INT,
  abnormal_return NUMERIC, car_from_t0 NUMERIC,
  normal_return NUMERIC, actual_return NUMERIC, n_events INT

使い方:
  from build_event_study_abnormal_return_panel import build_panel
  df = build_panel(parquet_path="data/prices.parquet", event_dates=["2026-01-10"])
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def build_panel(
    parquet_path: str | None = None,
    event_dates: list[str] | None = None,
    window_before: int = 10,
    window_after: int = 20,
    benchmark_col: str = "benchmark_close",
    price_col: str = "close",
    ticker_col: str = "ticker",
    date_col: str = "date",
) -> Any:
    """
    DuckDB で abnormal return / CAR panel を構築し pandas DataFrame で返す。

    Parameters
    ----------
    parquet_path  : Parquet ファイルパス (未指定なら ENV: EVENT_AR_OUTPUT_PATH)
    event_dates   : イベント日付リスト (ISO 8601)
    window_before : イベント前ウィンドウ日数
    window_after  : イベント後ウィンドウ日数
    """
    try:
        import duckdb
    except ImportError:
        logger.warning("duckdb 未インストール → サンプルデータを使用 (pip install duckdb で有効化)")
        return _build_sample_panel()

    path = parquet_path or os.environ.get("EVENT_AR_OUTPUT_PATH", "")
    if not path or not Path(path).exists():
        logger.warning("Parquet ファイルが見つかりません: %s → サンプルデータを使用", path)
        return _build_sample_panel()

    if event_dates is None:
        event_dates = []

    con = duckdb.connect()

    # ---- ASOF JOIN で benchmark price をマージ ----
    # prices_raw: ticker 別日次終値
    # benchmark: 基準インデックス終値
    sql = f"""
    WITH prices AS (
        SELECT
            {ticker_col}                        AS ticker,
            CAST({date_col} AS DATE)            AS price_date,
            CAST({price_col} AS DOUBLE)         AS close_price,
            CAST({benchmark_col} AS DOUBLE)     AS bm_close
        FROM read_parquet('{path}')
    ),
    -- イベント日付テーブル (インラインで生成)
    events AS (
        SELECT
            ticker,
            CAST(event_dt AS DATE) AS event_date
        FROM (
            SELECT ticker, UNNEST(
                ARRAY[{','.join(repr(d) for d in event_dates) or "'2026-01-01'"}]::DATE[]
            ) AS event_dt
            FROM (SELECT DISTINCT ticker FROM prices)
        )
    ),
    -- イベントウィンドウ展開
    windows AS (
        SELECT
            e.ticker,
            e.event_date,
            CAST(gs.offset AS INT) AS event_offset,
            e.event_date + CAST(gs.offset AS INT) AS target_date
        FROM events e
        CROSS JOIN generate_series({-window_before}, {window_after}) AS gs(offset)
    ),
    -- ASOF JOIN で price を補間
    joined AS (
        SELECT
            w.ticker,
            w.event_date,
            w.event_offset,
            w.target_date,
            p.close_price,
            p.bm_close
        FROM windows w
        ASOF JOIN prices p
          ON p.ticker = w.ticker AND p.price_date <= w.target_date
    ),
    -- リターン計算
    returns AS (
        SELECT
            ticker,
            event_date,
            event_offset,
            close_price,
            bm_close,
            -- 前日比リターン
            (close_price / LAG(close_price) OVER (
                PARTITION BY ticker, event_date ORDER BY event_offset
            ) - 1.0) AS actual_return,
            (bm_close / LAG(bm_close) OVER (
                PARTITION BY ticker, event_date ORDER BY event_offset
            ) - 1.0) AS normal_return
        FROM joined
    ),
    ar AS (
        SELECT
            ticker              AS benchmark_id,
            event_date,
            event_offset,
            actual_return,
            normal_return,
            (actual_return - normal_return)  AS abnormal_return,
            COUNT(*) OVER (PARTITION BY ticker, event_date) AS n_events
        FROM returns
        WHERE actual_return IS NOT NULL
    )
    SELECT
        benchmark_id,
        event_date,
        event_offset,
        abnormal_return,
        -- CAR: t0 からの累積
        SUM(abnormal_return) OVER (
            PARTITION BY benchmark_id, event_date
            ORDER BY event_offset
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        )                       AS car_from_t0,
        normal_return,
        actual_return,
        CAST(n_events AS INT)   AS n_events
    FROM ar
    ORDER BY benchmark_id, event_date, event_offset
    """

    logger.info("DuckDB panel SQL 実行中: path=%s", path)
    df = con.execute(sql).df()
    con.close()
    logger.info("DuckDB panel 完成: rows=%d", len(df))
    return df


def _build_sample_panel() -> Any:
    """
    Parquet なしでテスト用サンプル DataFrame を返す。
    """
    import pandas as pd
    import numpy as np

    rng = np.random.default_rng(42)
    rows = []
    for ticker in ["7203.T", "9984.T", "MOMO_FACTOR"]:
        for offset in range(-5, 11):
            ar = rng.normal(0.001, 0.01)
            rows.append({
                "benchmark_id": ticker,
                "event_date": pd.Timestamp("2026-01-10").date(),
                "event_offset": offset,
                "abnormal_return": float(ar),
                "car_from_t0": float(ar * (offset + 6)),  # 簡易近似
                "normal_return": float(rng.normal(0.0005, 0.008)),
                "actual_return": float(rng.normal(0.001, 0.012)),
                "n_events": 1,
            })

    df = pd.DataFrame(rows)
    logger.info("サンプル panel 生成: rows=%d", len(df))
    return df
