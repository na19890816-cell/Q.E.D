"""Independent raw JSON artifact v1, opt-in local storage only.

Reuses the pinned PR #5 POSIX primitives without changing record/JSONL v1.
No network, DB, trading I/O or research-eligibility granting API.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import os
import re
import uuid

from .local_ingestion import (
    Limits, LocalAuditStore, StoreCorrupt, _ATTEMPT, _HEX, _ID,
    _canonical, _now, _sha, _strict_json,
)

CONTRACT = "urn:qed:artifact:raw-json:1"
_PROVENANCE = {
    "market_date", "historical_captured_at", "available_at", "availability_evidence_sha256",
    "source_revision", "code_commit", "code_content_sha256", "policy_hash",
    "universe_manifest_sha256", "calendar_version", "logical_record_count",
}
_FIELDS = {
    "contract_id", "artifact_schema_version", "source_system", "export_id", "artifact_id",
    "artifact_kind", "serialization", "media_type", "exported_at", "complete",
    "raw_sha256", "size_bytes", "artifact_role", "derived_from", "provenance", "provenance_gaps",
}


@dataclass(frozen=True)
class ArtifactLimits(Limits):
    json_depth: int = 64
    json_nodes: int = 100_000


def _timestamp(value):
    if type(value) is not str or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value
    ):
        raise ValueError
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _hex(value):
    return type(value) is str and _HEX.fullmatch(value)


def validate_artifact_manifest(value, limits=None):
    """Pure, closed-schema validation; unknown history remains explicit null."""
    limits = limits or ArtifactLimits()
    try:
        if type(value) is not dict or set(value) != _FIELDS:
            raise ValueError
        raw = _canonical(value)
        if len(raw) > limits.manifest_bytes:
            raise ValueError
        if (value["contract_id"] != CONTRACT or type(value["artifact_schema_version"]) is not int
                or value["artifact_schema_version"] != 1 or value["source_system"] != "prostock2"
                or value["serialization"] != "single_json_document"
                or value["media_type"] != "application/json" or value["complete"] is not True):
            raise ValueError
        for field in ("export_id", "artifact_id"):
            if type(value[field]) is not str or not _ID.fullmatch(value[field]):
                raise ValueError
        if value["artifact_kind"] not in ("snapshot", "jvram_history", "ovx_history", "regime_history", "other"):
            raise ValueError
        if (not _hex(value["raw_sha256"]) or type(value["size_bytes"]) is not int
                or not 1 <= value["size_bytes"] <= min(limits.file_bytes, 16 * 1024 * 1024)):
            raise ValueError
        exported = _timestamp(value["exported_at"])
        provenance = value["provenance"]
        if type(provenance) is not dict or set(provenance) != _PROVENANCE:
            raise ValueError
        gaps = value["provenance_gaps"]
        if (type(gaps) is not list or len(gaps) > 64 or any(type(g) is not str or not _ID.fullmatch(g) for g in gaps)
                or len(set(gaps)) != len(gaps) or not {k for k, v in provenance.items() if v is None} <= set(gaps)):
            raise ValueError
        for key, item in provenance.items():
            if item is None:
                continue
            if key in ("historical_captured_at", "available_at"):
                if _timestamp(item) > exported:
                    raise ValueError
            elif key == "market_date":
                if type(item) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item):
                    raise ValueError
                date.fromisoformat(item)
            elif key == "logical_record_count":
                if type(item) is not int or item < 0:
                    raise ValueError
            elif key == "code_commit":
                if type(item) is not str or not re.fullmatch(r"[0-9a-f]{40}", item):
                    raise ValueError
            elif key.endswith("sha256") or key == "policy_hash":
                if not _hex(item):
                    raise ValueError
            elif type(item) is not str or not _ID.fullmatch(item):
                raise ValueError
        if provenance["available_at"] is not None and provenance["availability_evidence_sha256"] is None:
            raise ValueError
        parents = value["derived_from"]
        if type(parents) is not list or len(parents) > 64:
            raise ValueError
        if value["artifact_role"] == "original":
            if parents:
                raise ValueError
        elif value["artifact_role"] == "derived":
            if not parents:
                raise ValueError
        else:
            raise ValueError
        parent_hashes = []
        for parent in parents:
            if type(parent) is not dict or set(parent) != {
                "raw_sha256", "transformation_code_sha256", "spec_sha256", "settings_sha256"
            } or any(not _hex(item) for item in parent.values()):
                raise ValueError
            parent_hashes.append(parent["raw_sha256"])
        if len(set(parent_hashes)) != len(parent_hashes) or value["raw_sha256"] in parent_hashes:
            raise ValueError
        return _strict_json(raw)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ValueError("INVALID_ARTIFACT_MANIFEST") from None


def validate_document(raw, limits=None):
    """One strict UTF-8 JSON object/array. Checks structure, not source semantics.

    Scan nesting before decoding to bound parser recursion. Keep raw unchanged.
    """
    limits = limits or ArtifactLimits()
    try:
        if type(raw) is not bytes or not 1 <= len(raw) <= limits.file_bytes:
            raise ValueError
        depth = 0
        quoted = escaped = False
        for byte in raw:
            if quoted:
                if escaped:
                    escaped = False
                elif byte == 92:
                    escaped = True
                elif byte == 34:
                    quoted = False
            elif byte == 34:
                quoted = True
            elif byte in (91, 123):
                depth += 1
                if depth > limits.json_depth:
                    raise ValueError
            elif byte in (93, 125):
                depth -= 1
        document = _strict_json(raw)
        if type(document) not in (dict, list):
            raise ValueError
        pending = [document]
        nodes = 0
        while pending:
            item = pending.pop()
            nodes += 1
            if nodes > limits.json_nodes:
                raise ValueError
            if type(item) is dict:
                pending.extend(item.keys())
                pending.extend(item.values())
            elif type(item) is list:
                pending.extend(item)
            elif type(item) is str:
                item.encode("utf-8", errors="strict")  # reject lone escaped surrogates
        return document
    except (ValueError, UnicodeError, OverflowError, RecursionError):
        raise ValueError("INVALID_SINGLE_JSON_DOCUMENT") from None


class RawJsonArtifactStore(LocalAuditStore):
    """Separate root/namespace; terminal receipts never authorize research.

    Only the parent class filesystem methods are reused. JSONL intake, record
    claims and v1 contract validation are not invoked. Local owner is trusted.
    """

    def __init__(self, root, *, limits=None):
        super().__init__(root, limits=limits or ArtifactLimits())
        with self._locked():
            marker = self.root / "artifact-store.json"
            if not marker.exists():
                if any(any((self.root / name).iterdir()) for name in self._DIRS):
                    raise StoreCorrupt("NONEMPTY_FOREIGN_STORE")
            self._publish_marker()

    def _publish_marker(self):
        # Use the same exclusive publication and fsync protocol as other metadata.
        self._publish(".", "artifact-store.json", _canonical({"store_contract": CONTRACT}))

    def _manifest_for(self, manifest_hash):
        if not _hex(manifest_hash):
            raise StoreCorrupt("CORRUPT_ARTIFACT_MANIFEST")
        path = self.root / "manifests" / (manifest_hash + ".json")
        raw = self._read(path, self.limits.manifest_bytes)
        if _sha(raw) != manifest_hash:
            raise StoreCorrupt("MANIFEST_HASH_MISMATCH")
        try:
            result = validate_artifact_manifest(_strict_json(raw), self.limits)
        except (ValueError, UnicodeError, RecursionError):
            raise StoreCorrupt("CORRUPT_ARTIFACT_MANIFEST") from None
        self._ensure_durable(path)
        return result

    def _all_receipts(self):
        result = []
        for path in sorted((self.root / "receipts").iterdir()):
            receipt = self._load_json(path)
            try:
                checksum = receipt["receipt_sha256"]
                body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
                if (_sha(_canonical(body)) != checksum or path.name != receipt["receipt_id"] + ".json"
                        or receipt["contract_version"] != CONTRACT
                        or receipt["state"] not in ("PROVENANCE_INCOMPLETE", "QUARANTINED")
                        or receipt["research_eligible"] is not False
                        or "RESEARCH_ELIGIBLE" in receipt["state_history"]):
                    raise ValueError
                raw = self._blob(receipt["raw_sha256"])
                if len(raw) != receipt["stored_bytes"]:
                    raise ValueError
                if receipt["manifest_sha256"]:
                    self._manifest_for(receipt["manifest_sha256"])
                self._ensure_durable(path)
                result.append(receipt)
            except (KeyError, TypeError, ValueError):
                raise StoreCorrupt("CORRUPT_ARTIFACT_RECEIPT") from None
        return result

    def _finish(self, intent, raw, *, complete, interrupted=False, recovered=False):
        raw_hash = _sha(raw)
        self._publish("blobs", raw_hash + ".raw", raw)
        manifest_hash = intent["manifest_sha256"]
        manifest = self._manifest_for(manifest_hash) if manifest_hash else None
        states = ["RECEIVED"]
        reasons = []
        checks = {"raw_integrity": False, "single_json_document": False, "parent_artifacts": False}
        if interrupted:
            reasons.append("INTERRUPTED_TRANSFER")
        elif intent["manifest_error"]:
            reasons.append("INVALID_ARTIFACT_MANIFEST")
        elif not complete:
            reasons.append("FILE_SIZE_LIMIT")
        elif len(raw) != manifest["size_bytes"] or raw_hash != manifest["raw_sha256"]:
            reasons.append("RAW_INTEGRITY_MISMATCH")
        else:
            checks["raw_integrity"] = True
            states.append("INTEGRITY_VERIFIED")
            try:
                validate_document(raw, self.limits)
                checks["single_json_document"] = True
                states.append("CONTRACT_VALIDATED")  # storage grammar only
            except ValueError:
                reasons.append("INVALID_SINGLE_JSON_DOCUMENT")
        existing = self._all_receipts()
        prior_valid = [r for r in existing if r["state"] == "PROVENANCE_INCOMPLETE" and r["raw_complete"]]
        parents = manifest["derived_from"] if manifest else []
        missing = sorted({p["raw_sha256"] for p in parents} - {r["raw_sha256"] for r in prior_valid})
        checks["parent_artifacts"] = bool(manifest and not missing)
        if missing:
            reasons.append("PARENT_ARTIFACTS_MISSING")
        identity = [manifest["source_system"], manifest["artifact_id"]] if manifest else None
        export_identity = [manifest["source_system"], manifest["export_id"]] if manifest else None
        duplicate = None
        if checks["raw_integrity"]:
            for prior in prior_valid:
                if prior["artifact_identity"] == identity or prior["export_identity"] == export_identity:
                    if prior["manifest_sha256"] != manifest_hash:
                        reasons.append("ARTIFACT_ID_CONFLICT")
                    else:
                        duplicate = prior["duplicate_of"] or prior["receipt_id"]
        state = "QUARANTINED" if reasons else "PROVENANCE_INCOMPLETE"
        if state == "QUARANTINED":
            self._quarantine(intent["receipt_id"], raw_hash)
            duplicate = None
        else:
            reasons.append("INDEPENDENT_PROVENANCE_REVIEW_REQUIRED")
        states.append(state)
        receipt = {
            "receipt_version": 1, "receipt_id": intent["receipt_id"],
            "contract_version": CONTRACT, "manifest_sha256": manifest_hash,
            "received_at": intent["received_at"], "finished_at": _now(), "recovered": recovered,
            "state": state, "state_history": states, "research_eligible": False,
            "raw_sha256": raw_hash, "stored_bytes": len(raw),
            "declared_raw_sha256": manifest["raw_sha256"] if manifest else None,
            "raw_complete": bool(complete and checks["raw_integrity"]), "stream_complete": complete,
            "artifact_identity": identity, "export_identity": export_identity,
            "artifact_role": manifest["artifact_role"] if manifest else None,
            "parent_raw_sha256": [p["raw_sha256"] for p in parents],
            "missing_parent_sha256": missing, "checks": checks,
            "reason_codes": sorted(set(reasons)), "duplicate_of": duplicate,
            "logical_record_count_verified": False,
        }
        receipt["receipt_sha256"] = _sha(_canonical(receipt))
        self._publish("receipts", intent["receipt_id"] + ".json", _canonical(receipt))
        return receipt

    def receive(self, stream, manifest):
        """Durable receipt or safe storage error. An interrupted prefix is retained."""
        with self._locked():
            self._recover_locked()
            try:
                valid = validate_artifact_manifest(manifest, self.limits)
            except ValueError:
                valid = None
            manifest_hash = None
            if valid:
                data = _canonical(valid)
                manifest_hash = _sha(data)
                self._publish("manifests", manifest_hash + ".json", data)
            self._capacity((valid["size_bytes"] if valid else self.limits.file_bytes) + 8192)
            attempt = uuid.uuid4().hex
            intent = {"intent_version": 1, "receipt_id": attempt, "received_at": _now(),
                      "manifest_sha256": manifest_hash, "manifest_error": valid is None}
            intent["intent_sha256"] = _sha(_canonical(intent))
            self._publish("intents", attempt + ".json", _canonical(intent))
            partial = self.root / "pending" / (attempt + ".part")
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            complete, interrupted, length = True, False, 0
            with os.fdopen(fd, "wb") as target:
                self._sync_dir(partial.parent)
                while True:
                    maximum = min(65536, self.limits.file_bytes + 1 - length)
                    try:
                        chunk = stream.read(maximum)
                        if type(chunk) is not bytes or len(chunk) > maximum:
                            raise ValueError
                    except Exception:
                        complete, interrupted = False, True
                        break
                    if not chunk:
                        break
                    target.write(chunk)
                    length += len(chunk)
                    if length > self.limits.file_bytes:
                        complete = False
                        break
                target.flush()
                os.fsync(target.fileno())
            raw = self._read(partial, self.limits.file_bytes + 1)
            receipt = self._finish(intent, raw, complete=complete, interrupted=interrupted)
            partial.unlink()
            self._sync_dir(partial.parent)
            return receipt

    # recover(), receipts() and acknowledge() dispatch to the artifact overrides.
    # The pinned parent's checksummed intent schema and POSIX protocol are shared.
