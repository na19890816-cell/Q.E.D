"""
alpha_genome_cluster.py
-----------------------
GenomeVector のクラスタリングモジュール。

k-means（numpy 化実装）で Genome 空間を因子グループに分類し、
近似因子の塊を可視化・管理する。

設計原則:
  - Phase 7 numpy 化 (ADR-001 対象): _l2_dist/_cosine_sim/_mean_vector/K-Means Assignment
  - K-Means（Lloyd アルゴリズム）— 距離行列を numpy ブロードキャストで一括計算
  - 環境変数: ALPHA_GENOME_CLUSTERING=1
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .alpha_genome_encoder import GENOME_AXES, GENOME_DIM, GenomeVector


# ---------------------------------------------------------------------------
# 定数・環境変数
# ---------------------------------------------------------------------------

_CLUSTERING_ENABLED: bool = os.environ.get(
    "ALPHA_GENOME_CLUSTERING", "1"
).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class GenomeCluster:
    """
    単一 Genome クラスター。

    Attributes
    ----------
    cluster_id : int
    centroid : dict[str, float]
        クラスター重心ベクトル
    member_ids : list[str]
        所属候補 ID リスト
    dominant_axis : str
        重心の最大軸
    intra_cluster_avg_sim : float
        クラスター内平均コサイン類似度
    """
    cluster_id: int
    centroid: Dict[str, float]
    member_ids: List[str]
    dominant_axis: str
    intra_cluster_avg_sim: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "cluster_id": self.cluster_id,
            "centroid": self.centroid,
            "member_ids": self.member_ids,
            "dominant_axis": self.dominant_axis,
            "intra_cluster_avg_sim": self.intra_cluster_avg_sim,
        }


@dataclass
class ClusteringResult:
    """
    クラスタリング全体の結果。
    """
    clusters: List[GenomeCluster]
    assignment: Dict[str, int]  # candidate_id → cluster_id
    n_clusters: int
    n_iterations: int
    converged: bool
    inertia: float  # クラスター内距離の総和

    def to_dict(self) -> Dict:
        return {
            "n_clusters": self.n_clusters,
            "n_iterations": self.n_iterations,
            "converged": self.converged,
            "inertia": self.inertia,
            "assignment": self.assignment,
            "clusters": [c.to_dict() for c in self.clusters],
        }


# ---------------------------------------------------------------------------
# K-Means（純 Python）
# ---------------------------------------------------------------------------

def _vec_to_list(v: Dict[str, float]) -> List[float]:
    return [v.get(a, 0.0) for a in GENOME_AXES]


def _l2_dist(v1: List[float], v2: List[float]) -> float:
    """L2 距離（後方互換シム — 内部は numpy）。"""
    a = np.array(v1, dtype=np.float64)
    b = np.array(v2, dtype=np.float64)
    return float(np.linalg.norm(a - b))


def _cosine_sim(v1: List[float], v2: List[float]) -> float:
    """コサイン類似度（後方互換シム — 内部は numpy）。"""
    a = np.array(v1, dtype=np.float64)
    b = np.array(v2, dtype=np.float64)
    n1 = np.linalg.norm(a)
    n2 = np.linalg.norm(b)
    if n1 < 1e-15 or n2 < 1e-15:
        return 0.0
    return float(min(1.0, np.dot(a, b) / (n1 * n2)))


def _mean_vector(vectors: List[List[float]]) -> List[float]:
    """ベクトルリストの平均を計算する（後方互換シム — 内部は numpy）。"""
    if not vectors:
        return [0.0] * GENOME_DIM
    return np.array(vectors, dtype=np.float64).mean(axis=0).tolist()


def _normalize(v: List[float]) -> List[float]:
    """ベクトルを L1 正規化する（合計が 1 になるよう）。"""
    total = sum(v)
    if total < 1e-15:
        return [1.0 / GENOME_DIM] * GENOME_DIM
    return [x / total for x in v]


def cluster_genomes(
    genomes: List[GenomeVector],
    k: int = 5,
    max_iter: int = 100,
    tol: float = 1e-6,
    random_seed: int = 42,
) -> ClusteringResult:
    """
    GenomeVector リストを K-Means でクラスタリングする。

    Parameters
    ----------
    genomes : list[GenomeVector]
    k : int
        クラスター数
    max_iter : int
        最大イテレーション数
    tol : float
        重心移動の収束判定閾値
    random_seed : int

    Returns
    -------
    ClusteringResult
    """
    if not genomes:
        return ClusteringResult(
            clusters=[], assignment={}, n_clusters=0,
            n_iterations=0, converged=True, inertia=0.0,
        )

    n = len(genomes)
    k = min(k, n)
    vectors = [g.to_list() for g in genomes]
    ids = [g.candidate_id for g in genomes]

    # K-Means++ 初期化（k個の重心を選ぶ）
    rng = random.Random(random_seed)
    centroids: List[List[float]] = _kmeans_plus_plus_init(vectors, k, rng)

    assignment: List[int] = [0] * n
    converged = False
    n_iter = 0

    # numpy 行列変換（Assignment ステップの高速化）
    vec_mat = np.array(vectors, dtype=np.float64)  # (N, D)

    for iteration in range(max_iter):
        n_iter = iteration + 1

        # Assignment ステップ — ブロードキャスト距離行列 (N, k)
        cent_mat = np.array(centroids, dtype=np.float64)  # (k, D)
        # diff: (N, k, D) → 各点と各重心の差
        diff = vec_mat[:, np.newaxis, :] - cent_mat[np.newaxis, :, :]  # (N, k, D)
        dists = np.sqrt(np.sum(diff ** 2, axis=2))  # (N, k)
        new_assignment: List[int] = np.argmin(dists, axis=1).tolist()

        # Update ステップ
        new_centroids: List[List[float]] = []
        for ci in range(k):
            mask = np.array(new_assignment) == ci
            if mask.any():
                new_centroids.append(vec_mat[mask].mean(axis=0).tolist())
            else:
                # 空クラスター → ランダム再初期化
                new_centroids.append(vectors[rng.randint(0, n - 1)])

        # 収束チェック
        old_cent = np.array(centroids, dtype=np.float64)
        new_cent = np.array(new_centroids, dtype=np.float64)
        max_centroid_shift = float(np.max(np.linalg.norm(new_cent - old_cent, axis=1)))
        assignment = new_assignment
        centroids = new_centroids

        if max_centroid_shift < tol:
            converged = True
            break

    # 結果整理
    assignment_map = {ids[i]: assignment[i] for i in range(n)}

    # クラスター情報を構築
    clusters: List[GenomeCluster] = []
    for ci in range(k):
        member_ids = [ids[i] for i in range(n) if assignment[i] == ci]
        centroid_vec = {a: centroids[ci][ai] for ai, a in enumerate(GENOME_AXES)}
        dominant_axis = max(centroid_vec, key=lambda x: centroid_vec[x])

        # クラスター内平均類似度
        member_vecs = [vectors[i] for i in range(n) if assignment[i] == ci]
        intra_sim = _intra_cluster_avg_similarity(member_vecs)

        clusters.append(GenomeCluster(
            cluster_id=ci,
            centroid=centroid_vec,
            member_ids=member_ids,
            dominant_axis=dominant_axis,
            intra_cluster_avg_sim=intra_sim,
        ))

    # 慣性（inertia）計算 — numpy 一括
    cent_mat_final = np.array(centroids, dtype=np.float64)  # (k, D)
    assign_arr = np.array(assignment, dtype=np.int64)        # (N,)
    assigned_cents = cent_mat_final[assign_arr]              # (N, D)
    inertia = float(np.sum(np.sum((vec_mat - assigned_cents) ** 2, axis=1)))

    return ClusteringResult(
        clusters=clusters,
        assignment=assignment_map,
        n_clusters=k,
        n_iterations=n_iter,
        converged=converged,
        inertia=inertia,
    )


def _kmeans_plus_plus_init(
    vectors: List[List[float]],
    k: int,
    rng: random.Random,
) -> List[List[float]]:
    """K-Means++ 初期化: 最初の重心をランダムに、残りは距離比例確率で選択。"""
    n = len(vectors)
    centroids: List[List[float]] = [vectors[rng.randint(0, n - 1)]]

    for _ in range(k - 1):
        # 各点から最近傍重心までの距離
        dists = []
        for vec in vectors:
            min_d = min(_l2_dist(vec, c) for c in centroids)
            dists.append(min_d ** 2)

        # 距離比例確率でサンプリング（ルーレット選択）
        total_d = sum(dists)
        if total_d < 1e-15:
            centroids.append(vectors[rng.randint(0, n - 1)])
            continue
        threshold = rng.uniform(0, total_d)
        cumsum = 0.0
        chosen = 0
        for i, d in enumerate(dists):
            cumsum += d
            if cumsum >= threshold:
                chosen = i
                break
        centroids.append(vectors[chosen])

    return centroids


def _intra_cluster_avg_similarity(member_vecs: List[List[float]]) -> float:
    """クラスター内の全ペアのコサイン類似度平均を計算する（numpy 一括）。"""
    n = len(member_vecs)
    if n < 2:
        return 1.0 if n == 1 else 0.0

    mat = np.array(member_vecs, dtype=np.float64)  # (n, D)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)  # (n, 1)
    norms = np.where(norms < 1e-15, 1.0, norms)
    normed = mat / norms  # (n, D)
    cos_mat = normed @ normed.T  # (n, n)
    cos_mat = np.clip(cos_mat, 0.0, 1.0)

    # 上三角（対角除く）の平均
    i_idx, j_idx = np.triu_indices(n, k=1)
    count = len(i_idx)
    return float(cos_mat[i_idx, j_idx].sum() / count) if count > 0 else 0.0
