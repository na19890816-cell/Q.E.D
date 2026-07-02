"""
alpha_genome_similarity.py
--------------------------
GenomeVector 間の類似度計算・新規性スコア付与モジュール。

用途:
  - 候補ライブラリ全体でのゲノム類似度行列を計算
  - 各候補の novelty_score を算出（既知ゲノムとの最小距離）
  - 近似 Genome 候補を検出して排除候補リストを提示

設計原則:
  - Phase 7 numpy 化 (ADR-001 対象): compute_genome_similarity_matrix 全ペア→行列一括
  - 副作用なし
  - N^2 計算を想定（候補数 < 1000 での利用を前提）
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from .alpha_genome_encoder import (
    GENOME_AXES,
    GenomeVector,
    genome_cosine_similarity,
    genome_l2_distance,
)


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class GenomeSimilarityEdge:
    """
    2候補間の Genome 類似度エッジ。

    Attributes
    ----------
    candidate_id_a : str
    candidate_id_b : str
    cosine_similarity : float  (0〜1)
    l2_distance : float        (0〜√10)
    dominant_axis_match : bool
        両候補の dominant_axis が同じかどうか
    """
    candidate_id_a: str
    candidate_id_b: str
    cosine_similarity: float
    l2_distance: float
    dominant_axis_match: bool

    def to_dict(self) -> Dict:
        return {
            "candidate_id_a": self.candidate_id_a,
            "candidate_id_b": self.candidate_id_b,
            "cosine_similarity": self.cosine_similarity,
            "l2_distance": self.l2_distance,
            "dominant_axis_match": self.dominant_axis_match,
        }


@dataclass
class GenomeNoveltyResult:
    """
    候補の Genome 新規性評価結果。

    Attributes
    ----------
    candidate_id : str
    novelty_score : float
        0〜1（高いほど既知ゲノムと異なる）
    min_cosine_sim : float
        ライブラリ内で最も類似した候補とのコサイン類似度
    most_similar_id : Optional[str]
        最も類似した候補の ID
    is_novel : bool
        novelty_score >= min_novelty_threshold であれば True
    """
    candidate_id: str
    novelty_score: float
    min_cosine_sim: float
    most_similar_id: Optional[str]
    is_novel: bool


# ---------------------------------------------------------------------------
# 類似度行列計算
# ---------------------------------------------------------------------------

def compute_genome_similarity_matrix(
    genomes: List[GenomeVector],
) -> List[GenomeSimilarityEdge]:
    """
    GenomeVector リストの全ペア類似度を計算する。

    Phase 7 numpy 化: GenomeVector を (N, D) 行列に変換し、
    コサイン類似度を行列積・L2距離を broadcast で一括計算することで
    O(N^2) の純 Python ループを除去する。

    N*(N-1)/2 ペアの上三角部分のみ返す（対称行列のため）。

    Parameters
    ----------
    genomes : list[GenomeVector]

    Returns
    -------
    list[GenomeSimilarityEdge]
    """
    n = len(genomes)
    edges: List[GenomeSimilarityEdge] = []
    if n < 2:
        return edges

    # (N, D) 行列へ変換
    mat = np.array([g.to_list() for g in genomes], dtype=np.float64)

    # コサイン類似度行列 — 正規化後の内積 (N, N)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)   # (N, 1)
    zero_mask = norms < 1e-15
    safe_norms = np.where(zero_mask, 1.0, norms)
    normed = mat / safe_norms                             # (N, D)
    cos_mat = np.clip(normed @ normed.T, -1.0, 1.0)       # (N, N)

    # L2 距離行列 — broadcast (N, N)
    diff = mat[:, np.newaxis, :] - mat[np.newaxis, :, :]  # (N, N, D)
    l2_mat = np.sqrt(np.sum(diff ** 2, axis=2))            # (N, N)

    # 上三角インデックス
    i_idx, j_idx = np.triu_indices(n, k=1)

    for ii, jj in zip(i_idx.tolist(), j_idx.tolist()):
        g1, g2 = genomes[ii], genomes[jj]
        edges.append(GenomeSimilarityEdge(
            candidate_id_a=g1.candidate_id,
            candidate_id_b=g2.candidate_id,
            cosine_similarity=float(cos_mat[ii, jj]),
            l2_distance=float(l2_mat[ii, jj]),
            dominant_axis_match=g1.dominant_axis == g2.dominant_axis,
        ))

    return edges


def build_similarity_lookup(
    edges: List[GenomeSimilarityEdge],
) -> Dict[str, Dict[str, float]]:
    """
    エッジリストから候補 ID → (相手ID → 類似度) の辞書を構築する。

    Parameters
    ----------
    edges : list[GenomeSimilarityEdge]

    Returns
    -------
    dict[str, dict[str, float]]
    """
    lookup: Dict[str, Dict[str, float]] = {}
    for edge in edges:
        lookup.setdefault(edge.candidate_id_a, {})[edge.candidate_id_b] = edge.cosine_similarity
        lookup.setdefault(edge.candidate_id_b, {})[edge.candidate_id_a] = edge.cosine_similarity
    return lookup


# ---------------------------------------------------------------------------
# 新規性スコア
# ---------------------------------------------------------------------------

def compute_novelty_scores(
    genomes: List[GenomeVector],
    reference_genomes: Optional[List[GenomeVector]] = None,
    min_novelty_threshold: float = 0.20,
) -> List[GenomeNoveltyResult]:
    """
    各候補の Genome 新規性スコアを計算する。

    novelty_score = 1 - max(cosine_similarity with any reference)

    reference_genomes が None の場合は genomes 内でのクロス比較。

    Parameters
    ----------
    genomes : list[GenomeVector]
        新規性を評価する候補リスト
    reference_genomes : list[GenomeVector], optional
        既知の Genome ライブラリ。None なら genomes 自身を参照。
    min_novelty_threshold : float
        新規とみなす閾値（デフォルト: 0.20）

    Returns
    -------
    list[GenomeNoveltyResult]
    """
    if reference_genomes is None:
        # 自己参照: 他の全候補との最大類似度を使う
        return _compute_cross_novelty(genomes, genomes, min_novelty_threshold, exclude_self=True)
    return _compute_cross_novelty(genomes, reference_genomes, min_novelty_threshold, exclude_self=False)


def _compute_cross_novelty(
    targets: List[GenomeVector],
    references: List[GenomeVector],
    threshold: float,
    exclude_self: bool,
) -> List[GenomeNoveltyResult]:
    """Phase 7 numpy 化: targets × references のコサイン類似度を行列一括計算。"""
    results: List[GenomeNoveltyResult] = []
    if not references:
        for target in targets:
            results.append(GenomeNoveltyResult(
                candidate_id=target.candidate_id,
                novelty_score=1.0,
                min_cosine_sim=0.0,
                most_similar_id=None,
                is_novel=True,
            ))
        return results

    t_mat = np.array([g.to_list() for g in targets], dtype=np.float64)    # (T, D)
    r_mat = np.array([g.to_list() for g in references], dtype=np.float64)  # (R, D)

    # 正規化
    def _safe_norm(m: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        return m / np.where(norms < 1e-15, 1.0, norms)

    t_normed = _safe_norm(t_mat)
    r_normed = _safe_norm(r_mat)
    cos_mat = np.clip(t_normed @ r_normed.T, 0.0, 1.0)  # (T, R)

    for ti, target in enumerate(targets):
        row = cos_mat[ti].copy()
        if exclude_self:
            for ri, ref in enumerate(references):
                if ref.candidate_id == target.candidate_id:
                    row[ri] = 0.0
        if row.size == 0 or row.max() == 0.0:
            results.append(GenomeNoveltyResult(
                candidate_id=target.candidate_id,
                novelty_score=1.0,
                min_cosine_sim=0.0,
                most_similar_id=None,
                is_novel=True,
            ))
            continue
        best_ri = int(np.argmax(row))
        max_sim = float(row[best_ri])
        novelty_score = 1.0 - max_sim
        results.append(GenomeNoveltyResult(
            candidate_id=target.candidate_id,
            novelty_score=novelty_score,
            min_cosine_sim=max_sim,
            most_similar_id=references[best_ri].candidate_id,
            is_novel=novelty_score >= threshold,
        ))

    return results


def assign_novelty_scores(
    genomes: List[GenomeVector],
    novelty_results: List[GenomeNoveltyResult],
) -> List[GenomeVector]:
    """
    novelty_results を使って GenomeVector の novelty_score を更新する。

    Parameters
    ----------
    genomes : list[GenomeVector]
    novelty_results : list[GenomeNoveltyResult]

    Returns
    -------
    list[GenomeVector]  (novelty_score 更新済み)
    """
    novelty_map = {r.candidate_id: r.novelty_score for r in novelty_results}
    updated = []
    for g in genomes:
        if g.candidate_id in novelty_map:
            # dataclass は mutable なので直接更新
            g.novelty_score = novelty_map[g.candidate_id]
        updated.append(g)
    return updated


# ---------------------------------------------------------------------------
# 近似候補検出
# ---------------------------------------------------------------------------

def find_genome_near_duplicates(
    genomes: List[GenomeVector],
    similarity_threshold: float = 0.90,
) -> List[Tuple[str, str, float]]:
    """
    コサイン類似度が threshold を超えるペアを near-duplicate として返す。

    Parameters
    ----------
    genomes : list[GenomeVector]
    similarity_threshold : float
        近似判定閾値（デフォルト 0.90）

    Returns
    -------
    list[(id_a, id_b, cosine_sim)]
    """
    near_dups: List[Tuple[str, str, float]] = []
    n = len(genomes)

    for i in range(n):
        for j in range(i + 1, n):
            sim = genome_cosine_similarity(genomes[i], genomes[j])
            if sim >= similarity_threshold:
                near_dups.append((
                    genomes[i].candidate_id,
                    genomes[j].candidate_id,
                    sim,
                ))

    return sorted(near_dups, key=lambda x: -x[2])
