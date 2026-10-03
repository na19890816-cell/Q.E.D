"""
postgres_promotion_evidence.py
------------------------------
昇格前ゲート (G1 DSR / G2 相関) の証拠を PostgreSQL から収集する。

採用済みアルファ (G2 の比較対象):
    knowledge_artifacts のうち
      - status <> 'deprecated'      (Kill / 廃止済みは除外)
      - metadata ? 'promotion_signal'  (シグナルを保存して昇格したもの)
    の metadata.promotion_signal (数値配列) を返す。

    昇格 Bridge は昇格時に候補のシグナルを metadata.promotion_signal として保存するため、
    本機能導入以降に昇格したアルファが順次比較対象に加わる。導入前の artifact は
    シグナルを持たないため比較できない (fetch 結果の skipped_without_signal で可視化)。

比較可能性 (basis):
    シグナルは「位置で揃えた数値列」なので、同じデータ基盤 (family_key = 同じ panel /
    horizon / target / terminal set) 上のもの同士でしか相関に意味がない。
    metadata.promotion_signal_basis.family_key が一致する artifact のみを比較対象とし、
    それ以外は skipped_incomparable として件数を報告する (黙って混ぜない)。

DB 設計原則: psycopg3 / conn は呼び出し側が管理 / 読み取りのみ。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import psycopg

#: knowledge_artifacts.metadata に保存するシグナルのキー
SIGNAL_KEY = "promotion_signal"
#: シグナルの基盤情報 ({"kind": ..., "family_key": ..., "n": ...})
BASIS_KEY = "promotion_signal_basis"
#: 既定のシグナル種別: walk-forward OOS 日次ネットリターン
SIGNAL_KIND_OOS_NET_RETURNS = "walk_forward_oos_net_returns"

_SELECT_SQL = """
    SELECT artifact_id, metadata -> %s, metadata -> %s ->> 'family_key'
      FROM knowledge_artifacts
     WHERE status <> 'deprecated'
       AND artifact_type = ANY(%s)
       AND metadata ? %s
"""

_COUNT_WITHOUT_SIGNAL_SQL = """
    SELECT count(*)
      FROM knowledge_artifacts
     WHERE status <> 'deprecated'
       AND artifact_type = ANY(%s)
       AND NOT (metadata ? %s)
"""


@dataclass
class PromotedSignals:
    signals: Dict[str, List[float]] = field(default_factory=dict)
    skipped_without_signal: int = 0
    skipped_invalid: int = 0
    skipped_incomparable: int = 0
    family_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "family_key": self.family_key,
            "promoted_with_signal": len(self.signals),
            "skipped_without_signal": self.skipped_without_signal,
            "skipped_invalid": self.skipped_invalid,
            "skipped_incomparable": self.skipped_incomparable,
        }


def _to_float_list(v: Any) -> Optional[List[float]]:
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray, memoryview)):
        v = bytes(v).decode("utf-8")
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    if not isinstance(v, list):
        return None
    out: List[float] = []
    for x in v:
        try:
            f = float(x)
        except (TypeError, ValueError):
            return None
        out.append(f if math.isfinite(f) else 0.0)
    return out


def _s(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).decode("utf-8")
    return str(v)


def fetch_promoted_signals(
    conn: psycopg.Connection,
    family_key: Optional[str],
    artifact_types: Optional[List[str]] = None,
    exclude_artifact_ids: Optional[List[str]] = None,
) -> PromotedSignals:
    """
    採用済みアルファのシグナルを取得する。

    family_key が一致する (同じデータ基盤の) artifact のみ返す。
    family_key=None の場合は基盤不問で全件返す (テスト・診断用。本番では指定すること)。
    """
    types = list(artifact_types or ["eml_alpha_candidate"])
    exclude = set(exclude_artifact_ids or [])
    res = PromotedSignals(family_key=family_key)
    with conn.cursor() as cur:
        cur.execute(_SELECT_SQL, (SIGNAL_KEY, BASIS_KEY, types, SIGNAL_KEY))
        for row in cur.fetchall() or []:
            aid = _s(row[0]) or ""
            if aid in exclude:
                continue
            row_fam = _s(row[2]) if len(row) > 2 else None
            if family_key is not None and row_fam != family_key:
                res.skipped_incomparable += 1
                continue
            sig = _to_float_list(row[1])
            if sig is None or len(sig) < 3:
                res.skipped_invalid += 1
                continue
            res.signals[aid] = sig
        cur.execute(_COUNT_WITHOUT_SIGNAL_SQL, (types, SIGNAL_KEY))
        r = cur.fetchone()
        res.skipped_without_signal = int(r[0]) if r and r[0] is not None else 0
    return res


def make_signal_basis(family_key: str, n: int,
                      kind: str = SIGNAL_KIND_OOS_NET_RETURNS) -> Dict[str, Any]:
    return {"kind": kind, "family_key": family_key, "n": int(n)}


def encode_signal(signal: Optional[List[float]], decimals: int = 8) -> Optional[List[float]]:
    """metadata 保存用にシグナルを丸める (JSON サイズ抑制 / NaN→0)。"""
    if signal is None:
        return None
    out = []
    for x in signal:
        try:
            f = float(x)
        except (TypeError, ValueError):
            f = 0.0
        out.append(round(f, decimals) if math.isfinite(f) else 0.0)
    return out
