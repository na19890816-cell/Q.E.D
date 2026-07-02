"""
base_writer.py
--------------
PostgreSQL writer 群の共通骨格。

解決する負債 (D6):
  - _safe_float / _safe_float_opt / _safe_json が
    analytics/python/io/ と analytics/python/pg_io/ の計 5 モジュールに
    ほぼ同一実装で散在している。
  - dry_run ガード & ログ出力のパターンが各 writer で独自実装されている。
  - conn.execute() 直接呼び出し vs with conn.cursor() as cur の不統一。
  - _now() UTC ユーティリティが 2 モジュールに重複している。

設計原則:
  - pure Python / numpy 禁止 (ADR-001 Phase 7 まで)
  - psycopg3 のみ使用 (%s プレースホルダー)
  - BaseWriter は抽象クラス。具体 writer はこれを継承する
  - 後方互換: 既存モジュールの公開 API シグネチャを変更しない
    (既存の _safe_float 等はモジュールレベル関数として残す)
  - conn は呼び出し側が管理。BaseWriter はコンストラクタで受け取るだけ

公開インターフェース:
  BaseWriter              — 共通骨格 (抽象基底クラス)
  safe_float(v)           — NaN/Inf → 0.0
  safe_float_opt(v)       — NaN/Inf → None
  safe_json(obj)          — 再帰 NaN/Inf サニタイズ → JSON 文字列
  now_utc()               — UTC datetime.now()
  DryRunSkipped           — dry_run ガードが書き込みをスキップした例外クラス

変更履歴:
  Phase 6 (2025-Q2): D6 負債解消として新規作成
"""
from __future__ import annotations

import json
import logging
import math
from abc import ABC
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg

__all__ = [
    "BaseWriter",
    "safe_float",
    "safe_float_opt",
    "safe_json",
    "now_utc",
    "DryRunSkipped",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------

class DryRunSkipped(Exception):
    """
    dry_run=True のとき DB 書き込みをスキップしたことを示す。

    通常は例外として raise しない。
    _guard_dry_run() が呼び出し元に「スキップした」ことを通知したい場合に限り raise する。
    通常は _guard_dry_run() の戻り値 (bool) でスキップを検知する。
    """


# ---------------------------------------------------------------------------
# モジュールレベル サニタイズ ユーティリティ
# (後方互換: 各 writer の _safe_float / _safe_json を置き換える際に
#  `from analytics.python.pg_io.base_writer import safe_float as _safe_float`
#  と 1 行インポート変更するだけで移行可能)
# ---------------------------------------------------------------------------

def safe_float(v: Any) -> float:
    """
    NaN / Inf を 0.0 に変換する。

    PostgreSQL NUMERIC / FLOAT 列へ安全に渡すためのユーティリティ。
    変換不可能な値は 0.0 を返す。

    Examples
    --------
    >>> safe_float(float("nan"))
    0.0
    >>> safe_float(float("inf"))
    0.0
    >>> safe_float(1.5)
    1.5
    >>> safe_float(None)
    0.0
    """
    try:
        f = float(v)
        return 0.0 if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return 0.0


def safe_float_opt(v: Any) -> Optional[float]:
    """
    NaN / Inf を None に変換する。NULL 許容 NUMERIC / FLOAT 列向け。

    Examples
    --------
    >>> safe_float_opt(float("nan")) is None
    True
    >>> safe_float_opt(None) is None
    True
    >>> safe_float_opt(1.5)
    1.5
    """
    if v is None:
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def safe_json(obj: Any, *, default: str = "str") -> str:
    """
    NaN / Inf を None に変換したうえで JSON シリアライズする。

    PostgreSQL JSONB 列への書き込みに使用する。
    datetime は ISO 8601 文字列に変換する。

    Parameters
    ----------
    obj : Any
        シリアライズ対象オブジェクト。dict / list / scalar すべて可。
    default : str
        json.dumps の default 関数の種別。
        "str"  → str() でフォールバック (デフォルト)
        "none" → シリアライズ不能な値を None にする

    Examples
    --------
    >>> import json, math
    >>> json.loads(safe_json({"x": float("nan"), "y": 1.0}))
    {'x': None, 'y': 1.0}
    """
    def _sanitize(v: Any) -> Any:
        if isinstance(v, float):
            return None if (math.isnan(v) or math.isinf(v)) else v
        if isinstance(v, dict):
            return {k: _sanitize(vv) for k, vv in v.items()}
        if isinstance(v, list):
            return [_sanitize(x) for x in v]
        if isinstance(v, datetime):
            return v.isoformat()
        return v

    if default == "none":
        def _fallback(o: Any) -> None:  # type: ignore[misc]
            return None
        return json.dumps(_sanitize(obj), default=_fallback)

    return json.dumps(_sanitize(obj), default=str)


def now_utc() -> datetime:
    """
    UTC の datetime.now() を返す。

    各 writer で `datetime.now(timezone.utc)` を直接呼ぶ代わりに使用する。
    テスト時にモンキーパッチで差し替え可能。
    """
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# BaseWriter
# ---------------------------------------------------------------------------

class BaseWriter(ABC):
    """
    PostgreSQL writer 群の抽象基底クラス。

    全 writer が共有する以下の責務を提供する:

    1. conn 管理
       - コンストラクタで psycopg.Connection を受け取り self.conn に保持
       - conn の開閉は呼び出し側の責務

    2. dry_run ガード
       - self.dry_run == True の場合、_guard_dry_run() が True を返す
       - 呼び出し元は ``if self._guard_dry_run(label): return`` とすれば良い

    3. サニタイズ ユーティリティ (モジュール関数への委譲)
       - self._sf(v)       → safe_float(v)
       - self._sfo(v)      → safe_float_opt(v)
       - self._sj(obj)     → safe_json(obj)
       - self._now()       → now_utc()

    4. ログ ヘルパー
       - self._log_skip(label)  — dry_run スキップ時のログ
       - self._log_ok(label)    — 書き込み成功時のログ
       - self._log_err(label, exc) — エラー時のログ

    5. 安全 commit
       - self._commit()     — conn.commit() を実行; dry_run 時はスキップ

    使い方
    ------
    具体 writer はこのクラスを継承し、公開メソッドを実装する。
    サニタイズは `self._sf()` / `self._sj()` を使う。

    class FrostWriter(BaseWriter):
        def __init__(self, conn, *, dry_run=False):
            super().__init__(conn, dry_run=dry_run, writer_name="FrostWriter")

        def upsert_run(self, output):
            if self._guard_dry_run("upsert_run"):
                return
            ...
            self._commit()
    """

    def __init__(
        self,
        conn: psycopg.Connection,
        *,
        dry_run: bool = False,
        writer_name: str = "",
    ) -> None:
        """
        Parameters
        ----------
        conn : psycopg.Connection
            psycopg3 接続オブジェクト。接続の開閉は呼び出し側が管理する。
        dry_run : bool
            True の場合、_guard_dry_run() が True を返し、
            _commit() がスキップされる。
        writer_name : str
            ログ出力のプレフィックス用。例: "FrostWriter", "EventStudyWriter"
        """
        self.conn: psycopg.Connection = conn
        self.dry_run: bool = dry_run
        self._writer_name: str = writer_name or self.__class__.__name__

    # ------------------------------------------------------------------
    # サニタイズ ユーティリティ (モジュール関数への薄い委譲)
    # ------------------------------------------------------------------

    @staticmethod
    def _sf(v: Any) -> float:
        """NaN / Inf → 0.0。safe_float() の短縮エイリアス。"""
        return safe_float(v)

    @staticmethod
    def _sfo(v: Any) -> Optional[float]:
        """NaN / Inf → None。safe_float_opt() の短縮エイリアス。"""
        return safe_float_opt(v)

    @staticmethod
    def _sj(obj: Any) -> str:
        """safe_json() の短縮エイリアス。"""
        return safe_json(obj)

    @staticmethod
    def _now() -> datetime:
        """now_utc() の短縮エイリアス。"""
        return now_utc()

    # ------------------------------------------------------------------
    # dry_run ガード
    # ------------------------------------------------------------------

    def _guard_dry_run(self, label: str = "") -> bool:
        """
        dry_run=True の場合に True を返しログを出力する。

        呼び出し例::

            def upsert_run(self, output):
                if self._guard_dry_run("upsert_run"):
                    return
                # ... 実際の書き込み処理

        Parameters
        ----------
        label : str
            スキップされた操作名。ログに含まれる。

        Returns
        -------
        bool
            dry_run=True のとき True、通常は False。
        """
        if self.dry_run:
            self._log_skip(label)
            return True
        return False

    # ------------------------------------------------------------------
    # ログ ヘルパー
    # ------------------------------------------------------------------

    def _log_skip(self, label: str, extra: str = "") -> None:
        """dry_run スキップ時の INFO ログ。"""
        msg = f"[DRY_RUN] {self._writer_name}.{label} skipped"
        if extra:
            msg += f": {extra}"
        logger.info(msg)

    def _log_ok(self, label: str, extra: str = "") -> None:
        """書き込み成功時の DEBUG ログ。"""
        msg = f"{self._writer_name}.{label} OK"
        if extra:
            msg += f": {extra}"
        logger.debug(msg)

    def _log_err(self, label: str, exc: Exception) -> None:
        """エラー時の WARNING ログ。"""
        logger.warning(
            "%s.%s error: %s: %s",
            self._writer_name, label, type(exc).__name__, exc,
        )

    # ------------------------------------------------------------------
    # 安全 commit
    # ------------------------------------------------------------------

    def _commit(self) -> None:
        """
        conn.commit() を呼び出す。

        dry_run=True の場合はスキップする。
        commit 失敗時は例外をそのまま伝搬する。
        """
        if self.dry_run:
            return
        self.conn.commit()

    # ------------------------------------------------------------------
    # execute ヘルパー
    # ------------------------------------------------------------------

    def _execute(
        self,
        sql: Any,
        params: Any = None,
        *,
        many: bool = False,
    ) -> None:
        """
        SQL を実行する。

        conn.execute() / conn.cursor().executemany() の使い方を統一する。

        Parameters
        ----------
        sql : str or psycopg.sql.Composable
            実行する SQL。
        params : tuple / list of tuples, optional
            クエリパラメータ。many=True の場合は list of tuples。
        many : bool
            True の場合は cursor.executemany() を使う (バッチ INSERT 用)。
        """
        if many:
            with self.conn.cursor() as cur:
                cur.executemany(sql, params or [])
        else:
            self.conn.execute(sql, params)
