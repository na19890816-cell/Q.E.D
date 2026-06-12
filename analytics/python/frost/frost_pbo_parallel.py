"""
frost_pbo_parallel.py
---------------------
PBO / CPCV 近似の並列化実装。

既存の frost_pbo.py (シリアル版) を multiprocessing で並列化する。
低スペック環境では worker 数を制限する。

フロー:
  1. 候補リストを worker ごとのチャンクに分割
  2. 各 worker が独立して compute_pbo() を実行
  3. 結果を集約して返す

設計原則:
  - frost_pbo.py の compute_pbo() を呼び出す（ロジック重複なし）
  - FROST_PBO_PARALLEL_ENABLED=0 の場合はシリアル動作
  - 環境変数: FROST_PBO_PARALLEL_ENABLED=1, FROST_PBO_MAX_WORKERS=4
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .frost_pbo import compute_pbo_all as compute_pbo
from .frost_worker_pool import WorkerPoolConfig, parallel_map


# ---------------------------------------------------------------------------
# 定数・環境変数
# ---------------------------------------------------------------------------

_PBO_PARALLEL_ENABLED: bool = os.environ.get(
    "FROST_PBO_PARALLEL_ENABLED", "0"
).strip().lower() in ("1", "true", "yes", "on")

_PBO_MAX_WORKERS: int = max(1, int(os.environ.get("FROST_PBO_MAX_WORKERS", "4")))


# ---------------------------------------------------------------------------
# 並列 PBO タスク定義
# ---------------------------------------------------------------------------

@dataclass
class PBOTask:
    """
    単一候補の PBO 計算タスク。

    Attributes
    ----------
    candidate_id : str
    fold_sharpes : list[float]
        IS fold の Sharpe 比リスト
    oos_sharpes : list[float]
        OOS fold の Sharpe 比リスト
    """
    candidate_id: str
    fold_sharpes: List[float]
    oos_sharpes: List[float]


@dataclass
class PBOParallelResult:
    """
    並列 PBO 実行の全体結果。

    Attributes
    ----------
    results : dict[str, PBOResult]
        candidate_id → PBOResult のマッピング
    n_tasks : int
    n_workers_used : int
    parallel_enabled : bool
    """
    results: Dict[str, "PBOResult"]
    n_tasks: int
    n_workers_used: int
    parallel_enabled: bool

    def get(self, candidate_id: str) -> Optional["PBOResult"]:
        return self.results.get(candidate_id)


# ---------------------------------------------------------------------------
# 並列 PBO 実行
# ---------------------------------------------------------------------------

def _run_pbo_task(task: PBOTask) -> Tuple[str, "PBOResult"]:
    """
    単一タスクの PBO 計算ワーカー関数。
    multiprocessing.Pool.map から呼ばれる（pickle 可能である必要がある）。
    """
    # compute_pbo_all は fold_results: List[Dict] 形式を受け取る
    fold_results = []
    n = max(len(task.fold_sharpes), len(task.oos_sharpes), 1)
    for i in range(len(task.fold_sharpes)):
        fold_results.append({
            "fold_sharpe": task.fold_sharpes[i],
            "oos_sharpe": task.oos_sharpes[i] if i < len(task.oos_sharpes) else 0.0,
        })
    raw = compute_pbo(fold_results)
    # dict を返すので PBOResult 互換 dict として返す
    return task.candidate_id, raw


def run_pbo_parallel(
    tasks: List[PBOTask],
    config: Optional[WorkerPoolConfig] = None,
) -> PBOParallelResult:
    """
    複数候補の PBO を並列計算する。

    Parameters
    ----------
    tasks : list[PBOTask]
    config : WorkerPoolConfig, optional

    Returns
    -------
    PBOParallelResult
    """
    if config is None:
        config = WorkerPoolConfig.from_env()

    if not tasks:
        return PBOParallelResult(
            results={}, n_tasks=0, n_workers_used=0,
            parallel_enabled=config.enabled,
        )

    n_workers = config.effective_workers(len(tasks))

    # 並列 or シリアル実行
    raw_results: List[Tuple[str, "PBOResult"]] = parallel_map(
        func=_run_pbo_task,
        items=tasks,
        config=config,
    )

    results_map: Dict[str, "PBOResult"] = {
        cid: res for cid, res in raw_results
    }

    return PBOParallelResult(
        results=results_map,
        n_tasks=len(tasks),
        n_workers_used=n_workers,
        parallel_enabled=config.enabled and n_workers > 1,
    )


def build_pbo_tasks_from_evaluations(
    evaluations: List[Dict[str, Any]],
) -> List[PBOTask]:
    """
    FROST evaluations リストから PBOTask リストを構築する便利関数。

    Parameters
    ----------
    evaluations : list[dict]
        各 dict は "candidate_id", "fold_sharpes", "oos_sharpes" を持つこと

    Returns
    -------
    list[PBOTask]
    """
    tasks: List[PBOTask] = []
    for ev in evaluations:
        cid = ev.get("candidate_id", "")
        fold_sharpes = ev.get("fold_sharpes", [])
        oos_sharpes = ev.get("oos_sharpes", [])
        if isinstance(fold_sharpes, list) and isinstance(oos_sharpes, list):
            tasks.append(PBOTask(
                candidate_id=cid,
                fold_sharpes=[float(x) for x in fold_sharpes],
                oos_sharpes=[float(x) for x in oos_sharpes],
            ))
    return tasks
