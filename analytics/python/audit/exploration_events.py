"""Durable observation of new research attempts; never an additional DSR ledger.

One immutable file per event, serialized with flock and published without overwrite.
The filesystem must provide local POSIX flock/link/fsync semantics. Hashes detect
damage; they do not prove a producer's completeness or protect against its owner.
"""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone
from typing import Any


class AuditError(RuntimeError):
    """Fail closed before evaluating more candidates after an audit failure."""


def audit_value(value: Any) -> Any:
    """JSON evidence encoding preserves missing/nonfinite values without imputation."""
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise AuditError("naive audit datetime")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return {"$nonfinite": "nan" if math.isnan(value) else "+inf" if value > 0 else "-inf"}
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise AuditError("audit object keys must be strings")
        return {k: audit_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [audit_value(v) for v in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    # NumPy scalars are common fitness outputs. Do not stringify opaque objects.
    if hasattr(value, "item"):
        return audit_value(value.item())
    raise AuditError("unsupported audit evidence type")


def _blob(value: Any) -> bytes:
    return json.dumps(audit_value(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def evidence_hash(value: Any) -> str:
    return hashlib.sha256(_blob(value)).hexdigest()


def contains_nonfinite(value: Any) -> bool:
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    if isinstance(value, dict):
        return any(contains_nonfinite(v) for v in value.values())
    if isinstance(value, (tuple, list)):
        return any(contains_nonfinite(v) for v in value)
    if isinstance(value, float):
        return not math.isfinite(value)
    if hasattr(value, "item"):
        return contains_nonfinite(value.item())
    return False


def _no_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if any(word in key.lower() for word in ("password", "secret", "token", "dsn", "authorization")):
                raise AuditError("credential-like settings key is prohibited")
            _no_secrets(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _no_secrets(item)


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise AuditError("duplicate JSON key")
        out[k] = v
    return out


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_unlocked(path: Path) -> list[dict]:
    events = []
    previous = None
    for i, file in enumerate(sorted(path.glob("*.json")), 1):
        if file.name != f"{i:012d}.json" or file.is_symlink():
            raise AuditError("event sequence is damaged")
        try:
            event = json.loads(file.read_bytes(), object_pairs_hook=_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(AuditError("nonfinite JSON")))
            checksum = event.pop("event_sha256")
            if (event["sequence"] != i or event["previous_sha256"] != previous
                    or evidence_hash(event) != checksum):
                raise AuditError("event checksum chain is damaged")
            event["event_sha256"] = checksum
            if event["schema_version"] != 1 or event["context"] != (events[0]["context"] if events else event["context"]):
                raise AuditError("audit context changed")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AuditError("invalid audit event") from exc
        previous = checksum
        events.append(event)
    return events


def read_events(path: str | Path) -> list[dict]:
    path = Path(path)
    with (path / ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        events = _read_unlocked(path)
        try:
            # A prior producer may have lost its acknowledgement after link but
            # before directory fsync. Reverification alone is not durability.
            for i in range(1, len(events) + 1):
                fd = os.open(path / f"{i:012d}.json", os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            _sync_dir(path)
            _sync_dir(path.parent)
        except OSError as exc:
            raise AuditError("audit durability recovery failed") from exc
        return events


def inspect_attempt(path: str | Path) -> dict:
    """Report incomplete trials, without inventing failed evaluations or unknown N."""
    events = read_events(path)
    if not events or events[0]["event_type"] != "RUN_STARTED":
        raise AuditError("missing research start")
    started = {e["data"]["trial_id"] for e in events if e["event_type"] == "TRIAL_STARTED"}
    finished = {e["data"]["trial_id"] for e in events if e["event_type"] == "TRIAL_FINISHED"}
    scope_counts = {}
    for e in events:
        if e["event_type"] == "SCOPE_FINISHED":
            scope_counts[e["data"]["scope_id"]] = e["data"]["stats"]
    return {"search_id": events[0]["context"]["search_id"], "events": len(events),
            "state": "COMPLETED" if events[-1]["event_type"] == "RUN_FINISHED" else "INCOMPLETE",
            "started_trials": len(started), "finished_trials": len(finished),
            "unfinished_trial_ids": sorted(started - finished), "scope_stats": scope_counts,
            "historical_n_raw": events[0]["context"]["historical_n_raw"],
            "last_event_sha256": events[-1]["event_sha256"],
            "orphan_staging_files": len(list(Path(path).glob(".pending-*"))),
            "ledger_writes": 0}


class ExplorationAudit:
    """Exclusive attempt writer, made explicit by callers (never enabled by env).

    search_id identifies an execution attempt, run_id the ADR-002 run. Retrying a
    read/receipt reuses files; rerunning computation needs a new search_id AND a
    new run_id for independent reevaluation. The journal performs no ledger writes.
    """
    def __init__(self, path: Path, context: dict):
        self.path, self.context = path, context
        self._scopes: dict[str, dict] = {}
        self._trials: dict[str, dict] = {}
        self._closed = False
        self._next_sequence = 1
        self._previous_sha256 = None

    @classmethod
    def create(cls, root: str | Path, *, search_id: str, run_id: str,
               family_key: str, policy_hash: str, code_commit: str,
               input_evidence_sha256: str, settings: dict,
               historical_n_raw: int | None = None) -> "ExplorationAudit":
        if not all(isinstance(v, str) and v.strip() for v in (search_id, run_id)):
            raise AuditError("search_id and run_id are required")
        if not re.fullmatch(r"fam:[0-9a-f]{32}", family_key):
            raise AuditError("explicit research family is required")
        if not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", policy_hash):
            raise AuditError("explicit policy hash is required")
        if not re.fullmatch(r"[0-9a-f]{40}", code_commit):
            raise AuditError("full code commit is required")
        if not re.fullmatch(r"[0-9a-f]{64}", input_evidence_sha256):
            raise AuditError("input evidence hash is required")
        if historical_n_raw is not None and (type(historical_n_raw) is not int or historical_n_raw < 1):
            raise AuditError("unknown historical N must remain null")
        _no_secrets(settings)
        _blob(settings)  # Validate before creating an attempt directory.
        root = Path(root)
        if not root.is_dir() or root.is_symlink():
            raise AuditError("audit root must be an existing provisioned directory")
        path = root / hashlib.sha256(search_id.encode()).hexdigest()
        try:
            with (root / ".registry-lock").open("a+b") as registry:
                fcntl.flock(registry, fcntl.LOCK_EX)
                if path.exists():
                    raise AuditError("search attempt already exists; inspect rather than overwrite")
                reservation = root / (".run-" + hashlib.sha256(run_id.encode()).hexdigest())
                if reservation.exists():
                    raise AuditError("run_id already reserved; independent reevaluation needs a new run_id")
                path.mkdir(mode=0o700)
                with reservation.open("xb") as f:
                    f.write(_blob({"search_id": search_id, "run_id": run_id, "attempt": path.name}))
                    f.flush()
                    os.fchmod(f.fileno(), 0o400)
                    os.fsync(f.fileno())
                _sync_dir(root)
        except FileExistsError as exc:
            raise AuditError("search attempt already exists; inspect rather than overwrite") from exc
        except OSError as exc:
            raise AuditError("audit directory durability failed") from exc
        context = {"search_id": search_id, "run_id": run_id, "family_key": family_key,
                   "policy_hash": policy_hash, "code_commit": code_commit,
                   "input_evidence_sha256": input_evidence_sha256,
                   "historical_n_raw": historical_n_raw}
        audit = cls(path, context)
        audit._append("RUN_STARTED", {"settings": settings, "ledger_role": "observation_only"})
        return audit

    def require_run(self, run_id: str) -> None:
        if run_id != self.context["run_id"]:
            raise AuditError("audit run_id mismatch")

    def _append(self, event_type: str, data: dict) -> dict:
        if self._closed:
            raise AuditError("audit attempt is closed")
        try:
            with (self.path / ".lock").open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                event = {"schema_version": 1, "sequence": self._next_sequence,
                         "previous_sha256": self._previous_sha256,
                         "context": self.context, "event_type": event_type,
                         "recorded_at": datetime.now(timezone.utc).isoformat(), "data": audit_value(data)}
                event["event_sha256"] = evidence_hash(event)
                blob = _blob(event)
                fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=self.path)
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(blob)
                        f.flush()
                        os.fchmod(f.fileno(), 0o400)
                        os.fsync(f.fileno())
                    # link fails if the immutable destination already exists.
                    os.link(tmp, self.path / f"{event['sequence']:012d}.json")
                    _sync_dir(self.path)
                    self._next_sequence += 1
                    self._previous_sha256 = event["event_sha256"]
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                return event
        except (OSError, ValueError, TypeError) as exc:
            self._closed = True
            raise AuditError("durable audit write failed") from exc

    def start_scope(self, stage: str, settings: dict) -> str:
        _no_secrets(settings)
        scope = f"{stage}:{len(self._scopes)}"
        self._append("SCOPE_STARTED", {"scope_id": scope, "stage": stage, "settings": settings})
        self._scopes[scope] = {"stage": stage, "finished": False}
        return scope

    def start_trial(self, scope_id: str, trial_id: str, *, evidence: dict,
                    counting_owner: str, n_raw_role: str) -> None:
        if scope_id not in self._scopes or self._scopes[scope_id]["finished"] or trial_id in self._trials:
            raise AuditError("duplicate trial or invalid scope")
        record = {"scope_id": scope_id, "trial_id": trial_id, "evidence": evidence,
                  "evidence_sha256": evidence_hash(evidence), "counting_owner": counting_owner,
                  "n_raw_role": n_raw_role, "ledger_batch_id": None, "ledger_write_verified": False}
        self._append("TRIAL_STARTED", record)
        self._trials[trial_id] = {**record, "finished": False}

    def note(self, event_type: str, data: dict) -> None:
        if event_type not in {"CANDIDATE_GENERATED", "EVALUATION_STARTED", "EVALUATION_FINISHED",
                              "CANDIDATE_DISPOSITION", "RUN_FAILED"}:
            raise AuditError("unsupported observation event")
        self._append(event_type, data)

    def finish_trial(self, trial_id: str, *, status: str, result: dict,
                     formula_key: str | None = None) -> None:
        trial = self._trials.get(trial_id)
        if not trial or trial["finished"]:
            raise AuditError("trial has not started or is already finished")
        if status not in {"SUCCEEDED", "FAILED", "NONFINITE", "INVALID", "SKIPPED"}:
            raise AuditError("invalid evaluation status")
        self._append("TRIAL_FINISHED", {"scope_id": trial["scope_id"], "trial_id": trial_id,
                                       "status": status, "result": result,
                                       "result_sha256": evidence_hash(result), "formula_key": formula_key})
        trial.update(finished=True, status=status, formula_key=formula_key)

    def finish_scope(self, scope_id: str, stats: dict) -> None:
        scope = self._scopes.get(scope_id)
        if not scope or scope["finished"]:
            raise AuditError("scope missing or finished")
        trials = [t for t in self._trials.values() if t["scope_id"] == scope_id]
        if any(not t["finished"] for t in trials):
            raise AuditError("cannot complete scope with unfinished trials")
        n_distinct = len({t["formula_key"] for t in trials if t.get("formula_key") is not None})
        if "n_distinct_expr" in stats and stats["n_distinct_expr"] != n_distinct:
            raise AuditError("observed formulas disagree with S1 trial count")
        self._append("SCOPE_FINISHED", {"scope_id": scope_id, "stats": stats,
                                       "observed_trials": len(trials), "observed_distinct_formula_keys": n_distinct})
        scope["finished"] = True

    def finish(self) -> dict:
        if any(not s["finished"] for s in self._scopes.values()):
            raise AuditError("cannot complete attempt with unfinished scopes")
        self._append("RUN_FINISHED", {"scopes": len(self._scopes), "trials": len(self._trials),
                                      "ledger_writes": 0})
        self._closed = True
        return inspect_attempt(self.path)
