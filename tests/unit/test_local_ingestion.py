"""Task C local-store software boundaries using synthetic audit fixtures.

No production DB, live transfer, ProStock2 cron, statistical alpha, or promotion
is exercised. Filesystem fault/concurrency integration has separate evidence.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import io
import json

import pytest

from analytics.python.data_contracts.local_ingestion import (
    Limits, LocalAuditStore, StoreCorrupt, validate_manifest,
)
from tests.fixtures.prostock2_contract import (
    feature_record, missing_record, observation_key, ohlcv_record, rehash,
)


CONTRACT = "prostock2_audit_v1"


def wire(records):
    return b"".join(json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
                    for record in records)


def manifest_for(raw, records, *, transfer_id="synthetic-transfer-1", contract=CONTRACT):
    return {
        "transport_schema_version": 1,
        "source_system": "prostock2",
        "transfer_id": transfer_id,
        "record_contract": contract,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "record_count": len(records),
        "expected_observations": (
            [list(key) for key in sorted({observation_key(record) for record in records})]
            if contract == CONTRACT else None
        ),
    }


def receive(store, records, *, transfer_id="synthetic-transfer-1"):
    raw = wire(records)
    return store.receive(io.BytesIO(raw), manifest_for(raw, records, transfer_id=transfer_id))


def second_record():
    record = ohlcv_record()
    record.update(record_id="synthetic-ohlcv-6758", symbol="6758.T")
    return record


def assert_terminal(receipt, state):
    assert receipt["state"] == state
    assert receipt["state"] != "RESEARCH_ELIGIBLE"
    assert receipt["receipt_id"]
    assert receipt["received_at"].endswith("Z")
    datetime.fromisoformat(receipt["received_at"].replace("Z", "+00:00"))
    assert receipt["state_history"][0] == "RECEIVED"
    assert receipt["state_history"][-1] == state
    return receipt


def assert_quarantined(receipt):
    assert_terminal(receipt, "QUARANTINED")
    assert receipt["reason_codes"]
    assert receipt["new_record_count"] == 0
    assert "RESEARCH_ELIGIBLE" not in receipt["state_history"]
    return receipt


def test_ready_closed_batch_is_validated_but_requires_independent_provenance_review(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    parent = ohlcv_record()
    records = [parent, feature_record(parent)]
    raw = wire(records)
    receipt = store.receive(io.BytesIO(raw), manifest_for(raw, records))
    assert_terminal(receipt, "PROVENANCE_INCOMPLETE")
    assert receipt["state_history"] == [
        "RECEIVED", "INTEGRITY_VERIFIED", "CONTRACT_VALIDATED", "PROVENANCE_INCOMPLETE",
    ]
    assert receipt["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["declared_raw_sha256"] == receipt["raw_sha256"]
    assert receipt["raw_complete"] is True
    assert receipt["record_count"] == 2
    assert receipt["new_record_count"] == 2
    assert receipt["reused_record_count"] == 0
    assert receipt["duplicate_of"] is None
    assert receipt["contract_version"] == CONTRACT
    assert receipt["reason_codes"]
    assert store.receipts() == [receipt]


def test_missing_record_is_archived_as_audit_information_without_becoming_eligible(tmp_path):
    receipt = receive(LocalAuditStore(tmp_path / "store"), [missing_record()])
    assert_terminal(receipt, "PROVENANCE_INCOMPLETE")
    assert "RESEARCH_ELIGIBLE" not in receipt["state_history"]


def test_exact_retransmission_creates_a_distinct_receipt_without_double_registering(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    records = [ohlcv_record()]
    first = receive(store, records)
    second = receive(store, records)
    third = receive(store, records)
    for receipt in (second, third):
        assert_terminal(receipt, "PROVENANCE_INCOMPLETE")
        assert receipt["receipt_id"] != first["receipt_id"]
        assert receipt["duplicate_of"] == first["receipt_id"]
        assert receipt["new_record_count"] == 0
    assert len({r["receipt_id"] for r in store.receipts()}) == 3


def test_same_records_in_a_new_transfer_reuse_existing_evidence(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    first = receive(store, [ohlcv_record()])
    replay = receive(store, [ohlcv_record()], transfer_id="synthetic-transfer-2")
    assert_terminal(replay, "PROVENANCE_INCOMPLETE")
    assert replay["new_record_count"] == 0
    assert replay["reused_record_count"] == 1
    assert replay["raw_sha256"] == first["raw_sha256"]


def test_partial_identical_overlap_reuses_old_and_registers_only_new_records(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    first = receive(store, [ohlcv_record()])
    overlap = receive(store, [second_record(), ohlcv_record()], transfer_id="synthetic-transfer-2")
    assert_terminal(overlap, "PROVENANCE_INCOMPLETE")
    assert overlap["new_record_count"] == 1
    assert overlap["reused_record_count"] == 1
    assert first in store.receipts()


def test_same_id_with_changed_evidence_quarantines_whole_transfer_without_partial_registration(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    first = receive(store, [ohlcv_record()])
    changed = ohlcv_record()
    changed["payload"]["close"] = "102.76"
    rehash(changed)
    bad = receive(store, [second_record(), changed], transfer_id="synthetic-transfer-2")
    assert_quarantined(bad)
    retry_clean = receive(store, [second_record()], transfer_id="synthetic-transfer-3")
    assert_terminal(retry_clean, "PROVENANCE_INCOMPLETE")
    assert retry_clean["new_record_count"] == 1
    assert retry_clean["reused_record_count"] == 0
    assert first in store.receipts()


def test_different_id_cannot_alias_an_existing_natural_observation(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    receive(store, [ohlcv_record()])
    alias = ohlcv_record()
    alias["record_id"] = "synthetic-alias-id"
    assert_quarantined(receive(store, [alias], transfer_id="synthetic-transfer-2"))


def test_transfer_id_cannot_be_reused_with_different_raw_evidence(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    first = receive(store, [ohlcv_record()])
    assert_quarantined(receive(store, [second_record()]))
    clean = receive(store, [second_record()], transfer_id="synthetic-transfer-2")
    assert clean["new_record_count"] == 1
    assert first in store.receipts()


def test_payload_semantics_cannot_be_bypassed_with_a_correct_transport_hash(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    malformed = ohlcv_record()
    malformed["payload"]["close"] = "999999"
    raw = wire([malformed])
    receipt = store.receive(io.BytesIO(raw), manifest_for(raw, [malformed]))
    assert_quarantined(receipt)
    assert receipt["raw_sha256"] == receipt["declared_raw_sha256"]
    assert "INTEGRITY_VERIFIED" in receipt["state_history"]
    assert "CONTRACT_VALIDATED" not in receipt["state_history"]


@pytest.mark.parametrize("field,value", [
    ("raw_sha256", "0" * 64), ("size_bytes", 1), ("record_count", 2),
])
def test_declared_integrity_and_count_must_match_exact_received_bytes(tmp_path, field, value):
    records = [ohlcv_record()]
    raw = wire(records)
    declaration = manifest_for(raw, records)
    declaration[field] = value
    receipt = LocalAuditStore(tmp_path / "store").receive(io.BytesIO(raw), declaration)
    assert_quarantined(receipt)


@pytest.mark.parametrize("bad_raw", [
    b'{"schema_version":1,"schema_version":1}\n',
    b'{"some_value":NaN}\n',
    b'{"some_value":Infinity}\n',
    b'{"some_value":1.0}\n',
    b'\xff\n',
    b'\n',
    b'{"incomplete":\n',
])
def test_bad_jsonl_is_quarantined_even_when_raw_hash_and_size_match(tmp_path, bad_raw):
    declaration = manifest_for(bad_raw, [ohlcv_record()])
    receipt = LocalAuditStore(tmp_path / "store").receive(io.BytesIO(bad_raw), declaration)
    assert_quarantined(receipt)
    assert receipt["raw_sha256"] == receipt["declared_raw_sha256"]


@pytest.mark.parametrize("change", ["no_final_newline", "blank_record", "two_records_on_one_line"])
def test_jsonl_requires_complete_individual_newline_terminated_records(tmp_path, change):
    records = [ohlcv_record()]
    raw = wire(records)
    if change == "no_final_newline":
        raw = raw[:-1]
    elif change == "blank_record":
        raw += b"\n"
    else:
        raw = raw[:-1] + b" " + raw
    receipt = LocalAuditStore(tmp_path / "store").receive(io.BytesIO(raw), manifest_for(raw, records))
    assert_quarantined(receipt)


@pytest.mark.parametrize("missing", ["symbol", "date", "measurement"])
def test_expected_observations_detect_missing_coverage(tmp_path, missing):
    records = [ohlcv_record()]
    raw = wire(records)
    declaration = manifest_for(raw, records)
    expected = list(observation_key(records[0]))
    expected[{"symbol": 3, "date": 4, "measurement": 6}[missing]] = {
        "symbol": "6758.T", "date": "2026-01-06", "measurement": "missing-measurement",
    }[missing]
    declaration["expected_observations"].append(expected)
    receipt = LocalAuditStore(tmp_path / "store").receive(io.BytesIO(raw), declaration)
    assert_quarantined(receipt)


def test_feature_dependencies_must_be_closed_in_the_transfer_not_guessed_from_history(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    parent = ohlcv_record()
    receive(store, [parent])
    receipt = receive(store, [feature_record(parent)], transfer_id="synthetic-transfer-2")
    assert_quarantined(receipt)
    assert "CONTRACT_VALIDATED" not in receipt["state_history"]


@pytest.mark.parametrize("bad_time", ["2026-01-05T06:30:03", "2026-01-05T15:30:03+09:00",
                                       "2026-01-05T06:29:59Z"])
def test_unavailable_or_naive_observations_never_become_contract_validated(tmp_path, bad_time):
    record = ohlcv_record()
    record["available_at"] = bad_time
    receipt = receive(LocalAuditStore(tmp_path / "store"), [record])
    assert_quarantined(receipt)
    assert "CONTRACT_VALIDATED" not in receipt["state_history"]


def test_same_batch_duplicate_records_are_quarantined_not_silently_counted(tmp_path):
    record = ohlcv_record()
    receipt = receive(LocalAuditStore(tmp_path / "store"), [record, deepcopy(record)])
    assert_quarantined(receipt)


def test_failed_transfer_does_not_poison_a_new_valid_retry(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    records = [ohlcv_record()]
    raw = wire(records)
    wrong = manifest_for(raw, records)
    wrong["raw_sha256"] = "0" * 64
    failed = store.receive(io.BytesIO(raw), wrong)
    assert_quarantined(failed)
    valid = receive(store, records, transfer_id="synthetic-corrected-retry")
    assert_terminal(valid, "PROVENANCE_INCOMPLETE")
    assert valid["new_record_count"] == 1
    assert len(store.receipts()) == 2


def test_receipts_and_input_manifest_are_independent_copies(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    records = [ohlcv_record()]
    raw = wire(records)
    declaration = manifest_for(raw, records)
    before = deepcopy(declaration)
    receipt = store.receive(io.BytesIO(raw), declaration)
    receipt["state_history"].append("RESEARCH_ELIGIBLE")
    receipt["reason_codes"].append("MUTATED_BY_CALLER")
    assert declaration == before
    fresh = store.receipts()[0]
    assert "RESEARCH_ELIGIBLE" not in fresh["state_history"]
    assert "MUTATED_BY_CALLER" not in fresh["reason_codes"]


def test_store_reopen_recovers_registry_and_receipts_without_doubling_records(tmp_path):
    root = tmp_path / "store"
    first_store = LocalAuditStore(root)
    first = receive(first_store, [ohlcv_record()])
    second_store = LocalAuditStore(root)
    replay = receive(second_store, [ohlcv_record()], transfer_id="synthetic-reopen-replay")
    assert_terminal(replay, "PROVENANCE_INCOMPLETE")
    assert replay["new_record_count"] == 0
    assert replay["reused_record_count"] == 1
    assert first in second_store.receipts()


def test_normal_recovery_does_not_change_completed_receipts(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    first = receive(store, [ohlcv_record()])
    store.recover()
    assert store.receipts() == [first]


def test_raw_evidence_is_saved_exactly_without_reserialization_or_overwrite(tmp_path):
    root = tmp_path / "store"
    store = LocalAuditStore(root)
    record = ohlcv_record()
    raw = json.dumps(record, indent=2).encode("utf-8") + b"\n"
    # Multiline pretty JSON is not JSONL; preserve even rejected evidence as-is.
    first = store.receive(io.BytesIO(raw), manifest_for(raw, [record]))
    assert_quarantined(first)
    raw_path = root / "blobs" / (hashlib.sha256(raw).hexdigest() + ".raw")
    assert raw_path.read_bytes() == raw
    assert not list((root / "pending").glob("*.part"))
    second = receive(store, [record], transfer_id="synthetic-valid-transfer")
    assert_terminal(second, "PROVENANCE_INCOMPLETE")
    assert raw_path.read_bytes() == raw


def test_whitespace_changes_raw_hash_but_not_the_identity_of_unchanged_records(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    record = ohlcv_record()
    original = receive(store, [record])
    raw = json.dumps(record, separators=(", ", ": ")).encode("utf-8") + b"\n"
    replay = store.receive(io.BytesIO(raw), manifest_for(raw, [record], transfer_id="synthetic-spaced-transfer"))
    assert_terminal(replay, "PROVENANCE_INCOMPLETE")
    assert replay["raw_sha256"] != original["raw_sha256"]
    assert replay["new_record_count"] == 0
    assert replay["reused_record_count"] == 1


@pytest.mark.parametrize("field,value", [
    ("transport_schema_version", True), ("transport_schema_version", 1.0),
    ("size_bytes", True), ("size_bytes", 1.0), ("size_bytes", 0),
    ("record_count", True), ("record_count", 1.0), ("record_count", 0),
    ("source_system", "some-other-system"),
    ("transfer_id", "../../outside"), ("transfer_id", "/tmp/outside"),
    ("raw_sha256", "A" * 64), ("record_contract", "unreviewed_contract"),
    ("expected_observations", None), ("expected_observations", []),
])
def test_manifest_strict_types_and_contract_fail_closed(tmp_path, field, value):
    record = ohlcv_record()
    raw = wire([record])
    declaration = manifest_for(raw, [record])
    declaration[field] = value
    with pytest.raises(ValueError, match="^INVALID_MANIFEST$"):
        validate_manifest(declaration)
    receipt = LocalAuditStore(tmp_path / "store").receive(io.BytesIO(raw), declaration)
    assert_quarantined(receipt)
    assert receipt["reason_codes"] == ["INVALID_MANIFEST"]
    assert receipt["declared_raw_sha256"] is None


@pytest.mark.parametrize("manifest_change", ["missing", "extra", "duplicate_expected", "wrong_key_length"])
def test_manifest_requires_exact_fields_and_unique_seven_field_observations(tmp_path, manifest_change):
    record = ohlcv_record()
    raw = wire([record])
    declaration = manifest_for(raw, [record])
    if manifest_change == "missing":
        del declaration["source_system"]
    elif manifest_change == "extra":
        declaration["research_eligible"] = True
    elif manifest_change == "duplicate_expected":
        declaration["expected_observations"] *= 2
    else:
        declaration["expected_observations"][0].pop()
    assert_quarantined(LocalAuditStore(tmp_path / "store").receive(io.BytesIO(raw), declaration))


def test_entirely_absent_manifest_is_a_durable_quarantined_attempt(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    receipt = store.receive(io.BytesIO(wire([ohlcv_record()])), None)
    assert_quarantined(receipt)
    assert store.receipts() == [receipt]


def test_changed_coverage_manifest_cannot_reuse_a_transfer_identity(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    record = ohlcv_record()
    receive(store, [record])
    raw = wire([record])
    changed = manifest_for(raw, [record])
    extra = list(observation_key(record))
    extra[3] = "6758.T"
    changed["expected_observations"].append(extra)
    receipt = store.receive(io.BytesIO(raw), changed)
    assert_quarantined(receipt)
    assert "TRANSFER_ID_CONFLICT" in receipt["reason_codes"]


def test_changed_record_contract_cannot_reuse_a_transfer_identity(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    record = ohlcv_record()
    receive(store, [record])
    raw = wire([record])
    legacy = manifest_for(raw, [record], contract="legacy_audit_archive_v1")
    receipt = store.receive(io.BytesIO(raw), legacy)
    assert_quarantined(receipt)
    assert "TRANSFER_ID_CONFLICT" in receipt["reason_codes"]


def test_legacy_archive_retains_unknowns_without_inventing_v1_or_research_provenance(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    records = [{"legacy_id": "synthetic-legacy", "oos_sharpe": 0.125,
                "n_trials": None, "source_revision": None}]
    raw = wire(records)
    declaration = manifest_for(raw, records, contract="legacy_audit_archive_v1")
    receipt = store.receive(io.BytesIO(raw), declaration)
    assert_terminal(receipt, "PROVENANCE_INCOMPLETE")
    assert receipt["reason_codes"] == ["LEGACY_PROVENANCE_INCOMPLETE"]
    assert receipt["state_history"] == ["RECEIVED", "INTEGRITY_VERIFIED", "PROVENANCE_INCOMPLETE"]
    assert receipt["record_claims"] == []
    assert receipt["new_record_count"] == 0
    assert receipt["checks"]["record_contract"] is False
    assert receipt["checks"]["coverage"] is False
    assert records[0]["n_trials"] is None
    ack = store.acknowledge(receipt["receipt_id"])
    assert ack["research_eligible"] is False
    assert ack["producer_deletion_authorized"] is False


@pytest.mark.parametrize("raw", [b'{"x":NaN}\n', b'{"x":Infinity}\n', b'{"x":1e999}\n',
                                  b'{"x":1,"x":2}\n', b'{"x":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}\n'],
                         ids=["nan", "infinity", "exponent_overflow", "duplicate_key", "parser_depth"])
def test_legacy_json_must_be_finite_unambiguous_and_parser_bounded(tmp_path, raw):
    declaration = manifest_for(raw, [{}], contract="legacy_audit_archive_v1")
    receipt = LocalAuditStore(tmp_path / "store").receive(io.BytesIO(raw), declaration)
    assert_quarantined(receipt)
    assert receipt["reason_codes"] == ["INVALID_STRICT_JSON"]


def test_limits_require_explicit_positive_integers_without_bool_or_float_coercion():
    for name in ("file_bytes", "line_bytes", "records", "manifest_bytes", "store_bytes", "reserve_bytes"):
        for invalid in (-1, True, 1.0):
            with pytest.raises(ValueError, match="^INVALID_LIMIT$"):
                Limits(**{name: invalid})
        if name != "reserve_bytes":
            with pytest.raises(ValueError, match="^INVALID_LIMIT$"):
                Limits(**{name: 0})
    assert Limits(reserve_bytes=0).reserve_bytes == 0


def test_line_limit_quarantines_oversized_records_before_contract_validation(tmp_path):
    record = ohlcv_record()
    limits = Limits(line_bytes=128)
    raw = wire([record])
    receipt = LocalAuditStore(tmp_path / "store", limits=limits).receive(io.BytesIO(raw), manifest_for(raw, [record]))
    assert_quarantined(receipt)
    assert "LINE_SIZE_LIMIT" in receipt["reason_codes"]


def test_file_limit_captures_bounded_prefix_and_never_acknowledges_it(tmp_path):
    store = LocalAuditStore(tmp_path / "store", limits=Limits(file_bytes=256))
    record = ohlcv_record()
    raw = wire([record])
    receipt = store.receive(io.BytesIO(raw), manifest_for(raw, [record]))
    assert_quarantined(receipt)
    assert receipt["raw_complete"] is False
    assert receipt["stored_bytes"] <= 257
    with pytest.raises(ValueError, match="^ACK_NOT_PERMITTED$"):
        store.acknowledge(receipt["receipt_id"])


def test_ack_is_bound_to_immutable_receipt_and_is_not_permission_to_delete_or_research(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    receipt = receive(store, [ohlcv_record()])
    ack = store.acknowledge(receipt["receipt_id"])
    for field in ("receipt_id", "raw_sha256", "manifest_sha256", "receipt_sha256"):
        assert ack[field] == receipt[field]
    assert ack["size_bytes"] == receipt["stored_bytes"]
    assert ack["confirmed_at"].endswith("Z")
    assert ack["research_eligible"] is False
    assert ack["producer_deletion_authorized"] is False


def test_quarantine_cannot_be_acknowledged_or_promoted(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    receipt = store.receive(io.BytesIO(b"not jsonl\n"), None)
    assert_quarantined(receipt)
    with pytest.raises(ValueError, match="^ACK_NOT_PERMITTED$"):
        store.acknowledge(receipt["receipt_id"])


def test_legacy_eligibility_claim_cannot_grant_research_permission(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    records = [{"state": "RESEARCH_ELIGIBLE", "research_eligible": True,
                "promotion_approved": True}]
    raw = wire(records)
    receipt = store.receive(io.BytesIO(raw), manifest_for(raw, records, contract="legacy_audit_archive_v1"))
    assert_terminal(receipt, "PROVENANCE_INCOMPLETE")
    assert "RESEARCH_ELIGIBLE" not in receipt["state_history"]
    assert store.acknowledge(receipt["receipt_id"])["research_eligible"] is False


def test_raw_corruption_blocks_read_replay_and_ack_without_overwriting_original(tmp_path):
    root = tmp_path / "store"
    store = LocalAuditStore(root)
    receipt = receive(store, [ohlcv_record()])
    raw_path = root / "blobs" / (receipt["raw_sha256"] + ".raw")
    damaged = b"synthetic-damaged-original\n"
    raw_path.write_bytes(damaged)
    for action in (store.receipts, lambda: store.acknowledge(receipt["receipt_id"]),
                   lambda: receive(store, [ohlcv_record()])):
        with pytest.raises(StoreCorrupt, match="^RAW_HASH_MISMATCH$"):
            action()
    assert raw_path.read_bytes() == damaged


def test_reserved_eligible_state_in_saved_metadata_is_rejected_even_with_new_checksum(tmp_path):
    root = tmp_path / "store"
    store = LocalAuditStore(root)
    receipt = receive(store, [ohlcv_record()])
    receipt["state"] = "RESEARCH_ELIGIBLE"
    content = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=True,
                           separators=(",", ":"), allow_nan=False).encode("ascii")
    receipt["receipt_sha256"] = hashlib.sha256(canonical).hexdigest()
    (root / "receipts" / (receipt["receipt_id"] + ".json")).write_text(json.dumps(receipt), encoding="utf-8")
    # This is fail-closed consistency checking, not protection from a malicious
    # store owner who can modify both code and all local evidence.
    with pytest.raises(StoreCorrupt, match="^CORRUPT_RECEIPT$"):
        store.receipts()


def test_ack_rejects_path_traversal_and_unknown_receipts(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    with pytest.raises(ValueError, match="^INVALID_RECEIPT_ID$"):
        store.acknowledge("../../outside")
    with pytest.raises(ValueError, match="^UNKNOWN_RECEIPT$"):
        store.acknowledge("0" * 32)


def test_errors_do_not_echo_submitted_tokens_payload_or_exception_text(tmp_path):
    secret = "synthetic-token-never-log"
    declaration = {secret: secret}
    with pytest.raises(ValueError) as caught:
        validate_manifest(declaration)
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    store = LocalAuditStore(tmp_path / "store")
    receipt = store.receive(io.BytesIO((secret + "\n").encode("utf-8")), declaration)
    assert secret not in json.dumps(receipt)
    assert secret not in json.dumps(store.receipts())
