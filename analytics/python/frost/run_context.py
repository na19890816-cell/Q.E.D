"""
run_context.py
--------------
RunContext — FROST / EML パイプラインの実行コンテキスト統一管理 (Phase 5 D5 負債解消)。

Phase 5 で新設。以前は run_id / trace_id / batch_label / dry_run / verbose が
各エントリポイント (frost_runner.py / run_eml_pipeline.py) で個別に
環境変数から取得されていた。本モジュールに一元化する。

公開 API
--------
RunContext
  .run_id        : str        (UUID4)
  .trace_id      : str        (UUID4)
  .batch_label   : str
  .dry_run       : bool
  .verbose       : bool
  .started_at    : datetime   (UTC)

  .from_env()    : classmethod — 環境変数から RunContext を生成
  .from_args()   : classmethod — argparse.Namespace から RunContext を生成
  .from_dict()   : classmethod — dict から RunContext を生成
  .to_dict()     : dict        — JSON シリアライズ可能な dict

設計原則
--------
- frozen=False (run_id / trace_id を後から子候補に付与できるよう可変にする)
- pure Python / numpy 不使用
- 後方互換: run_id / trace_id を単独で使う旧コードはそのまま動く
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


# --------------------------------------------------------------------------- #
# ユーティリティ
# --------------------------------------------------------------------------- #

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _env_bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, "1" if default else "0") == "1"


def _env_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _make_run_id(prefix: str = "run") -> str:
    """タイムスタンプ付きの run_id を生成する。"""
    ts = _now_utc().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}__{ts}"


def _make_trace_id(namespace: str = "frost") -> str:
    """UUID5 (決定論的) ベースの trace_id を生成する。"""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_DNS,
            f"{namespace}:{_now_utc().isoformat()}:{uuid.uuid4()}",
        )
    )


# --------------------------------------------------------------------------- #
# RunContext
# --------------------------------------------------------------------------- #

@dataclass
class RunContext:
    """
    パイプライン実行コンテキスト。

    Attributes
    ----------
    run_id : str
        実行単位の一意 ID。DB の frost_runs / eml_alpha_runs 主キーに使用。
    trace_id : str
        監査追跡 ID。全テーブルにわたって伝播させる。
    batch_label : str
        バッチラベル (例: "frost_v1", "eml_20260702")。
    dry_run : bool
        True の場合は DB への destructive 書き込みをスキップ。
    verbose : bool
        True の場合は詳細ログを出力。
    started_at : datetime
        コンテキスト生成時刻 (UTC)。
    pipeline : str
        パイプライン識別子 (例: "frost", "eml")。ログ用途。
    """
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    batch_label: str = "default"
    dry_run: bool = False
    verbose: bool = False
    started_at: datetime = field(default_factory=_now_utc)
    pipeline: str = "frost"

    # ---------------------------------------------------------------------- #
    # ファクトリメソッド
    # ---------------------------------------------------------------------- #

    @classmethod
    def from_env(
        cls,
        pipeline: str = "frost",
        prefix: str = "",
    ) -> "RunContext":
        """
        環境変数から RunContext を生成する。

        参照する環境変数 (prefix が "EML_" の場合は EML_RUN_ID など):
          {prefix}RUN_ID        : run_id   (未設定時は自動生成)
          {prefix}TRACE_ID      : trace_id (未設定時は自動生成)
          {prefix}BATCH_LABEL   : batch_label (未設定時は "{pipeline}_v1")
          {prefix}DRY_RUN       : "1" = dry_run=True
          {prefix}VERBOSE       : "1" = verbose=True

        Parameters
        ----------
        pipeline : str
            "frost" or "eml" など。run_id / trace_id のデフォルト prefix に使用。
        prefix : str
            環境変数プレフィックス。"FROST_" や "EML_" など。
            空文字列の場合は pipeline をアッパーケース + "_" に変換して使用。
        """
        if not prefix:
            prefix = pipeline.upper() + "_"

        run_id = _env_str(f"{prefix}RUN_ID") or _make_run_id(pipeline)
        trace_id = _env_str(f"{prefix}TRACE_ID") or _make_trace_id(pipeline)
        batch_label = _env_str(f"{prefix}BATCH_LABEL") or f"{pipeline}_v1"
        dry_run = _env_bool(f"{prefix}DRY_RUN", default=False)
        verbose = _env_bool(f"{prefix}VERBOSE", default=False)

        return cls(
            run_id=run_id,
            trace_id=trace_id,
            batch_label=batch_label,
            dry_run=dry_run,
            verbose=verbose,
            pipeline=pipeline,
        )

    @classmethod
    def from_args(
        cls,
        args: Any,
        pipeline: str = "frost",
        run_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> "RunContext":
        """
        argparse.Namespace から RunContext を生成する。

        args が持つ属性:
          .dry_run     : bool  (--dry-run フラグ)
          .batch_label : str   (--batch-label)
          .verbose     : bool  (--verbose フラグ)
          .top_k       : int   (frost 専用。RunContext には入れないが後続で使用)

        Parameters
        ----------
        args : argparse.Namespace
        pipeline : str
        run_id : str | None
            明示的に指定する run_id。None の場合は from_env() の値を使用。
        trace_id : str | None
            明示的に指定する trace_id。None の場合は from_env() の値を使用。
        """
        # 環境変数ベースの値を基礎として取得
        base = cls.from_env(pipeline=pipeline)

        return cls(
            run_id=run_id or base.run_id,
            trace_id=trace_id or base.trace_id,
            batch_label=getattr(args, "batch_label", None) or base.batch_label,
            dry_run=bool(getattr(args, "dry_run", False)),
            verbose=bool(getattr(args, "verbose", False)),
            pipeline=pipeline,
        )

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RunContext":
        """dict から RunContext を生成する。"""
        started_at = d.get("started_at")
        if isinstance(started_at, str):
            started_at = datetime.fromisoformat(started_at)
        elif started_at is None:
            started_at = _now_utc()

        return cls(
            run_id=d.get("run_id") or str(uuid.uuid4()),
            trace_id=d.get("trace_id") or str(uuid.uuid4()),
            batch_label=d.get("batch_label", "default"),
            dry_run=bool(d.get("dry_run", False)),
            verbose=bool(d.get("verbose", False)),
            started_at=started_at,
            pipeline=d.get("pipeline", "frost"),
        )

    # ---------------------------------------------------------------------- #
    # シリアライズ
    # ---------------------------------------------------------------------- #

    def to_dict(self) -> Dict[str, Any]:
        """JSON シリアライズ可能な dict を返す。"""
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "batch_label": self.batch_label,
            "dry_run": self.dry_run,
            "verbose": self.verbose,
            "started_at": self.started_at.isoformat(),
            "pipeline": self.pipeline,
        }

    # ---------------------------------------------------------------------- #
    # ユーティリティ
    # ---------------------------------------------------------------------- #

    def log_header(self) -> str:
        """ログ出力用ヘッダー文字列を返す。"""
        return (
            f"[{self.pipeline.upper()}] run_id={self.run_id} "
            f"trace_id={self.trace_id} "
            f"batch_label={self.batch_label} "
            f"dry_run={self.dry_run}"
        )

    def child(
        self,
        run_id: Optional[str] = None,
        batch_label: Optional[str] = None,
    ) -> "RunContext":
        """
        trace_id を引き継いだ子コンテキストを生成する。

        パイプライン内でサブタスクを実行する際に使用する。
        trace_id は必ず親と同一にする (監査追跡のため)。
        """
        return RunContext(
            run_id=run_id or str(uuid.uuid4()),
            trace_id=self.trace_id,  # trace_id は必ず引き継ぐ
            batch_label=batch_label or self.batch_label,
            dry_run=self.dry_run,
            verbose=self.verbose,
            pipeline=self.pipeline,
        )
