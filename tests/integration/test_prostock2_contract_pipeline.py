"""Synthetic JSON → schema → semantic → dependency → decision integration.

No DB, real ProStock2 records, transfer, storage or trading integration is used.
This checks software interoperability, not statistical alpha or Task C durability.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from analytics.python.data_contracts.prostock2_v1 import (
    ContractValidationError, decode_record, evidence_sha256,
    first_eligible_decision, validate_records,
)
from tests.fixtures.prostock2_contract import (
    feature_record, macro_record, missing_record, observation_key,
    ohlcv_record, rejected_record,
)


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "qedschema/contracts/prostock2_audit_v1.schema.json"


@pytest.fixture(scope="module")
def schema_validator():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.mark.parametrize("factory", [ohlcv_record, feature_record, macro_record,
                                     missing_record, rejected_record])
def test_declared_schema_accepts_semantically_valid_synthetic_wire_records(schema_validator, factory):
    record = factory()
    schema_validator.validate(record)
    assert decode_record(json.dumps(record).encode("utf-8")) == record


def test_fractional_timestamp_is_interoperable_with_schema(schema_validator):
    record = ohlcv_record()
    record["available_at"] = "2026-01-05T06:30:03.123456Z"
    schema_validator.validate(record)
    assert decode_record(json.dumps(record).encode("utf-8")) == record


def test_closed_wire_batch_reaches_only_explicit_eligible_japan_decisions(schema_validator):
    parent = ohlcv_record()
    child = feature_record(parent)
    documents = [child, parent]
    decoded = []
    for record in documents:
        wire = json.dumps(record, ensure_ascii=True, sort_keys=False, indent=2).encode("utf-8")
        parsed = json.loads(wire)
        schema_validator.validate(parsed)
        decoded.append(decode_record(wire))
    accepted = validate_records(decoded, expected_observations={observation_key(r) for r in documents})
    assert accepted[0]["availability"]["dependencies"][0]["evidence_sha256"] == evidence_sha256(accepted[1])
    # A same-day synthetic decision occurs before feature computation, so only
    # the parent's already available values can be used at that instant.
    schedule = ["2026-01-05T06:30:03Z", "2026-01-06T00:00:00Z"]
    assert first_eligible_decision(accepted[1], schedule, calendar_version="synthetic-jpx-v1") == schedule[0]
    assert first_eligible_decision(accepted[0], schedule, calendar_version="synthetic-jpx-v1") == schedule[1]


@pytest.mark.parametrize("summer,decisions,expected", [
    (False, ["2026-01-09T00:00:00Z", "2026-01-13T00:00:00Z"], "2026-01-13T00:00:00Z"),
    (True, ["2026-07-02T00:00:00Z", "2026-07-03T00:00:00Z"], "2026-07-03T00:00:00Z"),
])
def test_us_macro_vintage_crosses_explicit_japan_session_gap(schema_validator, summer, decisions, expected):
    record = macro_record(summer=summer)
    schema_validator.validate(record)
    decoded = decode_record(json.dumps(record).encode("utf-8"))
    accepted, = validate_records([decoded], expected_observations={observation_key(record)})
    assert first_eligible_decision(accepted, decisions, calendar_version="synthetic-jpx-v1") == expected
    assert accepted["data_date"] == record["data_date"]


def test_schema_success_does_not_replace_hash_or_dependency_semantics(schema_validator):
    parent = ohlcv_record()
    child = feature_record(parent)
    stale_payload = deepcopy(parent)
    stale_payload["payload"]["close"] = "102.76"
    schema_validator.validate(stale_payload)
    with pytest.raises(ContractValidationError) as caught:
        decode_record(json.dumps(stale_payload).encode("utf-8"))
    assert caught.value.code == "PAYLOAD_HASH_MISMATCH"
    schema_validator.validate(child)
    decoded = decode_record(json.dumps(child).encode("utf-8"))
    with pytest.raises(ContractValidationError) as caught:
        validate_records([decoded])
    assert caught.value.code == "MISSING_DEPENDENCY"


def test_json_schema_integer_equivalence_does_not_authorize_float_wire_version(schema_validator):
    record = ohlcv_record()
    record["schema_version"] = 1.0
    # JSON Schema treats an integral JSON number as integer, while the proposed
    # wire contract explicitly forbids decimal-number representation/coercion.
    schema_validator.validate(record)
    with pytest.raises(ContractValidationError) as caught:
        decode_record(json.dumps(record).encode("utf-8"))
    assert caught.value.code == "NON_WIRE_NUMBER"


def test_unknown_fields_rejected_in_schema_and_semantic_pipeline(schema_validator):
    record = ohlcv_record()
    record["production_token"] = "fixture-only-nonsecret"
    with pytest.raises(ValidationError):
        schema_validator.validate(record)
    with pytest.raises(ContractValidationError) as caught:
        decode_record(json.dumps(record).encode("utf-8"))
    assert caught.value.code == "OBJECT_FIELDS"


def test_missing_observation_stays_explicitly_unusable_after_all_layers(schema_validator):
    record = missing_record()
    schema_validator.validate(record)
    decoded = decode_record(json.dumps(record).encode("utf-8"))
    accepted, = validate_records([decoded], expected_observations={observation_key(record)})
    assert accepted["payload"] is None
    assert first_eligible_decision(accepted, ["2026-01-06T00:00:00Z"], calendar_version="synthetic-jpx-v1") is None
