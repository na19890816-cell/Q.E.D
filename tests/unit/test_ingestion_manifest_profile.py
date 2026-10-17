"""Transport profile and raw source evidence tests, synthetic software only."""
import hashlib
import io
import json
from pathlib import Path

import jsonschema
import pytest

from analytics.python.data_contracts.local_ingestion import LocalAuditStore, validate_manifest
from tests.fixtures.prostock2_contract import observation_key, ohlcv_record


def packet(records, transfer="synthetic-v1", archive=False):
    raw = b"".join(json.dumps(record).encode() + b"\n" for record in records)
    manifest = {
        "transport_schema_version": 1, "source_system": "prostock2",
        "transfer_id": transfer,
        "record_contract": "legacy_audit_archive_v1" if archive else "prostock2_audit_v1",
        "raw_sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw),
        "record_count": len(records),
        "expected_observations": None if archive else [list(observation_key(record)) for record in records],
    }
    return raw, manifest


def profile():
    return json.loads((Path(__file__).resolve().parents[2] /
                       "qedschema/contracts/ingestion_manifest_v1.schema.json").read_text())


@pytest.mark.parametrize("archive", [False, True])
def test_valid_default_profile_passes_schema_and_runtime(archive):
    _, manifest = packet([{"legacy": 1}] if archive else [ohlcv_record()], archive=archive)
    schema = profile()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(manifest, schema)
    assert validate_manifest(manifest) == manifest


@pytest.mark.parametrize("field,value", [
    ("transport_schema_version", True), ("source_system", "other"),
    ("transfer_id", "../escape"), ("raw_sha256", "0" * 63),
    ("transfer_id", "synthetic\n"), ("raw_sha256", "0" * 64 + "\n"),
    ("size_bytes", 0), ("size_bytes", 16 * 1024 * 1024 + 1),
    ("record_count", 10001), ("expected_observations", None),
    ("record_contract", "regime"),
])
def test_invalid_default_profile_rejected_by_both(field, value):
    _, manifest = packet([ohlcv_record()])
    manifest[field] = value
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(manifest, profile())
    with pytest.raises(ValueError, match="INVALID_MANIFEST"):
        validate_manifest(manifest)


def test_missing_original_is_distinct_from_closed_record_dependency_check(tmp_path):
    record = ohlcv_record()
    raw, manifest = packet([record])
    receipt = LocalAuditStore(tmp_path / "store").receive(io.BytesIO(raw), manifest)
    assert receipt["checks"]["dependencies"] is True
    assert receipt["checks"]["source_artifacts"] is False
    assert receipt["missing_source_hashes"] == [record["market_data_hash"]]
    assert "SOURCE_ARTIFACTS_UNVERIFIED" in receipt["reason_codes"]


def test_received_source_raw_hash_resolves_artifact_but_never_grants_research(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    source_raw, source_manifest = packet([{"synthetic_original": "market-data"}], "source", archive=True)
    source_receipt = store.receive(io.BytesIO(source_raw), source_manifest)
    assert source_receipt["state"] == "PROVENANCE_INCOMPLETE"
    record = ohlcv_record()
    record["market_data_hash"] = hashlib.sha256(source_raw).hexdigest()
    raw, manifest = packet([record])
    receipt = store.receive(io.BytesIO(raw), manifest)
    assert receipt["checks"]["source_artifacts"] is True
    assert receipt["missing_source_hashes"] == []
    assert receipt["state"] == "PROVENANCE_INCOMPLETE"
    assert receipt["reason_codes"] == ["INDEPENDENT_PROVENANCE_REVIEW_REQUIRED"]


def test_quarantined_source_does_not_resolve_artifact(tmp_path):
    store = LocalAuditStore(tmp_path / "store")
    source_raw, source_manifest = packet([{"synthetic_original": "market-data"}], "source", archive=True)
    source_manifest["record_count"] = 2
    source_receipt = store.receive(io.BytesIO(source_raw), source_manifest)
    assert source_receipt["state"] == "QUARANTINED"
    record = ohlcv_record()
    record["market_data_hash"] = hashlib.sha256(source_raw).hexdigest()
    raw, manifest = packet([record])
    receipt = store.receive(io.BytesIO(raw), manifest)
    assert receipt["checks"]["source_artifacts"] is False


def test_missing_parent_directory_fails_closed(tmp_path):
    from analytics.python.data_contracts.local_ingestion import StorageError
    with pytest.raises(StorageError, match="STORAGE_UNAVAILABLE"):
        LocalAuditStore(tmp_path / "missing-parent" / "store")


def test_truncated_input_never_reports_unknown_records_as_zero_or_complete(tmp_path):
    raw, manifest = packet([ohlcv_record()])
    prefix = raw[:20]
    store = LocalAuditStore(tmp_path / "store")
    receipt = store.receive(io.BytesIO(prefix), manifest)
    assert receipt["state"] == "QUARANTINED"
    assert receipt["record_count"] is None
    assert receipt["declared_record_count"] == 1
    assert receipt["observed_line_count"] == 1
    assert receipt["stream_complete"] is True
    assert receipt["raw_complete"] is False
    assert receipt["raw_sha256"] == hashlib.sha256(prefix).hexdigest()
