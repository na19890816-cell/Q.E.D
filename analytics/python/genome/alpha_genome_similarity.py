"""
alpha_genome_similarity.py
--------------------------
GenomeVector 間の類似度計算・新規性スコア付与モジュール。

用途:
  - 候補ライブラリ全体でのゲノム類似度行列を計算
  - 各候補の novelty_score を算出（既知ゲノムとの最小距離）
  - 近似 Genome 候補を検出して排除候補リストを提示

設計原則:
  - pure Python
  - 副作用なし
  - N^2 計算を想定（候補数 < 1000 での利用を前提）
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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

    N*(N-1)/2 ペアの上三角部分のみ計算（対称行列のため）。

    Parameters
    ----------
    genomes : list[GenomeVector]

    Returns
    -------
    list[GenomeSimilarityEdge]
    """
    edges: List[GenomeSimilarityEdge] = []
    n = len(genomes)

    for i in range(n):
        for j in range(i + 1, n):
            g1 = genomes[i]
            g2 = genomes[j]
            cos_sim = genome_cosine_similarity(g1, g2)
            l2_dist = genome_l2_distance(g1, g2)
            dom_match = g1.dominant_axis == g2.dominant_axis

            edges.append(GenomeSimilarityEdge(
                candidate_id_a=g1.candidate_id,
                candidate_id_b=g2.candidate_id,
                cosine_similarity=cos_sim,
                l2_distance=l2_dist,
                dominant_axis_match=dom_match,
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
    results: List[GenomeNoveltyResult] = []

    for target in targets:
        max_sim = 0.0
        most_similar_id: Optional[str] = None

        for ref in references:
            if exclude_self and ref.candidate_id == target.candidate_id:
                continue
            sim = genome_cosine_similarity(target, ref)
            if sim > max_sim:
                max_sim = sim
                most_similar_id = ref.candidate_id

        novelty_score = 1.0 - max_sim
        is_novel = novelty_score >= threshold

        results.append(GenomeNoveltyResult(
            candidate_id=target.candidate_id,
            novelty_score=novelty_score,
            min_cosine_sim=max_sim,
            most_similar_id=most_similar_id,
            is_novel=is_novel,
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
