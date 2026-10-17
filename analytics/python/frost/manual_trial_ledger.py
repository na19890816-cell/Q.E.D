"""ADR-002 S3: validate a complete manual/external trial inventory, without scoring.

Evidence hashes bind the exact bytes and the normalized inventory respectively.
Neither claimed timestamps nor caller-supplied counts become ledger timestamps/N.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from analytics.python.frost.frost_lineage import (
    TrialBatch, family_spec_dict, make_family_key, normalize_formula,
)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}: nonempty string required")
    return value.strip()


def _timestamp(value: Any, name: str) -> datetime:
    try:
        ts = datetime.fromisoformat(_text(value, name).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name}: ISO-8601 timestamp required") from exc
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{name}: explicit timezone required")
    return ts.astimezone(timezone.utc)


def _keys(obj: Any, expected: set[str], name: str) -> None:
    if not isinstance(obj, dict) or set(obj) != expected:
        raise ValueError(f"{name}: expected exactly {sorted(expected)}")


def _unique_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON key: {key}")
        obj[key] = value
    return obj


@dataclass(frozen=True)
class ManualTrialSubmission:
    batch: TrialBatch
    source: str
    research_run_id: str
    evidence_sha256: str
    inventory_sha256: str
    completed_at: datetime
    submitted_by: str


def parse_manual_evidence(
    evidence: bytes, n_trials: int, submitted_by: str,
) -> ManualTrialSubmission:
    """Require all evaluated formulas (including losers), not a survivor-only count.

    N is the number of distinct normalized formulas, as in S1. Missing/hidden
    upstream trials cannot be proven absent by a digest: completeness is an
    explicit producer attestation retained in the evidence.
    """
    if type(n_trials) is not int or not 1 <= n_trials <= 2147483647:
        raise ValueError("n_trials: positive PostgreSQL INTEGER required")
    submitted_by = _text(submitted_by, "submitted_by")
    doc = json.loads(evidence.decode("utf-8"), object_pairs_hook=_unique_keys)
    _keys(doc, {"version", "source", "source_type", "research_run_id", "family",
                "complete", "n_trials", "completed_at", "trials"}, "evidence")
    if type(doc["version"]) is not int or doc["version"] != 1:
        raise ValueError("version: expected 1")
    if doc["complete"] is not True:
        raise ValueError("complete: full trial inventory attestation required")
    if type(doc["n_trials"]) is not int or doc["n_trials"] != n_trials:
        raise ValueError("n_trials: declaration differs from evidence")
    source = _text(doc["source"], "source")
    run_id = _text(doc["research_run_id"], "research_run_id")
    if doc["source_type"] not in ("manual", "external"):
        raise ValueError("source_type: manual or external required")
    spec = doc["family"]
    _keys(spec, {"horizon", "universe", "target", "terminal_set_hash"}, "family")
    spec = {k: _text(v, k) if k != "terminal_set_hash" else v for k, v in spec.items()}
    if not isinstance(spec["terminal_set_hash"], str):
        raise ValueError("terminal_set_hash: string required (empty is allowed)")
    spec["terminal_set_hash"] = spec["terminal_set_hash"].strip()
    family = make_family_key(**spec)
    completed = _timestamp(doc["completed_at"], "completed_at")
    if not isinstance(doc["trials"], list) or not doc["trials"]:
        raise ValueError("trials: nonempty complete inventory required")
    inventory = []
    for trial in doc["trials"]:
        _keys(trial, {"formula", "evaluated_at", "data_cutoff"}, "trial")
        formula = normalize_formula(_text(trial["formula"], "formula"))
        evaluated = _timestamp(trial["evaluated_at"], "evaluated_at")
        cutoff = _timestamp(trial["data_cutoff"], "data_cutoff")
        if cutoff > evaluated or evaluated > completed:
            raise ValueError("trial timestamps must satisfy data_cutoff <= evaluated_at <= completed_at")
        inventory.append({"formula": formula, "evaluated_at": evaluated.isoformat(),
                          "data_cutoff": cutoff.isoformat()})
    actual_n = len({trial["formula"] for trial in inventory})
    if actual_n != n_trials:
        raise ValueError(f"n_trials: declared {n_trials}, inventory has {actual_n} distinct formulas")
    # Labels, ordering, JSON whitespace and formula whitespace cannot evade dedup.
    unique_rows = sorted({json.dumps(t, sort_keys=True, separators=(",", ":")) for t in inventory})
    canonical = json.dumps({"family": spec, "completed_at": completed.isoformat(),
                            "trials": unique_rows},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    raw_hash = hashlib.sha256(evidence).hexdigest()
    inventory_hash = hashlib.sha256(canonical.encode()).hexdigest()
    identity = json.dumps([source, run_id], separators=(",", ":"), ensure_ascii=True)
    batch = TrialBatch.create(
        family, "manual:" + hashlib.sha256(identity.encode()).hexdigest(), "manual", actual_n,
        source_type=doc["source_type"], family_spec=family_spec_dict(**spec),
        metadata={"manual_ledger_version": 1, "source": source, "research_run_id": run_id,
                  "evidence_sha256": raw_hash, "inventory_sha256": inventory_hash,
                  "submitted_by": submitted_by, "complete": True,
                  "completed_at": completed.isoformat(), "inventory_rows": len(inventory)},
    )
    # No SR statistics: external evidence is not guaranteed to be selection-free.
    return ManualTrialSubmission(batch, source, run_id, raw_hash, inventory_hash,
                                 completed, submitted_by)
