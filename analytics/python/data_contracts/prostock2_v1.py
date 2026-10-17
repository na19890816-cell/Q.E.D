"""Strict, side-effect-free validation of the proposed ProStock2 audit wire format.

No database, network, storage, clock, trading, or FROST calls are made. UTC
instants use Z on the wire. Financial quantities are exact decimal strings.
ZoneInfo conversion requires the deployment's timezone database to be pinned.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_SAFE_INTEGER = 9007199254740991
NULLABLE = frozenset({"event_time", "available_at", "data_cutoff", "source_revision",
                      "code_commit", "policy_hash", "market_data_hash", "payload_sha256", "payload"})
FIELDS = NULLABLE | {"schema_version", "run_id", "record_id", "source_system", "record_type",
                     "symbol", "data_date", "universe_version", "measurement_id", "captured_at", "upstream_gate_state",
                     "status", "reason_codes", "missing_fields", "availability"}
ID = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}"
HASH = r"[0-9a-f]{64}"
INSTANT = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z"
DECIMAL = r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"


class ContractValidationError(ValueError):
    """Stable error code/path only: never include submitted values or raw JSON."""

    def __init__(self, code: str, path: str):
        self.code, self.path = code, path
        super().__init__(f"{code} at {path}")


def _fail(code, path):
    raise ContractValidationError(code, path)


def _keys(value, fields, path):
    if type(value) is not dict or set(value) != set(fields):
        _fail("OBJECT_FIELDS", path)


def _pattern(value, pattern, path, limit=None):
    if type(value) is not str or (limit is not None and len(value) > limit) or not re.fullmatch(pattern, value):
        _fail("STRING_FORMAT", path)
    return value


def _identifier(value, path):
    return _pattern(value, ID, path)


def _instant(value, path):
    _pattern(value, INSTANT, path)
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ContractValidationError("INVALID_UTC_INSTANT", path) from None


def _date(value, path):
    _pattern(value, r"[0-9]{4}-[0-9]{2}-[0-9]{2}", path)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ContractValidationError("INVALID_DATE", path) from None


def _decimal(value, path):
    _pattern(value, DECIMAL, path, 64)
    return Decimal(value)


def _unique_strings(value, path):
    if type(value) is not list:
        _fail("ARRAY_REQUIRED", path)
    for item in value:
        _identifier(item, path)
    if len(set(value)) != len(value):
        _fail("DUPLICATE_VALUE", path)


def _json_types(value):
    if value is None or type(value) is str:
        return
    if type(value) is int and abs(value) <= MAX_SAFE_INTEGER:
        return
    if type(value) is list:
        for item in value:
            _json_types(item)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():
            _json_types(item)
        return
    _fail("UNSUPPORTED_JSON_VALUE", "$")


def _canonical(value):
    try:
        _json_types(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("ascii")
    except (RecursionError, UnicodeError, TypeError, ValueError) as exc:
        if isinstance(exc, ContractValidationError):
            raise
        raise ContractValidationError("INVALID_JSON", "$") from None


def payload_sha256(payload: dict) -> str:
    """Hash canonical payload JSON, not the enclosing record or transfer bytes."""
    if type(payload) is not dict:
        _fail("OBJECT_REQUIRED", "payload")
    return hashlib.sha256(_canonical(payload)).hexdigest()


def evidence_sha256(record: dict) -> str:
    """Bind every validated envelope field, including payload_sha256."""
    return hashlib.sha256(_canonical(validate_record(record))).hexdigest()


def _payload(record, stamps):
    payload, kind = record["payload"], record["record_type"]
    if payload is None:
        if record["payload_sha256"] is not None:
            _fail("HASH_WITHOUT_PAYLOAD", "payload_sha256")
        return
    if kind == "ohlcv":
        _keys(payload, {"open", "high", "low", "close", "volume", "bar_start", "calendar_version"}, "payload")
        prices = {k: _decimal(payload[k], "payload." + k) for k in ("open", "high", "low", "close")}
        if any(v <= 0 for v in prices.values()) or not (
                prices["low"] <= min(prices["open"], prices["close"])
                <= max(prices["open"], prices["close"]) <= prices["high"]):
            _fail("INVALID_OHLC", "payload")
        if type(payload["volume"]) is not int or not 0 <= payload["volume"] <= MAX_SAFE_INTEGER:
            _fail("INVALID_VOLUME", "payload.volume")
        start = _instant(payload["bar_start"], "payload.bar_start")
        if start > stamps["captured_at"]:
            _fail("AFTER_CAPTURE", "payload.bar_start")
        if stamps["event_time"] is not None and start >= stamps["event_time"]:
            _fail("BAR_ORDER", "payload.bar_start")
        if stamps["published_at"] is not None and (
                start >= stamps["published_at"] or
                (stamps["event_time"] is not None and stamps["event_time"] > stamps["published_at"])):
            _fail("BAR_PUBLICATION_ORDER", "availability.published_at")
        _identifier(payload["calendar_version"], "payload.calendar_version")
    elif kind == "feature":
        _keys(payload, {"name", "value", "unit"}, "payload")
        _identifier(payload["name"], "payload.name")
        if record["measurement_id"] != payload["name"]:
            _fail("MEASUREMENT_MISMATCH", "measurement_id")
        _identifier(payload["unit"], "payload.unit")
        _decimal(payload["value"], "payload.value")
    else:
        _keys(payload, {"series_id", "value", "unit", "release_timezone", "release_local_date", "release_local_time"}, "payload")
        _identifier(payload["series_id"], "payload.series_id")
        if record["measurement_id"] != payload["series_id"]:
            _fail("MEASUREMENT_MISMATCH", "measurement_id")
        _identifier(payload["unit"], "payload.unit")
        _decimal(payload["value"], "payload.value")
        _pattern(payload["release_timezone"], r"[A-Za-z0-9_+./-]+", "payload.release_timezone", 128)
        # Abbreviations such as EST cannot express DST. Require an IANA location.
        if "/" not in payload["release_timezone"] or payload["release_timezone"].startswith("Etc/"):
            _fail("IANA_LOCATION_REQUIRED", "payload.release_timezone")
        try:
            zone = ZoneInfo(payload["release_timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            raise ContractValidationError("UNKNOWN_TIMEZONE", "payload.release_timezone") from None
        local_date = _date(payload["release_local_date"], "payload.release_local_date")
        _pattern(payload["release_local_time"], r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?", "payload.release_local_time")
        local_clock = time.fromisoformat(payload["release_local_time"])
        published = stamps["published_at"]
        if published is not None:
            try:
                actual = published.astimezone(zone)
            except (OverflowError, ValueError):
                raise ContractValidationError("INVALID_RELEASE_TIME", "payload.release_local_time") from None
            if actual.date() != local_date or actual.time() != local_clock:
                _fail("RELEASE_TIME_MISMATCH", "payload.release_local_time")
            if stamps["event_time"] is not None and stamps["event_time"] != published:
                _fail("RELEASE_EVENT_MISMATCH", "event_time")
    if record["payload_sha256"] != payload_sha256(payload):
        _fail("PAYLOAD_HASH_MISMATCH", "payload_sha256")


def validate_record(document: dict) -> dict:
    """Return an independent validated copy; never fill, infer, or mutate values.

    A valid missing/rejected record is audit information, not usable market data.
    Shape validation is implemented without a runtime JSON Schema dependency.
    """
    _keys(document, FIELDS, "$")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        _fail("SCHEMA_VERSION", "schema_version")
    for field in ("run_id", "record_id", "universe_version", "measurement_id"):
        _identifier(document[field], field)
    _pattern(document["symbol"], r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}", "symbol")
    _date(document["data_date"], "data_date")
    for field, choices in (("source_system", ("prostock2",)),
                           ("record_type", ("ohlcv", "feature", "us_macro")),
                           ("status", ("ready", "missing", "rejected")),
                           ("upstream_gate_state", ("passed", "failed", "not_evaluated", "unknown"))):
        if type(document[field]) is not str or document[field] not in choices:
            _fail("ENUM_VALUE", field)
    if document["record_type"] == "ohlcv" and document["measurement_id"] != "ohlcv":
        _fail("MEASUREMENT_MISMATCH", "measurement_id")
    _unique_strings(document["reason_codes"], "reason_codes")
    _unique_strings(document["missing_fields"], "missing_fields")
    nulls = {k for k in NULLABLE if document[k] is None}
    if set(document["missing_fields"]) != nulls:
        _fail("MISSING_FIELDS_MISMATCH", "missing_fields")
    ready = document["status"] == "ready"
    if ready and (nulls or document["reason_codes"] or document["upstream_gate_state"] != "passed"):
        _fail("NOT_READY", "status")
    if not ready and not document["reason_codes"]:
        _fail("REASON_REQUIRED", "reason_codes")
    if document["status"] == "missing" and not nulls:
        _fail("MISSING_VALUE_REQUIRED", "missing_fields")
    for field, pattern in (("source_revision", ID), ("code_commit", r"(?:[0-9a-f]{40}|[0-9a-f]{64})"),
                           ("policy_hash", r"sha256:[0-9a-f]{64}"),
                           ("market_data_hash", HASH), ("payload_sha256", HASH)):
        if document[field] is not None:
            _pattern(document[field], pattern, field)
    stamps = {k: _instant(document[k], k) if document[k] is not None else None
              for k in ("event_time", "captured_at", "available_at", "data_cutoff")}
    if stamps["captured_at"] is None:
        _fail("CAPTURE_TIME_REQUIRED", "captured_at")
    availability = document["availability"]
    _keys(availability, {"published_at", "received_at", "computed_at", "embargo_until", "dependencies"}, "availability")
    for field in ("published_at", "received_at", "computed_at", "embargo_until"):
        stamps[field] = _instant(availability[field], "availability." + field) if availability[field] is not None else None
    deps = availability["dependencies"]
    if type(deps) is not list:
        _fail("ARRAY_REQUIRED", "availability.dependencies")
    dependency_times, identities = [], set()
    for dep in deps:
        _keys(dep, {"source_system", "run_id", "record_id", "evidence_sha256", "available_at"}, "availability.dependencies")
        if dep["source_system"] != "prostock2":
            _fail("ENUM_VALUE", "availability.dependencies.source_system")
        for field in ("run_id", "record_id"):
            _identifier(dep[field], "availability.dependencies." + field)
        _pattern(dep["evidence_sha256"], HASH, "availability.dependencies.evidence_sha256")
        key = _identity(dep)
        if key in identities or key == _identity(document):
            _fail("DUPLICATE_OR_SELF_DEPENDENCY", "availability.dependencies")
        identities.add(key)
        dependency_times.append(_instant(dep["available_at"], "availability.dependencies.available_at"))
    if any(t > stamps["captured_at"] for t in dependency_times):
        _fail("AFTER_CAPTURE", "availability.dependencies.available_at")
    if document["record_type"] != "feature" and (deps or stamps["computed_at"] is not None):
        _fail("UNEXPECTED_COMPUTATION", "availability")
    if ready and (stamps["published_at"] is None or stamps["received_at"] is None or
                  (document["record_type"] == "feature" and (stamps["computed_at"] is None or not deps))):
        _fail("AVAILABILITY_EVIDENCE_REQUIRED", "availability")
    ordering = [("event_time", "available_at")]
    if document["record_type"] == "feature":
        ordering.extend((("received_at", "computed_at"), ("computed_at", "published_at"),
                         ("received_at", "published_at")))
    else:
        ordering.append(("published_at", "received_at"))
    for left, right in ordering:
        if stamps[left] is not None and stamps[right] is not None and stamps[left] > stamps[right]:
            _fail("TIME_ORDER", "availability")
    for field in ("event_time", "available_at", "data_cutoff", "published_at", "received_at", "computed_at"):
        if stamps[field] is not None and stamps[field] > stamps["captured_at"]:
            _fail("AFTER_CAPTURE", field)
    requirements = dependency_times + [stamps[k] for k in ("published_at", "received_at", "computed_at", "embargo_until") if stamps[k] is not None]
    if stamps["available_at"] is not None and any(t > stamps["available_at"] for t in requirements):
        _fail("PREMATURE_AVAILABILITY", "available_at")
    if stamps["computed_at"] is not None and any(t > stamps["computed_at"] for t in dependency_times):
        _fail("PREMATURE_COMPUTATION", "availability.computed_at")
    if document["record_type"] == "feature" and stamps["published_at"] is not None and any(
            t > stamps["published_at"] for t in dependency_times):
        _fail("PREMATURE_PUBLICATION", "availability.published_at")
    if ready and stamps["available_at"] > stamps["data_cutoff"]:
        _fail("AFTER_DATA_CUTOFF", "available_at")
    _payload(document, stamps)
    _canonical(document)
    return copy.deepcopy(document)


def _identity(record):
    return record["source_system"], record["run_id"], record["record_id"]


def coverage_key(record) -> tuple:
    return tuple(record[k] for k in ("source_system", "run_id", "record_type", "symbol", "data_date", "universe_version", "measurement_id"))


def validate_records(records: list, expected_observations=None) -> tuple[dict, ...]:
    """Validate a closed dependency batch and optional authoritative coverage set.

    Repeated IDs inside a batch are rejected, including identical duplicates.
    Cross-transfer idempotency, atomic files, receipts and quarantine are Task C.
    """
    if type(records) is not list or not records:
        _fail("NONEMPTY_BATCH_REQUIRED", "records")
    output = tuple(validate_record(record) for record in records)
    by_id, natural, hashes = {}, set(), {}
    for record in output:
        key = _identity(record)
        digest = hashlib.sha256(_canonical(record)).hexdigest()
        if key in by_id:
            _fail("DUPLICATE_ID" if hashes[key] == digest else "ID_EVIDENCE_CONFLICT", "records")
        observation = coverage_key(record) + (record["source_revision"],)
        if observation in natural:
            _fail("DUPLICATE_OBSERVATION", "records")
        natural.add(observation)
        by_id[key], hashes[key] = record, digest
    graph = {}
    for key, record in by_id.items():
        graph[key] = set()
        for dep in record["availability"]["dependencies"]:
            parent_key = _identity(dep)
            parent = by_id.get(parent_key)
            if parent is None:
                _fail("MISSING_DEPENDENCY", "records")
            graph[key].add(parent_key)
    # Kahn's algorithm avoids recursion-depth dependence for long feature chains.
    incoming = {key: len(parents) for key, parents in graph.items()}
    children = {key: [] for key in graph}
    for child, parents in graph.items():
        for parent in parents:
            children[parent].append(child)
    queue = [key for key, count in incoming.items() if count == 0]
    visited = 0
    while queue:
        visited += 1
        for child in children[queue.pop()]:
            incoming[child] -= 1
            if incoming[child] == 0:
                queue.append(child)
    if visited != len(graph):
        _fail("DEPENDENCY_CYCLE", "records")
    for record in output:
        for dep in record["availability"]["dependencies"]:
            parent_key = _identity(dep)
            parent = by_id[parent_key]
            if parent["status"] != "ready" or hashes[parent_key] != dep["evidence_sha256"]:
                _fail("DEPENDENCY_EVIDENCE_MISMATCH", "records")
            if _instant(parent["available_at"], "available_at") != _instant(dep["available_at"], "available_at"):
                _fail("DEPENDENCY_TIME_MISMATCH", "records")
    if expected_observations is not None:
        if type(expected_observations) not in (set, frozenset):
            _fail("INVALID_EXPECTED_COVERAGE", "expected_observations")
        expected = expected_observations
        if any(type(k) is not tuple or len(k) != 7 or any(type(v) is not str for v in k) for k in expected):
            _fail("INVALID_EXPECTED_COVERAGE", "expected_observations")
        if expected != {coverage_key(r) for r in output}:
            _fail("COVERAGE_MISMATCH", "expected_observations")
    return output


def decode_record(raw: bytes) -> dict:
    """Decode strict UTF-8 JSON without duplicate keys or non-wire numbers."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("DUPLICATE_JSON_KEY", "$")
            result[key] = value
        return result

    def reject_number(_):
        _fail("NON_WIRE_NUMBER", "$")

    if type(raw) is not bytes:
        _fail("BYTES_REQUIRED", "$")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                              parse_float=reject_number, parse_constant=reject_number)
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ContractValidationError):
            raise
        raise ContractValidationError("INVALID_JSON", "$") from None
    return validate_record(document)


def first_eligible_decision(record, decision_times, *, calendar_version) -> str | None:
    """Use caller-supplied, versioned exchange decisions; never guess holidays/DST.

    The caller must provide the actual eligible session/decision schedule. A
    version label alone does not verify that schedule or the publisher's claims.
    """
    _identifier(calendar_version, "calendar_version")
    valid = validate_record(record)
    if type(decision_times) is not list:
        _fail("ARRAY_REQUIRED", "decision_times")
    instants = [_instant(t, "decision_times") for t in decision_times]
    if any(a >= b for a, b in zip(instants, instants[1:])):
        _fail("DECISION_ORDER", "decision_times")
    if valid["status"] != "ready":
        return None
    available = _instant(valid["available_at"], "available_at")
    return next((text for text, instant in zip(decision_times, instants) if instant >= available), None)
