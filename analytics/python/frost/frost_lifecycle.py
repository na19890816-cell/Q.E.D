"""
frost_lifecycle.py
------------------
Phase 3 (G3): Detect → Kill ライフサイクル管理エンジン

## 背景 (NOTE-003 / 憲法ギャップ G3)

設計憲法のシグナルライフサイクル:
    Predict → Select → Execute → **Detect** → **Kill**

本モジュールは Detect フェーズと Kill キュー管理を担う。
CUSUM 検知 (frost_cusum.py) の上位レイヤーとして機能する。

## 設計原則

- **純 Python** — numpy/statistics 不使用 (ADR-001 完全準拠)
- **副作用なし** — 純関数 / dataclass メソッド (DB 接続不要)
- **半自動 Kill** — CUSUM で Detect → レビューキュー追加 → 人間承認で Kill
  完全自動 Kill は人間レビュー省略になるため実装しない (NOTE-003 §3.2 の方針)
- **DB レス設計** — LifecycleRecord / KillQueue は pure dataclass
  実際の永続化は pg_io 層が別途担う

## 公開 API

    LifecycleStatus (Enum-like 定数)
        ACTIVE / DEGRADED / UNDER_REVIEW / REVOKED / SUSPENDED

    LifecycleRecord
        artifact_id, status, rolling_ic, cusum_result, ...

    AlphaLifecycleEngine
        .check(record) -> LifecycleCheckResult
        .check_batch(records) -> List[LifecycleCheckResult]
        .build_kill_queue(records) -> KillQueue

    KillQueue
        .pending_reviews: List[LifecycleCheckResult]
        .to_dict()

    check_alpha_degradation(artifact_id, rolling_ic, params) -> LifecycleCheckResult
        — 関数型ラッパー
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from analytics.python.frost.frost_cusum import (
    CusumDetector,
    CusumParams,
    CusumRunResult,
    detect_degradation_cusum,
)


# ---------------------------------------------------------------------------
# LifecycleStatus 定数
# ---------------------------------------------------------------------------

class LifecycleStatus:
    """
    昇格済みアルファのライフサイクル状態。

    NOTE-003 §3.2 のフロー図に対応する。
    promotion_status の拡張として使用する (frost_contracts.py §14.4 参照)。
    """
    ACTIVE: str = "active"              # 正常稼働中
    DEGRADED: str = "degraded"          # CUSUM が Detect トリガー (要レビュー)
    UNDER_REVIEW: str = "under_review"  # 人間レビュー中
    REVOKED: str = "revoked"            # Kill 確定（昇格取消）
    SUSPENDED: str = "suspended"        # 一時停止（Champion-Challenger 並走中）

    ALL: tuple = (ACTIVE, DEGRADED, UNDER_REVIEW, REVOKED, SUSPENDED)


# ---------------------------------------------------------------------------
# LifecycleCheckResult
# ---------------------------------------------------------------------------

@dataclass
class LifecycleCheckResult:
    """
    AlphaLifecycleEngine.check() の 1 アルファの検査結果。
    """
    artifact_id: str
    current_status: str                 # 検査前の状態
    new_status: str                     # 検査後の推奨状態
    degradation_detected: bool          # CUSUM 下方 Detect トリガー
    cusum_result: Optional[CusumRunResult] = None
    rolling_ic_len: int = 0
    review_required: bool = False       # Kill キューに積むべきか
    reason: str = ""                    # 遷移理由の説明

    @property
    def status_changed(self) -> bool:
        """状態が変化したか。"""
        return self.current_status != self.new_status

    def to_dict(self) -> Dict[str, Any]:
        """診断用辞書。FrostEvaluation.diagnostics_json への格納に使用。"""
        cusum_d = self.cusum_result.to_dict() if self.cusum_result else {}
        return {
            "lifecycle": "detect_kill",
            "artifact_id": self.artifact_id,
            "current_status": self.current_status,
            "new_status": self.new_status,
            "degradation_detected": self.degradation_detected,
            "rolling_ic_len": self.rolling_ic_len,
            "review_required": self.review_required,
            "reason": self.reason,
            "cusum": cusum_d,
        }


# ---------------------------------------------------------------------------
# KillQueue
# ---------------------------------------------------------------------------

@dataclass
class KillQueue:
    """
    降格レビュー待ちアルファのキュー。

    AlphaLifecycleEngine.build_kill_queue() が生成する。
    実際の kill 操作は pg_io 層（postgres_frost_lifecycle_bridge.py 等）が担う。
    """
    pending_reviews: List[LifecycleCheckResult] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.pending_reviews)

    @property
    def artifact_ids(self) -> List[str]:
        return [r.artifact_id for r in self.pending_reviews]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kill_queue": "frost_lifecycle",
            "pending_count": self.count,
            "pending_artifact_ids": self.artifact_ids,
            "items": [r.to_dict() for r in self.pending_reviews],
        }


# ---------------------------------------------------------------------------
# LifecycleRecord (入力 DTO)
# ---------------------------------------------------------------------------

@dataclass
class LifecycleRecord:
    """
    AlphaLifecycleEngine に渡す 1 アルファのライフサイクル情報。

    knowledge_artifacts + 実績 IC 時系列から構築する。
    """
    artifact_id: str
    current_status: str = LifecycleStatus.ACTIVE
    rolling_ic: List[float] = field(default_factory=list)
    """昇格後の rolling IC 時系列 (古い順)。"""
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# AlphaLifecycleEngine
# ---------------------------------------------------------------------------

@dataclass
class AlphaLifecycleEngine:
    """
    昇格済みアルファの Detect → Kill ライフサイクル管理エンジン。

    CUSUM 検知結果に基づき、アルファの状態遷移を判定する。
    Kill 自体は行わず、KillQueue にレビュー候補を積む (半自動 Kill 方針)。

    Attributes
    ----------
    params : CusumParams
        CUSUM パラメータ。デフォルトは k=0.5, h=5.0, mu0=0.0。
    min_ic_len : int
        CUSUM を実行するために必要な最小 rolling IC データ点数。
        これに満たない場合は CUSUM をスキップし current_status を維持する。
    """

    params: CusumParams = field(default_factory=CusumParams)
    min_ic_len: int = 10

    def check(self, record: LifecycleRecord) -> LifecycleCheckResult:
        """
        1 アルファのライフサイクルを検査する。

        Parameters
        ----------
        record : LifecycleRecord
            検査対象アルファの情報。

        Returns
        -------
        LifecycleCheckResult
            new_status: 推奨する次の状態。
            review_required: Kill キューに積むべきか。
        """
        artifact_id = record.artifact_id
        current = record.current_status
        ic_series = record.rolling_ic

        # --- 既に REVOKED / UNDER_REVIEW のものは再検査しない ---
        if current in (LifecycleStatus.REVOKED, LifecycleStatus.UNDER_REVIEW):
            return LifecycleCheckResult(
                artifact_id=artifact_id,
                current_status=current,
                new_status=current,
                degradation_detected=False,
                rolling_ic_len=len(ic_series),
                review_required=False,
                reason=f"ステータス {current!r} のため再検査スキップ",
            )

        # --- IC データ不足 → スキップ ---
        if len(ic_series) < self.min_ic_len:
            return LifecycleCheckResult(
                artifact_id=artifact_id,
                current_status=current,
                new_status=current,
                degradation_detected=False,
                rolling_ic_len=len(ic_series),
                review_required=False,
                reason=(
                    f"rolling IC データ不足 ({len(ic_series)} < {self.min_ic_len}) "
                    "— CUSUM スキップ"
                ),
            )

        # --- CUSUM 実行 ---
        detector = CusumDetector(params=self.params)
        cusum_result = detector.run(ic_series)

        if cusum_result.degradation_detected:
            new_status = LifecycleStatus.DEGRADED
            review_required = True
            reason = (
                f"CUSUM 下方 Detect: step={cusum_result.first_degradation_index}, "
                f"cusum_neg={cusum_result.final_cusum_neg:.3f} >= h={self.params.h}"
            )
        else:
            # 以前 DEGRADED だったが CUSUM がリセット → ACTIVE に戻す
            if current == LifecycleStatus.DEGRADED:
                new_status = LifecycleStatus.ACTIVE
                review_required = False
                reason = "CUSUM 下方未検知 — ACTIVE に復帰"
            else:
                new_status = current
                review_required = False
                reason = "CUSUM 正常 — 状態維持"

        return LifecycleCheckResult(
            artifact_id=artifact_id,
            current_status=current,
            new_status=new_status,
            degradation_detected=cusum_result.degradation_detected,
            cusum_result=cusum_result,
            rolling_ic_len=len(ic_series),
            review_required=review_required,
            reason=reason,
        )

    def check_batch(
        self, records: List[LifecycleRecord]
    ) -> List[LifecycleCheckResult]:
        """
        複数アルファを一括検査する。

        Parameters
        ----------
        records : List[LifecycleRecord]

        Returns
        -------
        List[LifecycleCheckResult]
            入力と同順。
        """
        return [self.check(r) for r in records]

    def build_kill_queue(
        self, records: List[LifecycleRecord]
    ) -> KillQueue:
        """
        records を一括検査し、review_required=True のものを KillQueue に積む。

        Parameters
        ----------
        records : List[LifecycleRecord]

        Returns
        -------
        KillQueue
        """
        results = self.check_batch(records)
        pending = [r for r in results if r.review_required]
        return KillQueue(pending_reviews=pending)

    @classmethod
    def from_config(cls, config: object) -> "AlphaLifecycleEngine":
        """
        FrostConfig / PolicySpec から AlphaLifecycleEngine を生成する。

        config に cusum_k / cusum_h / cusum_mu0 / lifecycle_min_ic_len
        属性がない場合はデフォルト値を使用する。
        """
        params = CusumParams.from_config(config)
        min_ic_len = int(getattr(config, "lifecycle_min_ic_len", 10))
        return cls(params=params, min_ic_len=min_ic_len)


# ---------------------------------------------------------------------------
# 関数型ラッパー
# ---------------------------------------------------------------------------

def check_alpha_degradation(
    artifact_id: str,
    rolling_ic: List[float],
    params: Optional[CusumParams] = None,
    min_ic_len: int = 10,
) -> LifecycleCheckResult:
    """
    1 アルファの劣化検知を実行する関数型ラッパー。

    Parameters
    ----------
    artifact_id : str
        検査対象アルファの ID。
    rolling_ic : List[float]
        昇格後の rolling IC 時系列。
    params : CusumParams | None
        CUSUM パラメータ。None の場合はデフォルト (k=0.5, h=5.0) を使用。
    min_ic_len : int
        CUSUM を実行する最小データ点数 (default: 10)。

    Returns
    -------
    LifecycleCheckResult
    """
    p = params or CusumParams()
    engine = AlphaLifecycleEngine(params=p, min_ic_len=min_ic_len)
    record = LifecycleRecord(
        artifact_id=artifact_id,
        current_status=LifecycleStatus.ACTIVE,
        rolling_ic=rolling_ic,
    )
    return engine.check(record)
