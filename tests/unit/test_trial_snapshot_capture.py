"""Public capture input boundaries and CLI failure paths without a database."""
from datetime import datetime, timedelta, timezone

import pytest

from analytics.python.frost.frost_lineage import make_family_key
from analytics.python.pg_io.postgres_trial_snapshot_capture import capture_context
from scripts.postgres.capture_trial_snapshot import main


FAMILY = make_family_key("5d", "capture-unit", "fwd")
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


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
