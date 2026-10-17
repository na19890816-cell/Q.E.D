"""Synthetic audit records for software validation only, never alpha evidence.

No production history, parent schema, market-data source, or exchange calendar is
represented by these fixtures. Hashes and IDs are deliberately synthetic.
"""
from __future__ import annotations

from copy import deepcopy

from analytics.python.data_contracts.prostock2_v1 import evidence_sha256, payload_sha256


def rehash(record):
    """Recompute only the payload declaration after a deliberate fixture edit."""
    record["payload_sha256"] = (
        None if record["payload"] is None else payload_sha256(record["payload"])
    )
    return record


def ohlcv_record():
    return rehash({
        "schema_version": 1,
        "run_id": "synthetic-run-1",
        "record_id": "synthetic-ohlcv-7203",
        "source_system": "prostock2",
        "record_type": "ohlcv",
        "symbol": "7203.T",
        "data_date": "2026-01-05",
        "universe_version": "synthetic-universe-v1",
        "measurement_id": "ohlcv",
        "event_time": "2026-01-05T06:30:00Z",
        "captured_at": "2026-01-05T06:31:01Z",
        "available_at": "2026-01-05T06:30:03Z",
        "data_cutoff": "2026-01-05T06:31:00Z",
        "source_revision": "synthetic-initial",
        "code_commit": "1" * 40,
        "policy_hash": "sha256:" + "2" * 64,
        "market_data_hash": "3" * 64,
        "payload_sha256": None,
        "upstream_gate_state": "passed",
        "status": "ready",
        "reason_codes": [],
        "missing_fields": [],
        "availability": {
            "published_at": "2026-01-05T06:30:01Z",
            "received_at": "2026-01-05T06:30:02Z",
            "computed_at": None,
            "embargo_until": None,
            "dependencies": [],
        },
        "payload": {
            "open": "101.25", "high": "103.50", "low": "100.00",
            "close": "102.75", "volume": 123456,
            "bar_start": "2026-01-05T00:00:00Z",
            "calendar_version": "synthetic-jpx-v1",
        },
    })


def dependency(record):
    return {
        "source_system": record["source_system"],
        "run_id": record["run_id"],
        "record_id": record["record_id"],
        "evidence_sha256": evidence_sha256(record),
        "available_at": record["available_at"],
    }


def feature_record(parent=None):
    parent = ohlcv_record() if parent is None else parent
    record = ohlcv_record()
    record.update(record_id="synthetic-feature-7203", record_type="feature", measurement_id="synthetic-return")
    record["available_at"] = "2026-01-05T06:30:06Z"
    record["availability"].update(
        published_at="2026-01-05T06:30:05Z",
        received_at="2026-01-05T06:30:04Z",
        computed_at="2026-01-05T06:30:05Z",
        dependencies=[dependency(parent)],
    )
    record["payload"] = {"name": "synthetic-return", "value": "0.25", "unit": "ratio"}
    return rehash(record)


def macro_record(*, summer=False):
    record = ohlcv_record()
    day, hour = ("2026-07-02", "12") if summer else ("2026-01-09", "13")
    record.update(
        run_id="synthetic-macro-run", record_id="synthetic-macro-cpi",
        record_type="us_macro", symbol="US:CPI", data_date="2025-12-01",
        measurement_id="synthetic-cpi",
        event_time=f"{day}T{hour}:30:00Z",
        available_at=f"{day}T{hour}:30:03Z",
        data_cutoff=f"{day}T{hour}:31:00Z",
        captured_at=f"{day}T{hour}:31:01Z",
    )
    record["availability"].update(
        published_at=f"{day}T{hour}:30:00Z",
        received_at=f"{day}T{hour}:30:02Z",
    )
    record["payload"] = {
        "series_id": "synthetic-cpi", "value": "-0.125", "unit": "percent",
        "release_timezone": "America/New_York",
        "release_local_date": day, "release_local_time": "08:30:00",
    }
    return rehash(record)


def missing_record():
    record = ohlcv_record()
    fields = (
        "event_time", "available_at", "data_cutoff", "source_revision",
        "code_commit", "policy_hash", "market_data_hash", "payload_sha256", "payload",
    )
    record.update({field: None for field in fields})
    record.update(status="missing", upstream_gate_state="unknown",
                  reason_codes=["SOURCE_UNAVAILABLE"], missing_fields=list(fields))
    record["availability"].update(published_at=None, received_at=None)
    return record


def rejected_record():
    record = ohlcv_record()
    record.update(status="rejected", upstream_gate_state="failed",
                  reason_codes=["UPSTREAM_GATE_FAILED"])
    return record


def copy_with(record, path, value):
    copied = deepcopy(record)
    target = copied
    names = path.split(".")
    for name in names[:-1]:
        target = target[name]
    target[names[-1]] = value
    return copied


def observation_key(record):
    return tuple(record[field] for field in (
        "source_system", "run_id", "record_type", "symbol", "data_date", "universe_version", "measurement_id",
    ))
