"""
frost_lineage.py
----------------
ADR-002: 系譜ログ (B 設計) — 試行台帳 (Trial Ledger) の純 Python コア

## 役割

DSR (NOTE-001 / frost_dsr.py) に渡す試行回数 N と試行間 SR 分散 V[SR] を、
append-only の試行台帳から as-of で再現可能に集計する。

    family (研究課題)  ← family_key
      └── trial batch  ← n_trials + SR 十分統計量 (count, mean, M2)
    lineage edge       ← parent family → child family (派生元の探索コストを継承)

    N(c, t)    = Σ n_trials   over batches ∈ F(c), recorded_at <= t
    V[SR](c,t) = 並列分散合成 over 同 batch 群
    F(c)       = c の family ∪ 系譜エッジで遡れる全祖先 family

## 設計原則

- 純 Python / 標準ライブラリのみ (決定経路のため numpy 不使用 / statistics 不使用)
- 副作用なし: TrialLedger はメモリ上の不変ビュー。永続化は pg_io/postgres_lineage_bridge.py
- 決定論: formula_hash / family_key / batch_id / snapshot_hash はプロセス非依存
  (組み込み hash() は PYTHONHASHSEED 依存のため使用禁止)
- 過少計上防止: 迷ったら計上する (ADR-002 §4.6)

## 公開 API

    normalize_formula(text) / formula_hash(text)
    make_family_key(horizon, universe, target, terminal_set_hash, **extra)
    make_batch_id(run_id, stage, seq) / make_edge_id(...)
    SharpeStats.from_values(srs) / .merge(other) / .variance
    TrialBatch / LineageEdge
    TrialLedger(batches, edges).snapshot(family_key, as_of, sr_periodicity) -> TrialSnapshot
    TrialSnapshot.to_dsr_kwargs() -> {"n_trials": .., "sr_variance": ..}
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

#: batch_id / edge_id 生成用の固定 namespace (変更禁止: 冪等性が壊れる)
LEDGER_NAMESPACE: uuid.UUID = uuid.uuid5(uuid.NAMESPACE_DNS, "qed.adr002.trial_ledger")

VALID_STAGES: Tuple[str, ...] = ("exhaustive", "gradient", "manual", "frost_eval", "external")
VALID_RELATIONS: Tuple[str, ...] = (
    "mutation", "retrain", "param_tweak", "manual_edit", "ensemble_member",
)

FAMILY_PREFIX = "fam:"
FORMULA_PREFIX = "sha256:"
SNAPSHOT_PREFIX = "snap:"


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _to_utc(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 識別子
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def normalize_formula(text: Optional[str]) -> str:
    """式文字列の正規化: 前後空白除去 + 連続空白を除去 (演算子周りの空白差を吸収)。"""
    if not text:
        return ""
    return _WS_RE.sub("", str(text).strip())


def formula_hash(text: Optional[str]) -> str:
    """
    プロセス非依存の式ハッシュ ("sha256:" + 64 hex)。

    ADR-002 L3: frost_runner の candidate_hash=str(hash(...)) は PYTHONHASHSEED 依存で
    run 間同定に使えないため、系譜ではこちらを用いる。空式は ValueError。
    """
    norm = normalize_formula(text)
    if not norm:
        raise ValueError("formula_hash: 空の式はハッシュできません")
    return FORMULA_PREFIX + _sha256_hex(norm)


def make_family_key(
    horizon: str,
    universe: str,
    target: str,
    terminal_set_hash: str = "",
    **extra: Any,
) -> str:
    """
    family_key = "fam:" + SHA-256(canonical JSON)[:32]。

    同じ問い (horizon × universe × target × terminal_set) の試行は同一 family =
    同一の多重検定母集団とみなす。extra は粒度を細かくしたい場合の追加キー
    (粒度変更は ADR 改訂事項)。
    """
    for name, v in (("horizon", horizon), ("universe", universe), ("target", target)):
        if not str(v or "").strip():
            raise ValueError(f"make_family_key: {name} は必須です")
    spec = {
        "horizon": str(horizon).strip(),
        "universe": str(universe).strip(),
        "target": str(target).strip(),
        "terminal_set_hash": str(terminal_set_hash or "").strip(),
    }
    spec.update(extra)  # 予約キーとの衝突は Python の引数規則で TypeError になる
    return FAMILY_PREFIX + _sha256_hex(_canonical_json(spec))[:32]


def family_spec_dict(
    horizon: str, universe: str, target: str, terminal_set_hash: str = "", **extra: Any,
) -> Dict[str, Any]:
    """make_family_key と同じ入力の辞書表現 (qed_trial_batches.family_spec 用)。"""
    d = {"horizon": horizon, "universe": universe, "target": target,
         "terminal_set_hash": terminal_set_hash}
    d.update(extra)
    return d


def make_batch_id(run_id: str, stage: str, seq: int = 0) -> str:
    """batch_id = UUID5(LEDGER_NAMESPACE, "batch|run_id|stage|seq")。再実行で同一。"""
    if not run_id:
        raise ValueError("make_batch_id: run_id は必須です")
    return str(uuid.uuid5(LEDGER_NAMESPACE, f"batch|{run_id}|{stage}|{int(seq)}"))


def make_edge_id(
    parent_family_key: str,
    child_family_key: str,
    relation: str,
    parent_formula_hash: Optional[str] = None,
    child_formula_hash: Optional[str] = None,
) -> str:
    """edge_id = UUID5(LEDGER_NAMESPACE, 全構成要素)。同一エッジの二重登録を冪等化。"""
    key = "|".join([
        "edge", parent_family_key, child_family_key, relation,
        parent_formula_hash or "", child_formula_hash or "",
    ])
    return str(uuid.uuid5(LEDGER_NAMESPACE, key))


# ---------------------------------------------------------------------------
# SharpeStats — Welford 十分統計量
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SharpeStats:
    """
    試行群 SR の十分統計量 (count, mean, M2)。

    M2 = Σ (x - mean)²。分散 (ddof=1) = M2 / (count - 1)。
    Chan, Golub & LeVeque (1979) の並列合成で batch 間を結合する。
    """
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError(f"SharpeStats.count は非負: {self.count}")
        if self.m2 < 0.0:
            # 浮動小数点誤差による微小負値のみ許容しない (呼び出し側で丸める)
            raise ValueError(f"SharpeStats.m2 は非負: {self.m2}")
        if self.count == 0 and (self.mean != 0.0 or self.m2 != 0.0):
            raise ValueError("SharpeStats: count=0 のとき mean/m2 は 0")

    @classmethod
    def from_values(cls, values: Iterable[Any]) -> "SharpeStats":
        """有限値のみを採用して統計量を作る (None / NaN / Inf / 非数値は除外)。"""
        xs: List[float] = []
        for v in values or []:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(f):
                xs.append(f)
        n = len(xs)
        if n == 0:
            return cls()
        mean = math.fsum(xs) / n
        m2 = math.fsum((x - mean) ** 2 for x in xs)
        return cls(n, mean, max(m2, 0.0))

    def merge(self, other: "SharpeStats") -> "SharpeStats":
        """並列分散合成。結合則・交換則を満たす (浮動小数点誤差内)。"""
        if other.count == 0:
            return self
        if self.count == 0:
            return other
        n = self.count + other.count
        delta = other.mean - self.mean
        mean = self.mean + delta * other.count / n
        m2 = self.m2 + other.m2 + delta * delta * self.count * other.count / n
        return SharpeStats(n, mean, max(m2, 0.0))

    @property
    def variance(self) -> Optional[float]:
        """標本分散 (ddof=1)。count < 2 なら None (frost_dsr は推定量分散へフォールバック)。"""
        if self.count < 2:
            return None
        return self.m2 / (self.count - 1)

    def to_dict(self) -> Dict[str, Any]:
        return {"count": self.count, "mean": self.mean, "m2": self.m2}


def merge_all(stats: Iterable[SharpeStats]) -> SharpeStats:
    acc = SharpeStats()
    for s in stats:
        acc = acc.merge(s)
    return acc


# ---------------------------------------------------------------------------
# TrialBatch / LineageEdge
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrialBatch:
    """qed_trial_batches の 1 行。探索 1 回分の件数と SR 統計。"""
    batch_id: str
    family_key: str
    run_id: str
    stage: str
    n_trials: int
    sr_stats: SharpeStats = field(default_factory=SharpeStats)
    sr_periodicity: str = "daily"
    trace_id: str = ""
    source_type: str = "eml"
    family_spec: Dict[str, Any] = field(default_factory=dict, compare=False, hash=False)
    metadata: Dict[str, Any] = field(default_factory=dict, compare=False, hash=False)
    recorded_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.stage not in VALID_STAGES:
            raise ValueError(f"TrialBatch.stage は {VALID_STAGES} のいずれか: {self.stage!r}")
        if int(self.n_trials) < 0:
            raise ValueError(f"TrialBatch.n_trials は非負: {self.n_trials}")
        if self.sr_stats.count > self.n_trials:
            raise ValueError(
                f"TrialBatch: sr_stats.count={self.sr_stats.count} > n_trials={self.n_trials} "
                "(SR を計算した試行数は総試行数を超えられない)"
            )
        if not self.family_key.startswith(FAMILY_PREFIX):
            raise ValueError(f"TrialBatch.family_key は {FAMILY_PREFIX!r} で始まる必要があります")
        object.__setattr__(self, "recorded_at", _to_utc(self.recorded_at))

    @classmethod
    def create(
        cls,
        family_key: str,
        run_id: str,
        stage: str,
        n_trials: int,
        sharpes: Optional[Iterable[Any]] = None,
        seq: int = 0,
        **kw: Any,
    ) -> "TrialBatch":
        """batch_id を決定論的に生成して TrialBatch を作るファクトリ。"""
        return cls(
            batch_id=make_batch_id(run_id, stage, seq),
            family_key=family_key,
            run_id=run_id,
            stage=stage,
            n_trials=int(n_trials),
            sr_stats=SharpeStats.from_values(sharpes or []),
            **kw,
        )

    def to_row(self) -> Dict[str, Any]:
        """postgres_lineage_bridge 用の行辞書。"""
        return {
            "batch_id": self.batch_id,
            "family_key": self.family_key,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "source_type": self.source_type,
            "stage": self.stage,
            "n_trials": int(self.n_trials),
            "sr_count": self.sr_stats.count,
            "sr_mean": self.sr_stats.mean,
            "sr_m2": self.sr_stats.m2,
            "sr_periodicity": self.sr_periodicity,
            "family_spec": dict(self.family_spec),
            "metadata": dict(self.metadata),
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class LineageEdge:
    """qed_lineage_edges の 1 行。parent family → child family の派生。"""
    edge_id: str
    parent_family_key: str
    child_family_key: str
    relation: str
    parent_formula_hash: Optional[str] = None
    child_formula_hash: Optional[str] = None
    run_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict, compare=False, hash=False)
    recorded_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.relation not in VALID_RELATIONS:
            raise ValueError(f"LineageEdge.relation は {VALID_RELATIONS} のいずれか: {self.relation!r}")
        for k in (self.parent_family_key, self.child_family_key):
            if not k.startswith(FAMILY_PREFIX):
                raise ValueError(f"LineageEdge: family_key は {FAMILY_PREFIX!r} で始まる必要があります")
        object.__setattr__(self, "recorded_at", _to_utc(self.recorded_at))

    @classmethod
    def create(
        cls,
        parent_family_key: str,
        child_family_key: str,
        relation: str,
        parent_formula_hash: Optional[str] = None,
        child_formula_hash: Optional[str] = None,
        **kw: Any,
    ) -> "LineageEdge":
        return cls(
            edge_id=make_edge_id(parent_family_key, child_family_key, relation,
                                 parent_formula_hash, child_formula_hash),
            parent_family_key=parent_family_key,
            child_family_key=child_family_key,
            relation=relation,
            parent_formula_hash=parent_formula_hash,
            child_formula_hash=child_formula_hash,
            **kw,
        )

    def to_row(self) -> Dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "parent_family_key": self.parent_family_key,
            "child_family_key": self.child_family_key,
            "parent_formula_hash": self.parent_formula_hash,
            "child_formula_hash": self.child_formula_hash,
            "relation": self.relation,
            "run_id": self.run_id,
            "metadata": dict(self.metadata),
            "recorded_at": self.recorded_at,
        }


# ---------------------------------------------------------------------------
# TrialSnapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrialSnapshot:
    """
    ある候補 family・時刻 as_of における DSR 入力のスナップショット。

    snapshot_hash は (target family, as_of, 採用 batch_id 集合) の SHA-256 で、
    policy_hash と組にして DSR 判定を完全再現するキー (ADR-002 R4)。
    """
    family_key: str
    as_of: Optional[datetime]
    n_trials: int
    sr_stats: SharpeStats
    families: Tuple[str, ...]
    batch_ids: Tuple[str, ...]
    sr_periodicity: Optional[str]
    excluded_periodicity_batches: int = 0

    @property
    def sr_variance(self) -> Optional[float]:
        return self.sr_stats.variance

    @property
    def is_empty(self) -> bool:
        return self.n_trials == 0

    @property
    def snapshot_hash(self) -> str:
        payload = {
            "family_key": self.family_key,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "batch_ids": sorted(self.batch_ids),
            "sr_periodicity": self.sr_periodicity,
        }
        return SNAPSHOT_PREFIX + _sha256_hex(_canonical_json(payload))

    def to_dsr_kwargs(self) -> Dict[str, Any]:
        """
        DsrGate.check(returns, **snapshot.to_dsr_kwargs()) に渡す引数。

        - 台帳が空なら {} (→ DsrGate は n_trials_source="assumed" + review_required)
        - V[SR] が求まらなければ sr_variance を省略 (→ 推定量分散へフォールバック)
        """
        if self.is_empty:
            return {}
        kw: Dict[str, Any] = {"n_trials": int(self.n_trials)}
        v = self.sr_variance
        if v is not None:
            kw["sr_variance"] = v
        return kw

    def to_dict(self) -> Dict[str, Any]:
        return {
            "family_key": self.family_key,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "n_trials": self.n_trials,
            "sr_stats": self.sr_stats.to_dict(),
            "sr_variance": self.sr_variance,
            "families": list(self.families),
            "batch_count": len(self.batch_ids),
            "sr_periodicity": self.sr_periodicity,
            "excluded_periodicity_batches": self.excluded_periodicity_batches,
            "snapshot_hash": self.snapshot_hash,
        }


# ---------------------------------------------------------------------------
# TrialLedger
# ---------------------------------------------------------------------------

@dataclass
class TrialLedger:
    """
    試行台帳のメモリ上ビュー。batches / edges は append-only とみなす。

    同一 batch_id / edge_id が重複して渡された場合は先勝ちで 1 件とする
    (DB 側 ON CONFLICT DO NOTHING と同じ意味論)。
    """
    batches: List[TrialBatch] = field(default_factory=list)
    edges: List[LineageEdge] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.batches = _dedup_by(self.batches, lambda b: b.batch_id)
        self.edges = _dedup_by(self.edges, lambda e: e.edge_id)

    # -- 追記 -------------------------------------------------------------
    def append_batch(self, batch: TrialBatch) -> bool:
        """追記。既存 batch_id なら False (冪等)。"""
        if any(b.batch_id == batch.batch_id for b in self.batches):
            return False
        self.batches.append(batch)
        return True

    def append_edge(self, edge: LineageEdge) -> bool:
        if any(e.edge_id == edge.edge_id for e in self.edges):
            return False
        self.edges.append(edge)
        return True

    # -- 系譜 -------------------------------------------------------------
    def ancestor_families(self, family_key: str, as_of: Optional[datetime] = None) -> Set[str]:
        """
        family_key 自身 + 系譜エッジで遡れる全祖先 family (循環安全な BFS)。
        as_of 指定時は recorded_at <= as_of のエッジのみ辿る (未来の系譜で過去が変わらない)。
        子孫方向は辿らない。
        """
        t = _to_utc(as_of)
        parents: Dict[str, Set[str]] = {}
        for e in self.edges:
            if t is not None and e.recorded_at is not None and e.recorded_at > t:
                continue
            parents.setdefault(e.child_family_key, set()).add(e.parent_family_key)
        seen: Set[str] = {family_key}
        frontier = [family_key]
        while frontier:
            nxt: List[str] = []
            for f in frontier:
                for p in parents.get(f, ()):
                    if p not in seen:
                        seen.add(p)
                        nxt.append(p)
            frontier = nxt
        return seen

    # -- スナップショット --------------------------------------------------
    def snapshot(
        self,
        family_key: str,
        as_of: Optional[datetime] = None,
        sr_periodicity: Optional[str] = None,
        include_ancestors: bool = True,
    ) -> TrialSnapshot:
        """
        family_key の候補に対する DSR 入力を集計する。

        Parameters
        ----------
        as_of : datetime, optional
            この時刻以前 (<=) に記録された batch / edge のみ採用。
            recorded_at が None の batch は as_of 指定時には採用しない
            (時刻不明の記録で過去判定を変えないため)。
        sr_periodicity : str, optional
            指定時、SR 統計はこの頻度の batch のみ合成する。
            n_trials は頻度に関わらず全 batch を合算する (過少計上防止)。
        include_ancestors : bool
            False なら family 自身のみ (系譜遡及なし)。
        """
        t = _to_utc(as_of)
        fams = self.ancestor_families(family_key, t) if include_ancestors else {family_key}

        n_total = 0
        stats = SharpeStats()
        used: List[str] = []
        excluded = 0
        for b in sorted(self.batches, key=lambda x: x.batch_id):
            if b.family_key not in fams:
                continue
            if t is not None and (b.recorded_at is None or b.recorded_at > t):
                continue
            used.append(b.batch_id)
            n_total += int(b.n_trials)
            if sr_periodicity is not None and b.sr_periodicity != sr_periodicity:
                if b.sr_stats.count > 0:
                    excluded += 1
                continue
            stats = stats.merge(b.sr_stats)

        return TrialSnapshot(
            family_key=family_key,
            as_of=t,
            n_trials=n_total,
            sr_stats=stats,
            families=tuple(sorted(fams)),
            batch_ids=tuple(used),
            sr_periodicity=sr_periodicity,
            excluded_periodicity_batches=excluded,
        )


def _dedup_by(items: Sequence[Any], key) -> List[Any]:
    seen: Set[str] = set()
    out: List[Any] = []
    for it in items or []:
        k = key(it)
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out
