"""
alpha_genome_runner.py
----------------------
Alpha Genome Layer のオーケストレーターモジュール。

候補リストを受け取り、全 Genome 評価パイプラインを実行して
GenomeVector リスト・クラスタリング・新規性スコア・レポートを返す。

フロー:
  1. 候補ごとに encode_formula() でゲノムエンコード
  2. compute_genome_similarity_matrix() で類似度行列
  3. compute_novelty_scores() で新規性スコア付与
  4. cluster_genomes() でクラスタリング
  5. find_genome_near_duplicates() で近似候補検出
  6. build_genome_report() でサマリーレポート

設計原則:
  - trace_id end-to-end
  - dry_run 対応
  - 副作用なし（PostgreSQL 書き込みは bridge.py が担う）
  - 環境変数: ALPHA_GENOME_ENABLED=1
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .alpha_genome_encoder import (
    GENOME_AXES,
    GenomeVector,
    encode_formula,
)
from .alpha_genome_cluster import ClusteringResult, cluster_genomes
from .alpha_genome_report import (
    GenomeReport,
    build_genome_report,
    genome_report_to_frost_features,
)
from .alpha_genome_similarity import (
    GenomeNoveltyResult,
    GenomeSimilarityEdge,
    assign_novelty_scores,
    compute_genome_similarity_matrix,
    compute_novelty_scores,
    find_genome_near_duplicates,
)


# ---------------------------------------------------------------------------
# 定数・環境変数
# ---------------------------------------------------------------------------

_GENOME_ENABLED: bool = os.environ.get(
    "ALPHA_GENOME_ENABLED", "1"
).strip().lower() in ("1", "true", "yes", "on")

_GENOME_CLUSTERING: bool = os.environ.get(
    "ALPHA_GENOME_CLUSTERING", "1"
).strip().lower() in ("1", "true", "yes", "on")

_GENOME_MIN_NOVELTY_SCORE: float = float(
    os.environ.get("ALPHA_GENOME_MIN_NOVELTY_SCORE", "0.20")
)

_GENOME_NEAR_DUP_THRESHOLD: float = 0.90


# ---------------------------------------------------------------------------
# 入力候補の型（外部からの入力インターフェース）
# ---------------------------------------------------------------------------

@dataclass
class GenomeCandidate:
    """
    Genome 評価対象の候補。

    Attributes
    ----------
    candidate_id : str
    formula_text : str
    trace_id : str
    run_id : str
    extra : dict
        追加メタデータ（オプション）
    """
    candidate_id: str
    formula_text: str
    trace_id: str
    run_id: str
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 実行結果
# ---------------------------------------------------------------------------

@dataclass
class GenomeRunResult:
    """
    Genome Layer の実行結果。

    Attributes
    ----------
    run_id : str
    trace_id : str
    genomes : list[GenomeVector]
    novelty_results : list[GenomeNoveltyResult]
    similarity_edges : list[GenomeSimilarityEdge]
    clustering_result : ClusteringResult
    near_dup_pairs : list
    report : GenomeReport
    frost_features : dict[str, dict]
        FROST v2 統合用の候補別特徴量
    low_novelty_ids : list[str]
        新規性スコアが低い（排除候補）ID リスト
    dry_run : bool
    """
    run_id: str
    trace_id: str
    genomes: List[GenomeVector]
    novelty_results: List[GenomeNoveltyResult]
    similarity_edges: List[GenomeSimilarityEdge]
    clustering_result: Optional[ClusteringResult]
    near_dup_pairs: List[Any]
    report: GenomeReport
    frost_features: Dict[str, Dict[str, float]]
    low_novelty_ids: List[str]
    dry_run: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "n_candidates": len(self.genomes),
            "n_clusters": self.clustering_result.n_clusters if self.clustering_result else 0,
            "novel_ratio": self.report.novel_ratio,
            "mean_novelty_score": self.report.mean_novelty_score,
            "near_dup_count": len(self.near_dup_pairs),
            "low_novelty_count": len(self.low_novelty_ids),
            "dry_run": self.dry_run,
        }


# ---------------------------------------------------------------------------
# Genome Runner
# ---------------------------------------------------------------------------

def run_genome_layer(
    candidates: List[GenomeCandidate],
    run_id: str,
    trace_id: str,
    k_clusters: int = 5,
    near_dup_threshold: float = _GENOME_NEAR_DUP_THRESHOLD,
    min_novelty_threshold: float = _GENOME_MIN_NOVELTY_SCORE,
    reference_genomes: Optional[List[GenomeVector]] = None,
    dry_run: bool = False,
) -> GenomeRunResult:
    """
    Alpha Genome Layer のメイン実行関数。

    Parameters
    ----------
    candidates : list[GenomeCandidate]
        評価対象候補リスト
    run_id : str
        FROST run ID（trace_id 連携用）
    trace_id : str
        トレース ID（end-to-end 追跡用）
    k_clusters : int
        K-Means クラスター数
    near_dup_threshold : float
        近似候補検出の類似度閾値
    min_novelty_threshold : float
        新規とみなす最低 novelty_score
    reference_genomes : list[GenomeVector], optional
        既存ゲノムライブラリ（None の場合は自己参照）
    dry_run : bool
        True の場合は DB 書き込みを行わない

    Returns
    -------
    GenomeRunResult
    """
    if not _GENOME_ENABLED:
        return _disabled_result(run_id, trace_id, candidates, dry_run)

    if not candidates:
        return _empty_result(run_id, trace_id, dry_run)

    # Step 1: ゲノムエンコード
    genomes: List[GenomeVector] = []
    for cand in candidates:
        gv = encode_formula(cand.candidate_id, cand.formula_text)
        genomes.append(gv)

    # Step 2: 類似度行列
    similarity_edges = compute_genome_similarity_matrix(genomes)

    # Step 3: 新規性スコア付与
    novelty_results = compute_novelty_scores(
        genomes=genomes,
        reference_genomes=reference_genomes,
        min_novelty_threshold=min_novelty_threshold,
    )
    genomes = assign_novelty_scores(genomes, novelty_results)

    # Step 4: クラスタリング
    clustering_result: Optional[ClusteringResult] = None
    if _GENOME_CLUSTERING and len(genomes) >= 2:
        actual_k = min(k_clusters, len(genomes))
        clustering_result = cluster_genomes(genomes, k=actual_k)

    # Step 5: 近似候補検出
    near_dup_pairs = find_genome_near_duplicates(
        genomes, similarity_threshold=near_dup_threshold
    )

    # Step 6: レポート生成
    report = build_genome_report(
        genomes=genomes,
        novelty_results=novelty_results,
        clustering_result=clustering_result,
        near_dup_pairs=near_dup_pairs,
    )

    # FROST v2 特徴量変換
    frost_features = genome_report_to_frost_features(genomes, novelty_results)

    # 低新規性 ID リスト
    low_novelty_ids = [
        r.candidate_id for r in novelty_results
        if not r.is_novel
    ]

    return GenomeRunResult(
        run_id=run_id,
        trace_id=trace_id,
        genomes=genomes,
        novelty_results=novelty_results,
        similarity_edges=similarity_edges,
        clustering_result=clustering_result,
        near_dup_pairs=near_dup_pairs,
        report=report,
        frost_features=frost_features,
        low_novelty_ids=low_novelty_ids,
        dry_run=dry_run,
    )


def _disabled_result(
    run_id: str,
    trace_id: str,
    candidates: List[GenomeCandidate],
    dry_run: bool,
) -> GenomeRunResult:
    """ALPHA_GENOME_ENABLED=0 のときの no-op 結果。"""
    from .alpha_genome_report import _empty_report
    from .alpha_genome_cluster import ClusteringResult

    return GenomeRunResult(
        run_id=run_id,
        trace_id=trace_id,
        genomes=[],
        novelty_results=[],
        similarity_edges=[],
        clustering_result=None,
        near_dup_pairs=[],
        report=_empty_report(),
        frost_features={},
        low_novelty_ids=[],
        dry_run=dry_run,
    )


def _empty_result(run_id: str, trace_id: str, dry_run: bool) -> GenomeRunResult:
    """候補なしの空結果。"""
    return _disabled_result(run_id, trace_id, [], dry_run)
