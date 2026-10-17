"""Persist the first observed ledger snapshot for an explicit read context.

A capture freezes observed inputs; it does not reconstruct commit-time history.
Captures use the public ledger reader without changing decision paths.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

from analytics.python.frost.frost_lineage import SharpeStats, TrialSnapshot
from analytics.python.pg_io.postgres_lineage_bridge import _s, fetch_trial_snapshot


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def capture_context(family_key: str, as_of: datetime, sr_periodicity: str | None = None,
                    include_ancestors: bool = True) -> dict:
    """Require an explicit aware cutoff and normalize equivalent UTC instants."""
    if not isinstance(family_key, str) or not re.fullmatch(r"fam:[0-9a-f]{32}", family_key):
        raise ValueError("family_key: expected fam: followed by 32 lowercase hex characters")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of: explicit timezone required")
    if sr_periodicity is not None and (not isinstance(sr_periodicity, str) or not sr_periodicity.strip()):
        raise ValueError("sr_periodicity: nonempty string or None required")
    if type(include_ancestors) is not bool:
        raise ValueError("include_ancestors: boolean required")
    return {"version": 1, "family_key": family_key,
            "as_of": as_of.astimezone(timezone.utc).isoformat(),
            "sr_periodicity": sr_periodicity, "include_ancestors": include_ancestors}


def _capture_id(context: dict) -> str:
    return "capture:" + _digest(context)


def _payload(snapshot: TrialSnapshot, include_ancestors: bool) -> dict:
    return {"version": 1, "include_ancestors": include_ancestors,
            "snapshot": {**snapshot.to_dict(), "batch_ids": list(snapshot.batch_ids)}}


@dataclass(frozen=True)
class SnapshotCapture:
    capture_id: str
    captured_at: datetime
    payload_sha256: str
    include_ancestors: bool
    snapshot: TrialSnapshot

    def to_dict(self) -> dict:
        return {"capture_id": self.capture_id, "captured_at": self.captured_at.isoformat(),
                "payload_sha256": self.payload_sha256,
                **_payload(self.snapshot, self.include_ancestors)}


_SELECT = """
    SELECT capture_id, captured_at, payload_sha256, payload
    FROM qed_trial_snapshot_captures WHERE capture_id=%s
"""


def _decode(row) -> SnapshotCapture:
    capture_id, captured_at, checksum, payload = row
    # Match the live reader's SQL_ASCII handling: text results may be bytes,
    # while the JSONB loader has already decoded payload into Python objects.
    capture_id, checksum = _s(capture_id), _s(checksum)
    try:
        digest = _digest(payload)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("snapshot capture payload is not finite JSON") from exc
    if digest != checksum:
        raise ValueError("snapshot capture payload checksum mismatch")
    _require_fields(payload, {"version", "include_ancestors", "snapshot"}, "payload")
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise ValueError("unsupported snapshot capture version")
    data = payload["snapshot"]
    _require_fields(data, {"family_key", "as_of", "n_trials", "sr_stats", "sr_variance",
                          "families", "batch_ids", "batch_count", "sr_periodicity",
                          "excluded_periodicity_batches", "snapshot_hash"}, "snapshot")
    _require_fields(data["sr_stats"], {"count", "mean", "m2"}, "sr_stats")
    for field in ("n_trials", "batch_count", "excluded_periodicity_batches"):
        _require_nonnegative_int(data[field], field)
    stats = data["sr_stats"]
    _require_nonnegative_int(stats["count"], "sr_stats.count")
    for field in ("mean", "m2"):
        _require_finite_number(stats[field], "sr_stats." + field)
    if stats["count"] > data["n_trials"]:
        raise ValueError("snapshot capture SR count exceeds N")
    for field in ("families", "batch_ids"):
        _require_sorted_strings(data[field], field)
    if any(not re.fullmatch(r"fam:[0-9a-f]{32}", family) for family in data["families"]):
        raise ValueError("snapshot capture families: invalid family_key")
    if data["family_key"] not in data["families"]:
        raise ValueError("snapshot capture families omit target family")
    if payload["include_ancestors"] is False and data["families"] != [data["family_key"]]:
        raise ValueError("snapshot capture direct-only context contains ancestors")
    if data["batch_count"] != len(data["batch_ids"]):
        raise ValueError("snapshot capture batch count mismatch")
    if not data["batch_ids"] and data["n_trials"] != 0:
        raise ValueError("snapshot capture N without batches")
    if data["excluded_periodicity_batches"] > data["batch_count"]:
        raise ValueError("snapshot capture excluded count exceeds batch count")
    if data["sr_periodicity"] is None and data["excluded_periodicity_batches"] != 0:
        raise ValueError("snapshot capture unfiltered context excludes batches")
    if not isinstance(data["as_of"], str):
        raise ValueError("snapshot capture as_of: timestamp string required")
    snapshot = TrialSnapshot(
        family_key=data["family_key"], as_of=datetime.fromisoformat(data["as_of"]),
        n_trials=data["n_trials"], sr_stats=SharpeStats(**data["sr_stats"]),
        families=tuple(data["families"]), batch_ids=tuple(data["batch_ids"]),
        sr_periodicity=data["sr_periodicity"],
        excluded_periodicity_batches=data["excluded_periodicity_batches"],
    )
    context = capture_context(snapshot.family_key, snapshot.as_of, snapshot.sr_periodicity,
                              payload["include_ancestors"])
    # Python equality treats True == 1 and 1.0 == 1. Compare canonical bytes so
    # the emitted payload has exactly the representation bound by its checksum.
    if (data["as_of"] != context["as_of"] or _capture_id(context) != capture_id
            or _canonical(_payload(snapshot, payload["include_ancestors"])) != _canonical(payload)):
        raise ValueError("snapshot capture context or aggregate mismatch")
    return SnapshotCapture(capture_id, captured_at, checksum, payload["include_ancestors"], snapshot)


def _require_fields(value, fields: set[str], name: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"snapshot capture {name}: unexpected or missing fields")


def _require_nonnegative_int(value, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"snapshot capture {name}: nonnegative integer required")


def _require_finite_number(value, name: str) -> None:
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"snapshot capture {name}: finite number required")


def _require_sorted_strings(value, name: str) -> None:
    if (not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value)
            or value != sorted(set(value))):
        raise ValueError(f"snapshot capture {name}: sorted unique nonempty strings required")


def get_snapshot_capture(conn: psycopg.Connection, capture_id: str) -> SnapshotCapture:
    """Replay saved inputs without querying or aggregating the mutable live ledger."""
    if not isinstance(capture_id, str) or not re.fullmatch(r"capture:[0-9a-f]{64}", capture_id):
        raise ValueError("invalid capture_id")
    row = conn.execute(_SELECT, (capture_id,)).fetchone()
    if row is None:
        raise LookupError("snapshot capture not found")
    return _decode(row)


def capture_trial_snapshot(conn: psycopg.Connection, family_key: str, as_of: datetime,
                           sr_periodicity: str | None = None,
                           include_ancestors: bool = True) -> SnapshotCapture:
    """Save/replay the first capture for (family, cutoff, periodicity, ancestry).

    An idle autocommit connection gives this function its own transaction. All
    live-ledger reads share one REPEATABLE READ visibility snapshot. A racing
    first capture can cause a serialization failure: retry in a new transaction
    to observe the winner, which is immutable and cannot disappear.
    """
    context = capture_context(family_key, as_of, sr_periodicity, include_ancestors)
    capture_id = _capture_id(context)
    if not conn.autocommit or conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError("capture requires an idle autocommit connection; caller transactions are not committed")
    cutoff = datetime.fromisoformat(context["as_of"])
    for attempt in range(2):
        try:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                existing = conn.execute(_SELECT, (capture_id,)).fetchone()
                if existing is not None:
                    return _decode(existing)
                now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                if cutoff > now:
                    raise ValueError("as_of is in the future according to the database clock")
                snapshot = fetch_trial_snapshot(conn, family_key, cutoff, sr_periodicity, include_ancestors)
                # Normalize through JSONB before hashing, including numeric -0.0.
                payload = conn.execute("SELECT %s::jsonb", (_canonical(_payload(snapshot, include_ancestors)),)).fetchone()[0]
                row = conn.execute(
                    "INSERT INTO qed_trial_snapshot_captures(capture_id,payload_sha256,payload) "
                    "VALUES (%s,%s,%s::jsonb) ON CONFLICT (capture_id) DO NOTHING "
                    "RETURNING capture_id,captured_at,payload_sha256,payload",
                    (capture_id, _digest(payload), _canonical(payload)),
                ).fetchone()
                if row is None:
                    raise RuntimeError("snapshot capture was not persisted")
                result = _decode(row)
            return result  # the owned transaction has committed before returning
        except psycopg.errors.SerializationFailure:
            if attempt:
                raise
    raise RuntimeError("snapshot capture retry exhausted")  # pragma: no cover
