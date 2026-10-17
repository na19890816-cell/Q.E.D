"""Task B pure-contract boundaries, using synthetic records only.

These tests do not assert Task C file verification, durable idempotency, atomic
storage, operational integrations, statistical alpha, or market-calendar truth.
"""
from copy import deepcopy
import hashlib
import json

import pytest

from analytics.python.data_contracts.prostock2_v1 import (
    ContractValidationError, MAX_SAFE_INTEGER, decode_record, evidence_sha256,
    first_eligible_decision, payload_sha256, validate_record, validate_records,
)
from tests.fixtures.prostock2_contract import (
    copy_with, dependency, feature_record, macro_record, missing_record,
    observation_key, ohlcv_record, rejected_record, rehash,
)


def assert_invalid(record, code):
    with pytest.raises(ContractValidationError) as caught:
        validate_record(record)
    assert caught.value.code == code
    return caught.value


@pytest.mark.parametrize("factory", [ohlcv_record, feature_record, macro_record,
                                     missing_record, rejected_record])
def test_record_roundtrip_preserves_values_and_returns_independent_copy(factory):
    record = factory()
    original = deepcopy(record)
    result = decode_record(json.dumps(record).encode("utf-8"))
    assert result == original == record
    assert result is not record
    assert result["availability"] is not record["availability"]
    result["availability"]["dependencies"].append({"fixture_only": "changed"})
    assert record == original


@pytest.mark.parametrize("version", [True, False, 1.0, "1", 2, None])
def test_schema_version_has_no_integer_coercion(version):
    assert_invalid(copy_with(ohlcv_record(), "schema_version", version), "SCHEMA_VERSION")


@pytest.mark.parametrize("path,value,code", [
    ("payload.volume", True, "INVALID_VOLUME"),
    ("payload.volume", 1.0, "INVALID_VOLUME"),
    ("payload.volume", "123", "INVALID_VOLUME"),
    ("payload.volume", -1, "INVALID_VOLUME"),
    ("payload.volume", MAX_SAFE_INTEGER + 1, "INVALID_VOLUME"),
    ("payload.open", 101.25, "STRING_FORMAT"),
    ("payload.open", True, "STRING_FORMAT"),
    ("payload.open", "NaN", "STRING_FORMAT"),
    ("payload.open", "Infinity", "STRING_FORMAT"),
    ("payload.open", "1e2", "STRING_FORMAT"),
    ("payload.open", "+101", "STRING_FORMAT"),
    ("payload.open", "0101", "STRING_FORMAT"),
    ("payload.open", "1" * 65, "STRING_FORMAT"),
])
def test_quantities_require_exact_declared_types_and_finite_decimal_strings(path, value, code):
    assert_invalid(copy_with(ohlcv_record(), path, value), code)


def test_volume_zero_and_maximum_are_valid_and_prices_keep_declared_precision():
    for volume in (0, MAX_SAFE_INTEGER):
        record = ohlcv_record()
        record["payload"]["volume"] = volume
        rehash(record)
        assert validate_record(record)["payload"]["volume"] == volume
    payload = {"value": "1"}
    assert payload_sha256(payload) != payload_sha256({"value": "1.0"})


@pytest.mark.parametrize("instant,code", [
    ("2026-01-05T06:30:03", "STRING_FORMAT"),
    ("2026-01-05T06:30:03+00:00", "STRING_FORMAT"),
    ("2026-01-05T15:30:03+09:00", "STRING_FORMAT"),
    ("2026-01-05T01:30:03-05:00", "STRING_FORMAT"),
    ("2026-01-05T06:30:03z", "STRING_FORMAT"),
    ("2026-02-30T06:30:03Z", "INVALID_UTC_INSTANT"),
    ("2026-01-05T06:30:60Z", "INVALID_UTC_INSTANT"),
    ("2026-01-05T24:00:00Z", "INVALID_UTC_INSTANT"),
    ("2026-01-05T06:30:03.1234567Z", "STRING_FORMAT"),
])
def test_instant_requires_real_utc_z_time(instant, code):
    assert_invalid(copy_with(ohlcv_record(), "available_at", instant), code)


def test_fractional_utc_time_is_preserved_without_normalization():
    record = ohlcv_record()
    record["available_at"] = "2026-01-05T06:30:03.123456Z"
    assert validate_record(record)["available_at"] == record["available_at"]


@pytest.mark.parametrize("day", ["2026-02-29", "2026-13-01", "0000-01-01"])
def test_data_date_must_exist(day):
    assert_invalid(copy_with(ohlcv_record(), "data_date", day), "INVALID_DATE")


def test_required_and_unknown_fields_are_rejected_at_each_object_boundary():
    for target in ("root", "availability", "payload"):
        for operation in ("remove", "add"):
            record = ohlcv_record()
            obj = record if target == "root" else record[target]
            if operation == "remove":
                del obj[next(iter(obj))]
            else:
                obj["undeclared"] = "fixture-value"
            assert_invalid(record, "OBJECT_FIELDS")


@pytest.mark.parametrize("path,value,code", [
    ("missing_fields", [], "MISSING_FIELDS_MISMATCH"),
    ("reason_codes", [], "REASON_REQUIRED"),
    ("captured_at", None, "CAPTURE_TIME_REQUIRED"),
    ("data_date", "invalid", "STRING_FORMAT"),
])
def test_missing_records_still_need_identity_capture_and_exact_missing_manifest(path, value, code):
    assert_invalid(copy_with(missing_record(), path, value), code)


def test_absent_payload_is_not_filled_with_zero_or_an_invented_hash():
    record = missing_record()
    result = validate_record(record)
    assert result["payload"] is None
    assert result["payload_sha256"] is None
    assert result["available_at"] is None
    invalid = deepcopy(record)
    invalid["payload_sha256"] = "a" * 64
    invalid["missing_fields"].remove("payload_sha256")
    assert_invalid(invalid, "HASH_WITHOUT_PAYLOAD")


@pytest.mark.parametrize("path,value,code", [
    ("status", "missing", "REASON_REQUIRED"),
    ("upstream_gate_state", "unknown", "NOT_READY"),
    ("reason_codes", ["SOMETHING_WRONG"], "NOT_READY"),
    ("reason_codes", ["DUPLICATE", "DUPLICATE"], "DUPLICATE_VALUE"),
])
def test_status_never_silences_missing_or_rejected_evidence(path, value, code):
    assert_invalid(copy_with(ohlcv_record(), path, value), code)


def test_rejected_status_does_not_bypass_bad_timestamp_or_hash():
    record = rejected_record()
    assert_invalid(copy_with(record, "available_at", "not-a-time"), "STRING_FORMAT")
    assert_invalid(copy_with(record, "payload_sha256", "0" * 64), "PAYLOAD_HASH_MISMATCH")


@pytest.mark.parametrize("path,value", [
    ("code_commit", "a" * 39),
    ("code_commit", "A" * 40),
    ("policy_hash", "a" * 64),
    ("market_data_hash", "sha256:" + "a" * 64),
    ("payload_sha256", "A" * 64),
])
def test_provenance_hashes_and_commit_have_explicit_formats(path, value):
    assert_invalid(copy_with(ohlcv_record(), path, value), "STRING_FORMAT")


@pytest.mark.parametrize("path,value,code", [
    ("availability.received_at", "2026-01-05T06:30:00Z", "TIME_ORDER"),
    ("event_time", "2026-01-05T06:30:04Z", "TIME_ORDER"),
    ("captured_at", "2026-01-05T06:30:02Z", "AFTER_CAPTURE"),
    ("data_cutoff", "2026-01-05T06:32:00Z", "AFTER_CAPTURE"),
    ("data_cutoff", "2026-01-05T06:30:02Z", "AFTER_DATA_CUTOFF"),
    ("availability.embargo_until", "2026-01-05T06:30:04Z", "PREMATURE_AVAILABILITY"),
    ("availability.published_at", None, "AVAILABILITY_EVIDENCE_REQUIRED"),
    ("availability.computed_at", "2026-01-05T06:30:02Z", "UNEXPECTED_COMPUTATION"),
])
def test_observable_time_order_and_cutoff_fail_closed(path, value, code):
    assert_invalid(copy_with(ohlcv_record(), path, value), code)


@pytest.mark.parametrize("path,value,code", [
    ("payload.open", "0", "INVALID_OHLC"),
    ("payload.low", "102", "INVALID_OHLC"),
    ("payload.high", "102", "INVALID_OHLC"),
    ("payload.close", "104", "INVALID_OHLC"),
    ("payload.bar_start", "2026-01-05T06:30:00Z", "BAR_ORDER"),
    ("payload.bar_start", "2026-01-05T06:30:01Z", "BAR_ORDER"),
])
def test_ohlcv_range_and_bar_end_availability(path, value, code):
    assert_invalid(copy_with(ohlcv_record(), path, value), code)


def test_payload_hash_and_evidence_hash_have_distinct_explicit_boundaries():
    record = ohlcv_record()
    canonical = lambda obj: json.dumps(obj, sort_keys=True, ensure_ascii=True,
                                       separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert payload_sha256(record["payload"]) == hashlib.sha256(canonical(record["payload"])).hexdigest()
    assert evidence_sha256(record) == hashlib.sha256(canonical(record)).hexdigest()
    changed = deepcopy(record)
    changed["record_id"] = "synthetic-different-id"
    assert payload_sha256(changed["payload"]) == record["payload_sha256"]
    assert evidence_sha256(changed) != evidence_sha256(record)
    changed = deepcopy(record)
    changed["payload"]["close"] = "102.76"
    assert_invalid(changed, "PAYLOAD_HASH_MISMATCH")


@pytest.mark.parametrize("value", [True, 1.0, float("nan"), float("inf"),
                                  MAX_SAFE_INTEGER + 1, {"bad-key": {1: "not-string"}}])
def test_payload_canonicalization_rejects_non_wire_values(value):
    with pytest.raises(ContractValidationError) as caught:
        payload_sha256({"value": value})
    assert caught.value.code == "UNSUPPORTED_JSON_VALUE"


@pytest.mark.parametrize("raw,code", [
    (b'{"run_id":"one","run_id":"two"}', "DUPLICATE_JSON_KEY"),
    (b'{"nested":{"key":1,"key":2}}', "DUPLICATE_JSON_KEY"),
    (b'{"x":1.0}', "NON_WIRE_NUMBER"),
    (b'{"x":NaN}', "NON_WIRE_NUMBER"),
    (b'{"x":Infinity}', "NON_WIRE_NUMBER"),
    (b'\xff', "INVALID_JSON"),
    (b'{incomplete', "INVALID_JSON"),
    (b'[]', "OBJECT_FIELDS"),
    ('{}', "BYTES_REQUIRED"),
])
def test_decoder_rejects_ambiguous_or_non_wire_json(raw, code):
    with pytest.raises(ContractValidationError) as caught:
        decode_record(raw)
    assert caught.value.code == code


def test_error_messages_do_not_echo_submitted_values_keys_or_raw_json():
    secret = "fixture-only-token-do-not-print"
    record = ohlcv_record()
    record[secret] = secret
    error = assert_invalid(record, "OBJECT_FIELDS")
    assert secret not in str(error)
    assert secret not in repr(error)
    with pytest.raises(ContractValidationError) as caught:
        decode_record(('{"x":"' + secret).encode("utf-8"))
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("summer", [False, True])
def test_us_macro_uses_location_timezone_and_correct_seasonal_utc(summer):
    record = macro_record(summer=summer)
    assert validate_record(record) == record
    assert record["event_time"][11:13] == ("12" if summer else "13")


@pytest.mark.parametrize("zone,code", [("EST", "IANA_LOCATION_REQUIRED"),
                                       ("Etc/GMT+5", "IANA_LOCATION_REQUIRED"),
                                       ("Invalid/Nowhere", "UNKNOWN_TIMEZONE")])
def test_macro_disallows_fixed_abbreviations_and_unknown_timezones(zone, code):
    record = macro_record()
    record["payload"]["release_timezone"] = zone
    assert_invalid(rehash(record), code)


def test_macro_rejects_summer_using_fixed_winter_offset_and_nonexistent_dst_clock():
    record = macro_record(summer=True)
    for field in ("event_time", "available_at", "data_cutoff", "captured_at"):
        record[field] = record[field].replace("T12:", "T13:")
    for field in ("published_at", "received_at"):
        record["availability"][field] = record["availability"][field].replace("T12:", "T13:")
    assert_invalid(record, "RELEASE_TIME_MISMATCH")
    record = macro_record()
    for field in ("event_time", "available_at", "data_cutoff", "captured_at"):
        record[field] = record[field].replace("2026-01-09T13:", "2026-03-08T07:")
    for field in ("published_at", "received_at"):
        record["availability"][field] = record["availability"][field].replace("2026-01-09T13:", "2026-03-08T07:")
    record["payload"].update(release_local_date="2026-03-08", release_local_time="02:30:00")
    assert_invalid(rehash(record), "RELEASE_TIME_MISMATCH")


def test_macro_event_is_actual_vintage_publication_not_reference_period():
    record = macro_record()
    assert record["data_date"] != record["payload"]["release_local_date"]
    record["event_time"] = "2026-01-09T13:29:59Z"
    assert_invalid(record, "RELEASE_EVENT_MISMATCH")


@pytest.mark.parametrize("path,value,code", [
    ("availability.dependencies", [], "AVAILABILITY_EVIDENCE_REQUIRED"),
    ("availability.computed_at", None, "AVAILABILITY_EVIDENCE_REQUIRED"),
    ("availability.computed_at", "2026-01-05T06:30:03Z", "TIME_ORDER"),
    ("availability.computed_at", "2026-01-05T06:30:07Z", "TIME_ORDER"),
])
def test_features_need_complete_dependencies_and_computation_times(path, value, code):
    assert_invalid(copy_with(feature_record(), path, value), code)


def test_feature_publication_cannot_precede_its_computation():
    record = feature_record()
    record["availability"]["published_at"] = "2026-01-05T06:30:04Z"
    assert_invalid(record, "TIME_ORDER")


@pytest.mark.parametrize("received,published,code", [
    ("2026-01-05T06:30:06Z", "2026-01-05T06:30:05Z", "TIME_ORDER"),
    ("2026-01-05T06:30:01Z", "2026-01-05T06:30:02Z", "PREMATURE_PUBLICATION"),
])
def test_unknown_compute_time_does_not_hide_known_causal_contradictions(received, published, code):
    record = feature_record()
    record.update(status="rejected", reason_codes=["COMPUTATION_UNVERIFIED"])
    record["availability"].update(computed_at=None, received_at=received, published_at=published)
    assert_invalid(record, code)


def test_final_ohlcv_cannot_be_published_before_bar_close():
    record = ohlcv_record()
    record["availability"]["published_at"] = "2026-01-05T06:29:59Z"
    assert_invalid(record, "BAR_PUBLICATION_ORDER")


def test_feature_cannot_compute_before_dependency_or_depend_on_itself_twice():
    record = feature_record()
    record["availability"]["dependencies"][0]["available_at"] = "2026-01-05T06:30:06Z"
    assert_invalid(record, "PREMATURE_COMPUTATION")
    record = feature_record()
    record["availability"]["dependencies"] *= 2
    assert_invalid(record, "DUPLICATE_OR_SELF_DEPENDENCY")
    record = feature_record()
    record["availability"]["dependencies"][0].update(run_id=record["run_id"], record_id=record["record_id"])
    assert_invalid(record, "DUPLICATE_OR_SELF_DEPENDENCY")


def test_rejected_records_do_not_allow_future_bar_or_dependency_evidence():
    record = rejected_record()
    record["event_time"] = None
    record["missing_fields"] = ["event_time"]
    record["payload"]["bar_start"] = "2026-01-06T00:00:00Z"
    rehash(record)
    assert_invalid(record, "AFTER_CAPTURE")
    record = feature_record()
    record.update(status="rejected", reason_codes=["SOURCE_REJECTED"])
    record["availability"]["dependencies"][0]["available_at"] = "2026-01-06T00:00:00Z"
    assert_invalid(record, "AFTER_CAPTURE")


def test_closed_batch_resolves_dependency_independent_of_input_order_and_preserves_input():
    parent = ohlcv_record()
    child = feature_record(parent)
    records = [child, parent]
    before = deepcopy(records)
    expected = {observation_key(item) for item in records}
    result = validate_records(records, expected_observations=expected)
    assert result == tuple(before)
    assert records == before
    result[0]["payload"]["value"] = "999"
    assert records == before


@pytest.mark.parametrize("mutate,code", [
    ("same", "DUPLICATE_ID"),
    ("evidence", "ID_EVIDENCE_CONFLICT"),
    ("alias", "DUPLICATE_OBSERVATION"),
])
def test_batch_rejects_repeated_ids_conflicting_evidence_and_natural_key_aliases(mutate, code):
    first = ohlcv_record()
    second = deepcopy(first)
    if mutate == "evidence":
        second["captured_at"] = "2026-01-05T06:31:02Z"
    elif mutate == "alias":
        second["record_id"] = "synthetic-alias"
    with pytest.raises(ContractValidationError) as caught:
        validate_records([first, second])
    assert caught.value.code == code


def test_distinct_run_vintage_and_feature_name_are_not_silently_deduplicated():
    base = ohlcv_record()
    rerun = deepcopy(base)
    rerun.update(run_id="synthetic-independent-run", record_id="rerun")
    revision = deepcopy(base)
    revision.update(record_id="revised", source_revision="synthetic-revised-vintage")
    first_feature = feature_record(base)
    second_feature = deepcopy(first_feature)
    second_feature["record_id"] = "second-feature"
    second_feature["payload"]["name"] = "synthetic-second-feature"
    second_feature["measurement_id"] = "synthetic-second-feature"
    rehash(second_feature)
    result = validate_records([base, rerun, revision, first_feature, second_feature])
    assert len(result) == 5


@pytest.mark.parametrize("factory", [ohlcv_record, feature_record, macro_record])
def test_measurement_identity_is_required_and_matches_payload(factory):
    record = factory()
    record["measurement_id"] = "different-measurement"
    assert_invalid(record, "MEASUREMENT_MISMATCH")


def test_missing_features_preserve_measurement_identity_and_coverage():
    first = missing_record()
    first.update(record_type="feature", record_id="missing-feature-1", measurement_id="feature-1")
    second = deepcopy(first)
    second.update(record_id="missing-feature-2", measurement_id="feature-2")
    expected = {observation_key(first), observation_key(second)}
    result = validate_records([first, second], expected_observations=expected)
    assert len(result) == 2
    assert [r["measurement_id"] for r in result] == ["feature-1", "feature-2"]
    with pytest.raises(ContractValidationError) as caught:
        validate_records([first], expected_observations=expected)
    assert caught.value.code == "COVERAGE_MISMATCH"


@pytest.mark.parametrize("mutate,code", [
    ("missing", "MISSING_DEPENDENCY"),
    ("hash", "DEPENDENCY_EVIDENCE_MISMATCH"),
    ("time", "DEPENDENCY_TIME_MISMATCH"),
    ("rejected_parent", "DEPENDENCY_EVIDENCE_MISMATCH"),
])
def test_dependency_closure_requires_ready_matching_parent_evidence_and_time(mutate, code):
    parent = ohlcv_record()
    child = feature_record(parent)
    records = [child, parent]
    if mutate == "missing":
        records = [child]
    elif mutate == "hash":
        child["availability"]["dependencies"][0]["evidence_sha256"] = "0" * 64
    elif mutate == "time":
        child["availability"]["dependencies"][0]["available_at"] = "2026-01-05T06:30:02Z"
    else:
        parent.update(status="rejected", upstream_gate_state="failed", reason_codes=["REJECTED"])
        child["availability"]["dependencies"][0] = dependency(parent)
    with pytest.raises(ContractValidationError) as caught:
        validate_records(records)
    assert caught.value.code == code


def test_cyclic_claims_are_not_accepted_as_a_closed_evidence_batch():
    # A cyclic dependency claim is rejected before hash resolution; no fixed-point
    # evidence hash or fixture monkeypatch is needed to exercise cycle detection.
    parent = feature_record()
    child = deepcopy(parent)
    child["record_id"] = "cyclic-feature"
    child["payload"]["name"] = "cyclic-feature"
    child["measurement_id"] = "cyclic-feature"
    rehash(child)
    parent["availability"]["dependencies"] = [{
        "source_system": "prostock2", "run_id": child["run_id"],
        "record_id": child["record_id"], "evidence_sha256": "a" * 64,
        "available_at": child["available_at"],
    }]
    child["availability"]["dependencies"] = [{
        "source_system": "prostock2", "run_id": parent["run_id"],
        "record_id": parent["record_id"], "evidence_sha256": "b" * 64,
        "available_at": parent["available_at"],
    }]
    for record in (parent, child):
        record["availability"]["computed_at"] = record["available_at"]
        record["availability"]["published_at"] = record["available_at"]
    with pytest.raises(ContractValidationError) as caught:
        validate_records([parent, child])
    assert caught.value.code == "DEPENDENCY_CYCLE"


@pytest.mark.parametrize("extra", [
    ("prostock2", "synthetic-run-1", "ohlcv", "6758.T", "2026-01-05", "synthetic-universe-v1", "ohlcv"),
    ("prostock2", "synthetic-run-1", "ohlcv", "7203.T", "2026-01-06", "synthetic-universe-v1", "ohlcv"),
])
def test_expected_coverage_detects_missing_symbol_or_date(extra):
    record = ohlcv_record()
    with pytest.raises(ContractValidationError) as caught:
        validate_records([record], expected_observations={observation_key(record), extra})
    assert caught.value.code == "COVERAGE_MISMATCH"


def test_missing_status_is_a_present_audit_observation_not_ready_market_data():
    record = missing_record()
    result = validate_records([record], expected_observations={observation_key(record)})
    assert result[0]["status"] == "missing"
    assert first_eligible_decision(result[0], ["2026-01-06T00:00:00Z"], calendar_version="synthetic-jpx-v1") is None


@pytest.mark.parametrize("records", [[], (), {}, None])
def test_batch_requires_nonempty_list(records):
    with pytest.raises(ContractValidationError) as caught:
        validate_records(records)
    assert caught.value.code == "NONEMPTY_BATCH_REQUIRED"


@pytest.mark.parametrize("expected", [{("too", "short")}, {("a", "b", "c", "d", "e", "f", 1)}, [list("abcdefg")]])
def test_expected_coverage_rejects_invalid_key_shape(expected):
    with pytest.raises(ContractValidationError) as caught:
        validate_records([ohlcv_record()], expected_observations=expected)
    assert caught.value.code == "INVALID_EXPECTED_COVERAGE"


def test_decision_selection_uses_explicit_schedule_equality_and_no_holiday_inference():
    record = macro_record()
    friday_open = "2026-01-09T00:00:00Z"
    tuesday_open = "2026-01-13T00:00:00Z"
    # This is an explicitly supplied synthetic holiday-gap schedule, not an
    # assertion that the validator owns or verifies JPX's calendar.
    assert first_eligible_decision(record, [friday_open, tuesday_open], calendar_version="synthetic-jpx-v1") == tuesday_open
    assert first_eligible_decision(record, [record["available_at"]], calendar_version="synthetic-jpx-v1") == record["available_at"]
    assert first_eligible_decision(record, [friday_open], calendar_version="synthetic-jpx-v1") is None


@pytest.mark.parametrize("decisions,code", [
    (["2026-01-06T00:00:00Z", "2026-01-06T00:00:00Z"], "DECISION_ORDER"),
    (["2026-01-07T00:00:00Z", "2026-01-06T00:00:00Z"], "DECISION_ORDER"),
    (["2026-01-06T09:00:00+09:00"], "STRING_FORMAT"),
    (("2026-01-06T00:00:00Z",), "ARRAY_REQUIRED"),
])
def test_decision_schedule_is_strictly_ordered_utc(decisions, code):
    with pytest.raises(ContractValidationError) as caught:
        first_eligible_decision(ohlcv_record(), decisions, calendar_version="synthetic-jpx-v1")
    assert caught.value.code == code


def test_calendar_version_is_required_and_never_inferred():
    with pytest.raises(TypeError):
        first_eligible_decision(ohlcv_record(), [])
    with pytest.raises(ContractValidationError) as caught:
        first_eligible_decision(ohlcv_record(), [], calendar_version="")
    assert caught.value.path == "calendar_version"
