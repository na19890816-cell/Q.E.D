"""
alpha_genome_report.py
----------------------
Genome 分析レポート生成モジュール。

クラスタリング結果・新規性スコア・類似度エッジを集約し、
FROST v2 統合用のレポートを生成する。

設計原則:
  - pure Python
  - 副作用なし（print も除く）
  - dict/list で返す（JSON シリアライズ可能）
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .alpha_genome_encoder import GENOME_AXES, GenomeVector
from .alpha_genome_cluster import ClusteringResult
from .alpha_genome_similarity import (
    GenomeNoveltyResult,
    GenomeSimilarityEdge,
)


# ---------------------------------------------------------------------------
# レポートデータクラス
# ---------------------------------------------------------------------------

@dataclass
class GenomeReport:
    """
    全候補の Genome 分析サマリーレポート。

    Attributes
    ----------
    n_candidates : int
    n_clusters : int
    axis_distribution : dict[str, float]
        全候補でのゲノム軸の平均分布
    most_common_axis : str
        最も多い dominant_axis
    novel_count : int
    non_novel_count : int
    novel_ratio : float
    mean_novelty_score : float
    near_duplicate_pairs : list[tuple]
    cluster_summary : list[dict]
    top_novel_candidates : list[str]
    low_novelty_candidates : list[str]
    """
    n_candidates: int
    n_clusters: int
    axis_distribution: Dict[str, float]
    most_common_axis: str
    novel_count: int
    non_novel_count: int
    novel_ratio: float
    mean_novelty_score: float
    near_duplicate_pairs: List[Any]
    cluster_summary: List[Dict]
    top_novel_candidates: List[str]
    low_novelty_candidates: List[str]

    def to_dict(self) -> Dict:
        return {
            "n_candidates": self.n_candidates,
            "n_clusters": self.n_clusters,
            "axis_distribution": self.axis_distribution,
            "most_common_axis": self.most_common_axis,
            "novel_count": self.novel_count,
            "non_novel_count": self.non_novel_count,
            "novel_ratio": self.novel_ratio,
            "mean_novelty_score": self.mean_novelty_score,
            "near_duplicate_pairs": self.near_duplicate_pairs,
            "cluster_summary": self.cluster_summary,
            "top_novel_candidates": self.top_novel_candidates,
            "low_novelty_candidates": self.low_novelty_candidates,
        }


# ---------------------------------------------------------------------------
# レポート生成
# ---------------------------------------------------------------------------

def build_genome_report(
    genomes: List[GenomeVector],
    novelty_results: List[GenomeNoveltyResult],
    clustering_result: Optional[ClusteringResult] = None,
    near_dup_pairs: Optional[List[Any]] = None,
    top_n: int = 5,
) -> GenomeReport:
    """
    Genome 分析レポートを生成する。

    Parameters
    ----------
    genomes : list[GenomeVector]
    novelty_results : list[GenomeNoveltyResult]
    clustering_result : ClusteringResult, optional
    near_dup_pairs : list, optional
        find_genome_near_duplicates() の戻り値
    top_n : int
        top/low novelty 候補の表示数

    Returns
    -------
    GenomeReport
    """
    n = len(genomes)
    if n == 0:
        return _empty_report()

    # 軸分布（全候補の平均）
    axis_totals: Dict[str, float] = {a: 0.0 for a in GENOME_AXES}
    axis_counts: Dict[str, int] = {a: 0 for a in GENOME_AXES}
    for g in genomes:
        dom = g.dominant_axis
        axis_counts[dom] = axis_counts.get(dom, 0) + 1
        for a in GENOME_AXES:
            axis_totals[a] = axis_totals.get(a, 0.0) + g.vector.get(a, 0.0)

    axis_distribution = {a: axis_totals[a] / n for a in GENOME_AXES}
    most_common_axis = max(axis_counts, key=lambda k: axis_counts[k])

    # 新規性集計
    novelty_map = {r.candidate_id: r for r in novelty_results}
    novel_ids = [r.candidate_id for r in novelty_results if r.is_novel]
    non_novel_ids = [r.candidate_id for r in novelty_results if not r.is_novel]
    scores = [r.novelty_score for r in novelty_results]
    mean_novelty = sum(scores) / len(scores) if scores else 0.0

    # top/low novelty candidates
    sorted_by_novelty = sorted(novelty_results, key=lambda r: -r.novelty_score)
    top_novel = [r.candidate_id for r in sorted_by_novelty[:top_n]]
    low_novelty = [r.candidate_id for r in sorted_by_novelty[-top_n:]]

    # クラスター要約
    cluster_summary: List[Dict] = []
    if clustering_result:
        for c in clustering_result.clusters:
            cluster_summary.append({
                "cluster_id": c.cluster_id,
                "dominant_axis": c.dominant_axis,
                "member_count": len(c.member_ids),
                "intra_cluster_avg_sim": c.intra_cluster_avg_sim,
                "centroid_top3": sorted(
                    c.centroid.items(), key=lambda x: -x[1]
                )[:3],
            })

    return GenomeReport(
        n_candidates=n,
        n_clusters=clustering_result.n_clusters if clustering_result else 0,
        axis_distribution=axis_distribution,
        most_common_axis=most_common_axis,
        novel_count=len(novel_ids),
        non_novel_count=len(non_novel_ids),
        novel_ratio=len(novel_ids) / n if n > 0 else 0.0,
        mean_novelty_score=mean_novelty,
        near_duplicate_pairs=near_dup_pairs or [],
        cluster_summary=cluster_summary,
        top_novel_candidates=top_novel,
        low_novelty_candidates=low_novelty,
    )


def _empty_report() -> GenomeReport:
    return GenomeReport(
        n_candidates=0,
        n_clusters=0,
        axis_distribution={a: 0.0 for a in GENOME_AXES},
        most_common_axis=GENOME_AXES[0],
        novel_count=0,
        non_novel_count=0,
        novel_ratio=0.0,
        mean_novelty_score=0.0,
        near_duplicate_pairs=[],
        cluster_summary=[],
        top_novel_candidates=[],
        low_novelty_candidates=[],
    )


def genome_report_to_frost_features(
    genomes: List[GenomeVector],
    novelty_results: List[GenomeNoveltyResult],
) -> Dict[str, Dict[str, float]]:
    """
    各候補の Genome 情報を FROST v2 特徴量辞書に変換する。

    Returns
    -------
    dict[candidate_id, dict]
        {
          "genome_novelty_score": float,
          "genome_dominant_axis": str (encoded as index),
          "genome_confidence": float,
          ...
        }
    """
    novelty_map = {r.candidate_id: r for r in novelty_results}
    result: Dict[str, Dict[str, float]] = {}

    for g in genomes:
        novelty_r = novelty_map.get(g.candidate_id)
        novelty_score = novelty_r.novelty_score if novelty_r else g.novelty_score

        features: Dict[str, float] = {
            "genome_novelty_score": novelty_score,
            "genome_confidence": g.confidence,
            "genome_dominant_axis_idx": float(
                GENOME_AXES.index(g.dominant_axis)
                if g.dominant_axis in GENOME_AXES else 0
            ),
        }
        # ベクトル各軸も特徴量として追加
        for axis in GENOME_AXES:
            features[f"genome_{axis}"] = g.vector.get(axis, 0.0)

        result[g.candidate_id] = features

    return result
