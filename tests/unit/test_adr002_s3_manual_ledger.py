"""S3 evidence contract and CLI validation, without a database."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from analytics.python.frost.manual_trial_ledger import parse_manual_evidence
from analytics.python.frost.frost_lineage import make_family_key
from scripts.postgres.register_manual_trials import main


def evidence_doc():
    return {
        "version": 1, "source": "research-desk", "source_type": "external",
        "research_run_id": "run-1", "complete": True, "n_trials": 2,
        "completed_at": "2026-01-02T12:00:00Z",
        "family": {"horizon": "5d", "universe": "IT", "target": "fwd",
                   "terminal_set_hash": ""},
        "trials": [{"formula": formula, "evaluated_at": "2026-01-02T11:00:00Z",
                    "data_cutoff": "2026-01-01T00:00:00Z"} for formula in ("r1 + r5", "r20")],
    }


def raw(doc):
    return json.dumps(doc).encode()


@pytest.mark.parametrize("source_type", ["manual", "external"])
def test_full_inventory_exact_hash_and_existing_family_definition(source_type):
    doc = evidence_doc()
    doc["source_type"] = source_type
    submission = parse_manual_evidence(raw(doc), 2, "operator")
    batch = submission.batch
    assert batch.n_trials == 2
    assert batch.stage == "manual" and batch.source_type == source_type
    assert submission.evidence_sha256 == hashlib.sha256(raw(doc)).hexdigest()
    assert batch.family_key == make_family_key(**doc["family"])
    assert batch.metadata["evidence_sha256"] == submission.evidence_sha256
    assert batch.metadata["submitted_by"] == "operator"
    assert batch.metadata["complete"] is True
    assert batch.recorded_at is None and batch.sr_stats.count == 0


def test_duplicates_of_same_formula_count_once():
    doc = evidence_doc()
    original = parse_manual_evidence(raw(doc), 2, "operator")
    doc["trials"].append(dict(doc["trials"][0], formula="r1+r5"))
    duplicate = parse_manual_evidence(raw(doc), 2, "operator")
    assert duplicate.batch.n_trials == 2
    assert duplicate.inventory_sha256 == original.inventory_sha256


def test_semantic_hash_blocks_relabeling_reordering_and_whitespace():
    a = evidence_doc()
    b = copy.deepcopy(a)
    b.update(source="another-desk", research_run_id="new-name", source_type="manual")
    b["trials"].reverse()
    b["trials"][1]["formula"] = "r1+r5"
    b["completed_at"] = "2026-01-02T21:00:00+09:00"
    x = parse_manual_evidence(raw(a), 2, "operator")
    y = parse_manual_evidence(json.dumps(b, indent=2).encode(), 2, "operator")
    assert x.inventory_sha256 == y.inventory_sha256
    assert x.evidence_sha256 != y.evidence_sha256
    assert x.batch.batch_id != y.batch.batch_id


def test_identity_independent_of_n_and_evidence():
    a = evidence_doc()
    b = copy.deepcopy(a)
    b["n_trials"] = 1
    b["trials"] = b["trials"][:1]
    assert parse_manual_evidence(raw(a), 2, "a").batch.batch_id == parse_manual_evidence(raw(b), 1, "b").batch.batch_id


@pytest.mark.parametrize("n", [0, -1, True, 1.5, "2", 2147483648])
def test_invalid_declared_n(n):
    with pytest.raises(ValueError, match="n_trials"):
        parse_manual_evidence(raw(evidence_doc()), n, "operator")


@pytest.mark.parametrize("path,value", [
    (("complete",), False), (("complete",), 1), (("n_trials",), 1),
    (("n_trials",), 2.0), (("version",), True), (("version",), 2),
    (("source",), " "), (("research_run_id",), ""), (("source_type",), "eml"),
    (("trials",), []), (("trials",), {}), (("family", "horizon"), ""),
    (("family", "terminal_set_hash"), None), (("completed_at",), "2026-01-02T12:00:00"),
    (("completed_at",), "yesterday"),
])
def test_rejects_invalid_manifest(path, value):
    doc = evidence_doc()
    obj = doc
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value
    with pytest.raises(ValueError):
        parse_manual_evidence(raw(doc), 2, "operator")


@pytest.mark.parametrize("n", [1, 3])
def test_rejects_under_and_overstatement(n):
    doc = evidence_doc()
    doc["n_trials"] = n
    with pytest.raises(ValueError, match="distinct formulas"):
        parse_manual_evidence(raw(doc), n, "operator")


@pytest.mark.parametrize("key,value", [
    ("data_cutoff", "2026-01-02T11:00:01Z"),
    ("evaluated_at", "2026-01-03T00:00:00Z"),
    ("evaluated_at", "2026-01-02T11:00:00"),
    ("formula", "  "),
])
def test_rejects_lookahead_or_invalid_trial(key, value):
    doc = evidence_doc()
    doc["trials"][0][key] = value
    with pytest.raises(ValueError):
        parse_manual_evidence(raw(doc), 2, "operator")


@pytest.mark.parametrize("extra", ["recorded_at", "as_of", "sr_stats", "typo"])
def test_no_timestamp_or_sr_override(extra):
    doc = evidence_doc()
    doc[extra] = "2020-01-01T00:00:00Z"
    with pytest.raises(ValueError):
        parse_manual_evidence(raw(doc), 2, "operator")


def test_duplicate_json_keys_rejected():
    content = raw(evidence_doc()).replace(b'"n_trials": 2', b'"n_trials": 1, "n_trials": 2')
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_manual_evidence(content, 2, "operator")


def test_cli_validates_without_dsn(tmp_path, monkeypatch, capsys):
    path = tmp_path / "evidence.json"
    path.write_bytes(raw(evidence_doc()))
    monkeypatch.delenv("QED_PG_DSN", raising=False)
    assert main(["--evidence", str(path), "--n-trials", "2", "--submitted-by", "operator"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "validated_only" and output["n_trials"] == 2
    assert main(["--evidence", str(path), "--n-trials", "1", "--submitted-by", "operator"]) == 1
    assert "n_trials" in capsys.readouterr().err
    assert main(["--evidence", str(path), "--n-trials", "2", "--submitted-by", "operator", "--write-db"]) == 1
    assert "QED_PG_DSN" in capsys.readouterr().err


def test_cli_redacts_database_errors(tmp_path, monkeypatch, capsys):
    path = tmp_path / "evidence.json"
    path.write_bytes(raw(evidence_doc()))
    monkeypatch.setenv("QED_PG_DSN", "invalid-private-connection-string")
    assert main(["--evidence", str(path), "--n-trials", "2", "--submitted-by", "operator", "--write-db"]) == 1
    error = capsys.readouterr().err
    assert "database registration failed" in error
    assert "invalid-private-connection-string" not in error
