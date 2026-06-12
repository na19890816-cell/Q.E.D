"""
frost_worker_pool.py
--------------------
FROST 並列処理のワーカープール管理モジュール。

multiprocessing による並列化の基盤。
低スペック環境での worker 数制限に対応する。

環境変数:
  FROST_PBO_PARALLEL_ENABLED=1
  FROST_PBO_MAX_WORKERS=4
  FROST_PBO_FASTPATH=python
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

T = TypeVar("T")


# ---------------------------------------------------------------------------
# 定数・環境変数
# ---------------------------------------------------------------------------

_PBO_PARALLEL_ENABLED: bool = os.environ.get(
    "FROST_PBO_PARALLEL_ENABLED", "0"
).strip().lower() in ("1", "true", "yes", "on")

_PBO_MAX_WORKERS: int = max(1, int(os.environ.get("FROST_PBO_MAX_WORKERS", "4")))
_PBO_FASTPATH: str = os.environ.get("FROST_PBO_FASTPATH", "python")


# ---------------------------------------------------------------------------
# WorkerConfig
# ---------------------------------------------------------------------------

@dataclass
class WorkerPoolConfig:
    """
    ワーカープールの設定。

    Attributes
    ----------
    enabled : bool
        並列化を有効にするか
    max_workers : int
        最大ワーカー数
    fastpath : str
        "python" | "numpy" (Phase B: numpy 高速化分岐)
    chunk_size : int
        各ワーカーに割り当てるタスク数（バッチサイズ）
    timeout_seconds : int
        各タスクのタイムアウト
    """
    enabled: bool = _PBO_PARALLEL_ENABLED
    max_workers: int = _PBO_MAX_WORKERS
    fastpath: str = _PBO_FASTPATH
    chunk_size: int = 10
    timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> "WorkerPoolConfig":
        return cls(
            enabled=_PBO_PARALLEL_ENABLED,
            max_workers=_PBO_MAX_WORKERS,
            fastpath=_PBO_FASTPATH,
        )

    def effective_workers(self, n_tasks: int) -> int:
        """タスク数に応じた実効ワーカー数を返す。"""
        if not self.enabled:
            return 1
        return min(self.max_workers, max(1, n_tasks))


# ---------------------------------------------------------------------------
# 並列実行（シリアルフォールバック付き）
# ---------------------------------------------------------------------------

def parallel_map(
    func: Callable[[Any], T],
    items: List[Any],
    config: Optional[WorkerPoolConfig] = None,
) -> List[T]:
    """
    items を func で並列処理する。

    FROST_PBO_PARALLEL_ENABLED=0 またはシングルコアの場合は
    シリアル処理にフォールバックする。

    Parameters
    ----------
    func : callable
    items : list
    config : WorkerPoolConfig, optional

    Returns
    -------
    list of results
    """
    if config is None:
        config = WorkerPoolConfig.from_env()

    if not items:
        return []

    n_workers = config.effective_workers(len(items))

    if n_workers <= 1 or not config.enabled:
        # シリアル処理
        return [func(item) for item in items]

    # 並列処理（multiprocessing）
    try:
        import multiprocessing
        with multiprocessing.Pool(processes=n_workers) as pool:
            results = pool.map(func, items)
        return results
    except Exception:
        # 並列化失敗時はシリアルにフォールバック
        return [func(item) for item in items]


def parallel_map_chunks(
    func: Callable[[List[Any]], List[T]],
    items: List[Any],
    config: Optional[WorkerPoolConfig] = None,
) -> List[T]:
    """
    items をチャンク単位で並列処理する（チャンク単位の func を呼ぶ版）。

    Parameters
    ----------
    func : callable
        list[item] → list[result] を返す関数
    items : list
    config : WorkerPoolConfig, optional

    Returns
    -------
    list of results (フラット化済み)
    """
    if config is None:
        config = WorkerPoolConfig.from_env()

    if not items:
        return []

    chunk_size = max(1, config.chunk_size)
    chunks = [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]

    n_workers = config.effective_workers(len(chunks))

    if n_workers <= 1 or not config.enabled:
        results = []
        for chunk in chunks:
            results.extend(func(chunk))
        return results

    try:
        import multiprocessing
        with multiprocessing.Pool(processes=n_workers) as pool:
            chunk_results = pool.map(func, chunks)
        return [item for sublist in chunk_results for item in sublist]
    except Exception:
        results = []
        for chunk in chunks:
            results.extend(func(chunk))
        return results
