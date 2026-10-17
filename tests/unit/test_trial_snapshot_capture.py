"""Public capture input boundaries and CLI failure paths without a database."""
from datetime import datetime, timedelta, timezone

import pytest

from analytics.python.frost.frost_lineage import SharpeStats, TrialSnapshot, make_family_key
from analytics.python.pg_io import postgres_trial_snapshot_capture as captures
from analytics.python.pg_io.postgres_trial_snapshot_capture import capture_context
from scripts.postgres.capture_trial_snapshot import main


FAMILY = make_family_key("5d", "capture-unit", "fwd")
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def saved_payload():
    """A legal capture includes negative mean SR and all reconstructible fields."""
    parent = make_family_key("5d", "capture-unit-parent", "fwd")
    snapshot = TrialSnapshot(FAMILY, T0, 2, SharpeStats(2, -0.2, 0.02),
                             tuple(sorted((FAMILY, parent))), ("batch-a", "batch-b"), "daily")
    return captures._payload(snapshot, True)


def decode_payload(payload):
    context = captures.capture_context(FAMILY, T0, "daily")
    return captures._decode((captures._capture_id(context), T0, captures._digest(payload), payload))


def test_replay_preserves_legal_payload_and_matches_returned_checksum():
    payload = saved_payload()
    replay = decode_payload(payload)
    assert replay.snapshot.sr_stats.mean == -0.2
    assert captures._canonical(captures._payload(replay.snapshot, replay.include_ancestors)) == captures._canonical(payload)
    assert captures._digest(captures._payload(replay.snapshot, replay.include_ancestors)) == replay.payload_sha256


@pytest.mark.parametrize("path,value", [
    (("version",), True), (("version",), 1.0),
    (("include_ancestors",), 1),
    (("snapshot", "n_trials"), -1), (("snapshot", "n_trials"), True),
    (("snapshot", "n_trials"), 2.0),
    (("snapshot", "sr_stats", "count"), True),
    (("snapshot", "sr_stats", "count"), 2.0),
    (("snapshot", "sr_stats", "count"), 3),
    (("snapshot", "sr_stats", "mean"), True),
    (("snapshot", "sr_stats", "m2"), True),
    (("snapshot", "sr_stats", "m2"), -0.02),
    (("snapshot", "families"), []),
    (("snapshot", "families"), [FAMILY, FAMILY]),
    (("snapshot", "families"), ["fam:invalid"]),
    (("snapshot", "families"), ["fam:" + "0" * 32]),
    (("snapshot", "families"), "not-a-list"),
    (("snapshot", "batch_ids"), ["batch-a", "batch-a"]),
    (("snapshot", "batch_ids"), ["batch-b", "batch-a"]),
    (("snapshot", "batch_ids"), ["", "batch-b"]),
    (("snapshot", "batch_ids"), [1, "batch-b"]),
    (("snapshot", "batch_ids"), "not-a-list"),
    (("snapshot", "batch_count"), 2.0),
    (("snapshot", "batch_count"), 1),
    (("snapshot", "excluded_periodicity_batches"), -1),
    (("snapshot", "excluded_periodicity_batches"), True),
    (("snapshot", "excluded_periodicity_batches"), 3),
    (("snapshot", "sr_variance"), 0.0200001),
    (("snapshot", "sr_variance"), None),
    (("snapshot", "snapshot_hash"), "snapshot:invalid"),
    (("snapshot", "as_of"), "2026-01-01T00:00:00"),
    (("snapshot", "as_of"), "2026-01-01T09:00:00+09:00"),
    (("snapshot", "as_of"), 1),
    (("snapshot", "sr_periodicity"), ""),
    (("snapshot", "sr_stats"), []),
    (("snapshot",), []),
])
def test_replay_rejects_invalid_payload_even_with_matching_checksum(path, value):
    payload = saved_payload()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    data = payload["snapshot"]
    if path == ("snapshot", "batch_ids") and isinstance(value, list) and all(isinstance(v, str) for v in value):
        # Keep derived values consistent: duplicate/ordering validation must not
        # pass merely because an unrelated snapshot hash check rejects the row.
        data["batch_count"] = len(value)
        data["snapshot_hash"] = TrialSnapshot(FAMILY, T0, 0, SharpeStats(), (), tuple(value), "daily").snapshot_hash
    if len(path) == 3 and path[:2] == ("snapshot", "sr_stats") and path[2] in ("count", "m2"):
        stats = data["sr_stats"]
        data["sr_variance"] = stats["m2"] / (stats["count"] - 1) if stats["count"] >= 2 else None
    with pytest.raises(ValueError):
        decode_payload(payload)


def test_replay_requires_canonical_family_order():
    payload = saved_payload()
    payload["snapshot"]["families"].reverse()
    with pytest.raises(ValueError):
        decode_payload(payload)


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("snapshot"),
    lambda p: p.update(extra="unsupported"),
    lambda p: p["snapshot"].pop("n_trials"),
    lambda p: p["snapshot"]["sr_stats"].update(extra="unsupported"),
    lambda p: p["snapshot"].update(extra="unsupported"),
])
def test_replay_rejects_missing_or_unknown_fields(mutate):
    payload = saved_payload()
    mutate(payload)
    with pytest.raises(ValueError):
        decode_payload(payload)


@pytest.mark.parametrize("field,value", [("mean", float("inf")), ("m2", float("nan"))])
def test_replay_rejects_nonfinite_statistics(field, value):
    payload = saved_payload()
    payload["snapshot"]["sr_stats"][field] = value
    with pytest.raises(ValueError):
        captures._decode(("capture:" + "a" * 64, T0, "0" * 64, payload))


@pytest.mark.parametrize("case", ["direct-ancestors", "unfiltered-exclusions", "empty-sr", "no-batches"])
def test_replay_rejects_inconsistent_aggregate_context(case):
    payload = saved_payload()
    data = payload["snapshot"]
    if case == "direct-ancestors":
        payload["include_ancestors"] = False
    elif case == "unfiltered-exclusions":
        data["sr_periodicity"] = None
        data["excluded_periodicity_batches"] = 1
    elif case == "empty-sr":
        data["sr_stats"]["count"] = 0
    else:
        data["batch_ids"] = []
        data["batch_count"] = 0
    with pytest.raises(ValueError):
        decode_payload(payload)


def test_replay_keeps_legal_integer_sr_numbers_and_zero_trial_batch():
    snapshot = TrialSnapshot(FAMILY, T0, 0, SharpeStats(0, 0, 0), (FAMILY,), ("empty-batch",), None)
    payload = captures._payload(snapshot, True)
    context = captures.capture_context(FAMILY, T0)
    replay = captures._decode((captures._capture_id(context), T0, captures._digest(payload), payload))
    assert captures._canonical(captures._payload(replay.snapshot, True)) == captures._canonical(payload)


def test_read_context_normalizes_equivalent_timezone_instants():
    utc = capture_context(FAMILY, T0)
    local = capture_context(FAMILY, T0.astimezone(timezone(timedelta(hours=9))))
    assert utc == local
    assert utc != capture_context(FAMILY, T0, "daily")
    assert utc != capture_context(FAMILY, T0, include_ancestors=False)


@pytest.mark.parametrize("values", [
    {"family_key": "invalid"}, {"as_of": None}, {"as_of": T0.replace(tzinfo=None)},
    {"sr_periodicity": ""}, {"sr_periodicity": 1}, {"include_ancestors": 1},
])
def test_rejects_ambiguous_or_invalid_read_context(values):
    args = {"family_key": FAMILY, "as_of": T0, **values}
    with pytest.raises(ValueError):
        capture_context(**args)


def test_cli_requires_configured_database(monkeypatch, capsys):
    monkeypatch.delenv("QED_PG_DSN", raising=False)
    assert main(["capture", "--family-key", FAMILY, "--as-of", T0.isoformat()]) == 1
    assert "QED_PG_DSN" in capsys.readouterr().err


def test_cli_rejects_naive_cutoff_before_database_access(monkeypatch, capsys):
    monkeypatch.setenv("QED_PG_DSN", "private-invalid-dsn")
    assert main(["capture", "--family-key", FAMILY, "--as-of", "2026-01-01T00:00:00"]) == 1
    error = capsys.readouterr().err
    assert "timezone" in error and "private-invalid-dsn" not in error


@pytest.mark.parametrize("args", [
    ["capture", "--family-key", FAMILY, "--as-of", T0.isoformat()],
    ["show", "--capture-id", "capture:" + "a" * 64],
])
def test_cli_redacts_database_errors(args, monkeypatch, capsys):
    monkeypatch.setenv("QED_PG_DSN", "private-invalid-dsn")
    assert main(args) == 1
    error = capsys.readouterr().err
    assert "snapshot database operation failed" in error
    assert "private-invalid-dsn" not in error
