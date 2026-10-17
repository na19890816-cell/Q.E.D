"""Local, opt-in audit intake. No DB, network, research permission, or trading I/O.

Durability assumes an owner-controlled local POSIX filesystem with working flock,
hard links and fsync. Hashes detect inconsistency; they do not authenticate history.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .prostock2_v1 import (
    ContractValidationError, coverage_key, decode_record, evidence_sha256,
    validate_records,
)


class IngestionState(str, Enum):
    RECEIVED = "RECEIVED"
    INTEGRITY_VERIFIED = "INTEGRITY_VERIFIED"
    CONTRACT_VALIDATED = "CONTRACT_VALIDATED"
    PROVENANCE_INCOMPLETE = "PROVENANCE_INCOMPLETE"
    RESEARCH_ELIGIBLE = "RESEARCH_ELIGIBLE"  # reserved; no granting API
    QUARANTINED = "QUARANTINED"


class StorageError(RuntimeError):
    """Safe stable error code; never includes payload, source paths or credentials."""


class StoreCorrupt(StorageError):
    pass


@dataclass(frozen=True)
class Limits:
    file_bytes: int = 16 * 1024 * 1024
    line_bytes: int = 256 * 1024
    records: int = 10_000
    manifest_bytes: int = 4 * 1024 * 1024
    store_bytes: int = 1024 * 1024 * 1024
    reserve_bytes: int = 16 * 1024 * 1024

    def __post_init__(self):
        for name, value in vars(self).items():
            if type(value) is not int or value < (0 if name == "reserve_bytes" else 1):
                raise ValueError("INVALID_LIMIT")


_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ATTEMPT = re.compile(r"[0-9a-f]{32}\Z")
_MANIFEST_FIELDS = {
    "transport_schema_version", "source_system", "transfer_id", "record_contract",
    "raw_sha256", "size_bytes", "record_count", "expected_observations",
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("NONFINITE_JSON")


def _strict_json(raw):
    # Python's float parser can overflow an otherwise legal JSON number.
    import math

    def finite_float(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("NONFINITE_JSON")
        return value

    return json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_pairs,
                      parse_constant=_constant, parse_float=finite_float)


def validate_manifest(manifest, limits=None):
    """Validate transport metadata without changing the record-v1 contract."""
    limits = limits or Limits()
    try:
        if type(manifest) is not dict or set(manifest) != _MANIFEST_FIELDS:
            raise ValueError
        raw = _canonical(manifest)
        if len(raw) > limits.manifest_bytes:
            raise ValueError
        if type(manifest["transport_schema_version"]) is not int or manifest["transport_schema_version"] != 1:
            raise ValueError
        if manifest["source_system"] != "prostock2":
            raise ValueError
        if type(manifest["transfer_id"]) is not str or not _ID.fullmatch(manifest["transfer_id"]):
            raise ValueError
        if type(manifest["raw_sha256"]) is not str or not _HEX.fullmatch(manifest["raw_sha256"]):
            raise ValueError
        for field, maximum in (("size_bytes", limits.file_bytes), ("record_count", limits.records)):
            if type(manifest[field]) is not int or not 1 <= manifest[field] <= maximum:
                raise ValueError
        kind = manifest["record_contract"]
        observations = manifest["expected_observations"]
        if kind == "legacy_audit_archive_v1":
            if observations is not None:
                raise ValueError
        elif kind == "prostock2_audit_v1":
            if type(observations) is not list or not observations or len(observations) > limits.records:
                raise ValueError
            keys = []
            for observation in observations:
                if type(observation) is not list or len(observation) != 7:
                    raise ValueError
                if any(type(value) is not str or not value or len(value) > 128 for value in observation):
                    raise ValueError
                keys.append(tuple(observation))
            if len(keys) != len(set(keys)):
                raise ValueError
        else:
            raise ValueError
        return _strict_json(raw)  # independent copy
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ValueError("INVALID_MANIFEST") from None


class LocalAuditStore:
    """Serialized immutable raw/manifest/intent/receipt store.

    receive returns only after a terminal receipt is durable. QUARANTINED is a
    durable rejection, not a successful import. A storage error can leave an
    intent; recover it after fixing storage. No method grants research use.
    """

    _DIRS = ("pending", "intents", "blobs", "manifests", "receipts", "quarantine")

    def __init__(self, root, *, limits=None):
        self.root = Path(root)
        self.limits = limits or Limits()
        try:
            if self.root.is_symlink():
                raise StoreCorrupt("UNSAFE_STORE")
            # Parent must already be durable. Avoid silently creating ancestors
            # whose directory entries have not been fsynced.
            self.root.mkdir(mode=0o700, exist_ok=True)
            for name in self._DIRS:
                path = self.root / name
                if path.is_symlink():
                    raise StoreCorrupt("UNSAFE_STORE")
                path.mkdir(mode=0o700, exist_ok=True)
                if not path.is_dir():
                    raise StoreCorrupt("UNSAFE_STORE")
            self._sync_dir(self.root)
            self._sync_dir(self.root.parent)
        except OSError:
            raise StorageError("STORAGE_UNAVAILABLE") from None

    @staticmethod
    def _sync_dir(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _ensure_durable(self, path):
        # A previous publication may have linked the file and failed before its
        # directory fsync. Reuse must complete that durability boundary too.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self._sync_dir(path.parent)

    @contextmanager
    def _locked(self):
        try:
            fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)
        except OSError:
            raise StorageError("STORAGE_IO_FAILURE") from None

    def _read(self, path, maximum):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                    raise StoreCorrupt("INVALID_STORED_FILE")
                data = stream.read(maximum + 1)
                if len(data) > maximum:
                    raise StoreCorrupt("INVALID_STORED_FILE")
                return data
        except FileNotFoundError:
            raise StoreCorrupt("MISSING_STORED_FILE") from None

    def _capacity(self, additional):
        used = 0
        # Hardlinks in quarantine do not consume a second copy.
        inodes = set()
        for directory in self._DIRS:
            for path in (self.root / directory).iterdir():
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise StoreCorrupt("UNSAFE_STORE")
                key = (info.st_dev, info.st_ino)
                if key not in inodes:
                    used += info.st_size
                    inodes.add(key)
        space = os.statvfs(self.root)
        if (used + additional > self.limits.store_bytes or
                space.f_bavail * space.f_frsize < self.limits.reserve_bytes + additional):
            raise StorageError("INSUFFICIENT_STORAGE")

    def _publish(self, directory, filename, data):
        target = self.root / directory / filename
        if target.exists() or target.is_symlink():
            if self._read(target, max(len(data), 1)) != data:
                raise StoreCorrupt("IMMUTABLE_FILE_CONFLICT")
            self._ensure_durable(target)
            return
        self._capacity(len(data))
        temp = self.root / "pending" / ("publish-" + uuid.uuid4().hex + ".part")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, target, follow_symlinks=False)  # exclusive, never replace
        self._sync_dir(target.parent)
        temp.unlink()
        self._sync_dir(temp.parent)

    def _load_json(self, path):
        try:
            return _strict_json(self._read(path, self.limits.manifest_bytes + self.limits.file_bytes * 8))
        except (ValueError, UnicodeError, RecursionError):
            raise StoreCorrupt("CORRUPT_METADATA") from None

    def _blob(self, raw_hash):
        if type(raw_hash) is not str or not _HEX.fullmatch(raw_hash):
            raise StoreCorrupt("CORRUPT_METADATA")
        raw = self._read(self.root / "blobs" / (raw_hash + ".raw"), self.limits.file_bytes + 1)
        if _sha(raw) != raw_hash:
            raise StoreCorrupt("RAW_HASH_MISMATCH")
        self._ensure_durable(self.root / "blobs" / (raw_hash + ".raw"))
        return raw

    def _all_receipts(self):
        result = []
        for path in sorted((self.root / "receipts").iterdir()):
            receipt = self._load_json(path)
            try:
                checksum = receipt["receipt_sha256"]
                content = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
                if _sha(_canonical(content)) != checksum or path.name != receipt["receipt_id"] + ".json":
                    raise ValueError
                if receipt["state"] not in ("QUARANTINED", "PROVENANCE_INCOMPLETE"):
                    raise ValueError
                raw = self._blob(receipt["raw_sha256"])
                if len(raw) != receipt["stored_bytes"]:
                    raise ValueError
                if receipt["manifest_sha256"] is not None:
                    self._manifest_for(receipt["manifest_sha256"])
                self._ensure_durable(path)
                result.append(receipt)
            except (KeyError, TypeError, ValueError):
                raise StoreCorrupt("CORRUPT_RECEIPT") from None
        return result

    def _manifest_for(self, manifest_hash):
        if type(manifest_hash) is not str or not _HEX.fullmatch(manifest_hash):
            raise StoreCorrupt("CORRUPT_METADATA")
        raw = self._read(self.root / "manifests" / (manifest_hash + ".json"), self.limits.manifest_bytes)
        if _sha(raw) != manifest_hash:
            raise StoreCorrupt("MANIFEST_HASH_MISMATCH")
        self._ensure_durable(self.root / "manifests" / (manifest_hash + ".json"))
        try:
            return validate_manifest(_strict_json(raw), self.limits)
        except (ValueError, UnicodeError):
            raise StoreCorrupt("CORRUPT_MANIFEST") from None

    def _quarantine(self, attempt, raw_hash):
        target = self.root / "quarantine" / (attempt + ".raw")
        source = self.root / "blobs" / (raw_hash + ".raw")
        if not target.exists():
            os.link(source, target, follow_symlinks=False)
        self._ensure_durable(target)

    def _finish(self, intent, raw, *, complete, interrupted=False, recovered=False):
        attempt = intent["receipt_id"]
        raw_hash = _sha(raw)
        self._publish("blobs", raw_hash + ".raw", raw)
        states = ["RECEIVED"]
        reasons = []
        claims = []
        new_count = reused_count = 0
        duplicate_of = None
        manifest_hash = intent["manifest_sha256"]
        manifest = self._manifest_for(manifest_hash) if manifest_hash else None
        # JSONL is delimited by LF, not arbitrary Unicode/ASCII line separators.
        lines = raw.split(b"\n")
        if raw.endswith(b"\n"):
            lines.pop()
        if not raw:
            lines = []
        count = len(lines)
        checks = {"raw_integrity": False, "strict_jsonl": False,
                  "record_contract": False, "dependencies": False, "coverage": False,
                  "source_artifacts": False}
        required_source_hashes = set()
        source_hash_unknown = True
        if interrupted:
            reasons.append("INTERRUPTED_TRANSFER")
        elif intent["manifest_error"]:
            reasons.append("INVALID_MANIFEST")
        elif not complete:
            reasons.append("FILE_SIZE_LIMIT")
        elif len(raw) != manifest["size_bytes"] or raw_hash != manifest["raw_sha256"]:
            reasons.append("RAW_INTEGRITY_MISMATCH")
        else:
            states.append("INTEGRITY_VERIFIED")
            checks["raw_integrity"] = True
            if not raw.endswith(b"\n") or not raw or any(not line for line in lines):
                reasons.append("INCOMPLETE_JSONL")
            elif count != manifest["record_count"] or count > self.limits.records:
                reasons.append("RECORD_COUNT_MISMATCH")
            elif any(len(line) > self.limits.line_bytes for line in lines):
                reasons.append("LINE_SIZE_LIMIT")
            else:
                try:
                    records = [_strict_json(line) for line in lines]
                    if any(type(record) is not dict for record in records):
                        raise ValueError
                    checks["strict_jsonl"] = True
                except (ValueError, UnicodeError, RecursionError):
                    reasons.append("INVALID_STRICT_JSON")
                if not reasons and manifest["record_contract"] == "prostock2_audit_v1":
                    try:
                        records = [decode_record(line) for line in lines]
                        validate_records(records, expected_observations={
                            tuple(item) for item in manifest["expected_observations"]})
                        checks.update(record_contract=True, dependencies=True, coverage=True)
                        states.append("CONTRACT_VALIDATED")
                        source_hash_unknown = any(record["market_data_hash"] is None for record in records)
                        required_source_hashes = {record["market_data_hash"] for record in records
                                                  if record["market_data_hash"] is not None}
                        for record in records:
                            claims.append({
                                "identity": [record["source_system"], record["run_id"], record["record_id"]],
                                "natural_key": list(coverage_key(record)) + [record["source_revision"]],
                                "evidence_sha256": evidence_sha256(record),
                            })
                    except ContractValidationError:
                        # Validator code is a fixed vocabulary; never persist input values.
                        reasons.append("RECORD_CONTRACT_INVALID")
                elif not reasons:
                    reasons.append("LEGACY_PROVENANCE_INCOMPLETE")

        existing = self._all_receipts()
        # Closed record dependencies above are different from the original raw
        # market-data artifacts. Only an already durably received, integrity-
        # checked, non-quarantined raw copy can resolve a declared source hash.
        available_source_hashes = {previous["raw_sha256"] for previous in existing
                                   if previous["state"] != "QUARANTINED"
                                   and previous["checks"]["raw_integrity"]
                                   and previous["raw_complete"]}
        missing_source_hashes = sorted(required_source_hashes - available_source_hashes)
        checks["source_artifacts"] = bool(checks["record_contract"] and
                                            not source_hash_unknown and not missing_source_hashes)
        integrity_ok = checks["raw_integrity"]
        if integrity_ok:
            for previous in existing:
                if not previous["checks"]["raw_integrity"]:
                    continue
                if previous["transfer_identity"] == [manifest["source_system"], manifest["transfer_id"]]:
                    if (previous["raw_sha256"] != raw_hash or previous["manifest_sha256"] != manifest_hash):
                        reasons.append("TRANSFER_ID_CONFLICT")
                        break
                    if previous["duplicate_of"] is None:
                        duplicate_of = previous["receipt_id"]
        if claims:
            identities = {}
            natural_keys = {}
            for previous in existing:
                for claim in previous["record_claims"]:
                    identities[tuple(claim["identity"])] = claim
                    natural_keys[tuple(claim["natural_key"])] = claim
            for claim in claims:
                identity = tuple(claim["identity"])
                prior = identities.get(identity)
                if prior is not None:
                    if prior != claim:
                        reasons.append("RECORD_ID_CONFLICT")
                        break
                    reused_count += 1
                elif tuple(claim["natural_key"]) in natural_keys:
                    reasons.append("NATURAL_KEY_CONFLICT")
                    break
                else:
                    new_count += 1
        archive_ok = reasons == ["LEGACY_PROVENANCE_INCOMPLETE"]
        valid = checks["record_contract"] and not reasons
        state = "PROVENANCE_INCOMPLETE" if valid or archive_ok else "QUARANTINED"
        if valid:
            reasons.append("INDEPENDENT_PROVENANCE_REVIEW_REQUIRED")
            if not checks["source_artifacts"]:
                reasons.append("SOURCE_ARTIFACTS_UNVERIFIED")
        if state == "QUARANTINED":
            claims = []
            new_count = reused_count = 0
            self._quarantine(attempt, raw_hash)
        states.append(state)
        receipt = {
            "receipt_version": 1, "receipt_id": attempt,
            "received_at": intent["received_at"], "finished_at": _now(),
            "recovered": recovered, "state": state, "state_history": states,
            "raw_sha256": raw_hash,
            "declared_raw_sha256": manifest["raw_sha256"] if manifest else None,
            "raw_complete": bool(complete and checks["raw_integrity"]),
            "stream_complete": complete, "stored_bytes": len(raw),
            "record_count": count if checks["strict_jsonl"] else None,
            "observed_line_count": count, "final_line_terminated": raw.endswith(b"\n"),
            "declared_record_count": manifest["record_count"] if manifest else None,
            "manifest_sha256": manifest_hash,
            "contract_version": manifest["record_contract"] if manifest else None,
            "transfer_identity": [manifest["source_system"], manifest["transfer_id"]] if manifest else None,
            "checks": checks, "reason_codes": sorted(set(reasons)),
            "missing_source_hashes": missing_source_hashes,
            "source_hash_unknown": source_hash_unknown,
            "record_claims": claims, "new_record_count": new_count,
            "reused_record_count": reused_count, "duplicate_of": duplicate_of,
        }
        receipt["receipt_sha256"] = _sha(_canonical(receipt))
        self._publish("receipts", attempt + ".json", _canonical(receipt))
        return receipt

    def _recover_locked(self):
        existing = {receipt["receipt_id"] for receipt in self._all_receipts()}
        recovered = []
        for path in sorted((self.root / "intents").iterdir()):
            intent = self._load_json(path)
            try:
                if type(intent) is not dict or set(intent) != {
                    "intent_version", "receipt_id", "received_at", "manifest_sha256",
                    "manifest_error", "intent_sha256",
                }:
                    raise ValueError
                if type(intent["intent_version"]) is not int or intent["intent_version"] != 1:
                    raise ValueError
                content = {key: value for key, value in intent.items() if key != "intent_sha256"}
                if _sha(_canonical(content)) != intent["intent_sha256"]:
                    raise ValueError
                attempt = intent["receipt_id"]
                if type(attempt) is not str or not _ATTEMPT.fullmatch(attempt) or path.name != attempt + ".json":
                    raise ValueError
                timestamp = intent["received_at"]
                if type(timestamp) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", timestamp):
                    raise ValueError
                datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if type(intent["manifest_error"]) is not bool:
                    raise ValueError
                if intent["manifest_error"]:
                    if intent["manifest_sha256"] is not None:
                        raise ValueError
                elif type(intent["manifest_sha256"]) is not str or not _HEX.fullmatch(intent["manifest_sha256"]):
                    raise ValueError
            except (ValueError, TypeError, OverflowError):
                raise StoreCorrupt("CORRUPT_INTENT")
            partial = self.root / "pending" / (attempt + ".part")
            if attempt not in existing:
                # The original process may have died before any byte was received.
                raw = self._read(partial, self.limits.file_bytes + 1) if partial.exists() else b""
                manifest = self._manifest_for(intent["manifest_sha256"]) if intent["manifest_sha256"] else None
                complete = bool(manifest and len(raw) == manifest["size_bytes"] and _sha(raw) == manifest["raw_sha256"])
                recovered.append(self._finish(intent, raw, complete=complete,
                                              interrupted=not complete, recovered=True))
            if partial.exists():
                partial.unlink()
                self._sync_dir(partial.parent)
        # Atomic metadata staging files have no source-stream authority. Preserve
        # them for diagnosis, never interpret them as completed imports.
        return recovered

    def recover(self):
        with self._locked():
            return self._recover_locked()

    def receipts(self):
        with self._locked():
            return self._all_receipts()

    def receive(self, stream, manifest):
        """Receive binary stream; limits are enforced before unbounded parsing.

        Stream exceptions become a durable quarantined prefix, if storage works.
        Process termination/BaseException leaves an intent for restart recovery.
        """
        with self._locked():
            self._recover_locked()
            error = False
            try:
                valid_manifest = validate_manifest(manifest, self.limits)
            except ValueError:
                valid_manifest = None
                error = True
            manifest_hash = None
            if valid_manifest is not None:
                manifest_raw = _canonical(valid_manifest)
                manifest_hash = _sha(manifest_raw)
                self._publish("manifests", manifest_hash + ".json", manifest_raw)
            self._capacity((valid_manifest["size_bytes"] if valid_manifest else self.limits.file_bytes) + 8192)
            attempt = uuid.uuid4().hex
            intent = {"intent_version": 1, "receipt_id": attempt, "received_at": _now(),
                      "manifest_sha256": manifest_hash, "manifest_error": error}
            intent["intent_sha256"] = _sha(_canonical(intent))
            self._publish("intents", attempt + ".json", _canonical(intent))
            partial = self.root / "pending" / (attempt + ".part")
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            complete = True
            interrupted = False
            length = 0
            with os.fdopen(fd, "wb") as target:
                self._sync_dir(partial.parent)
                while True:
                    try:
                        chunk = stream.read(min(65536, self.limits.file_bytes + 1 - length))
                        if type(chunk) is not bytes:
                            raise ValueError
                    except Exception:
                        complete = False
                        interrupted = True
                        break
                    if not chunk:
                        break
                    # Noncompliant streams cannot force oversized allocations on disk.
                    chunk = chunk[:self.limits.file_bytes + 1 - length]
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

    def acknowledge(self, receipt_id):
        """Durable copy confirmation only; never producer deletion permission."""
        if type(receipt_id) is not str or not _ATTEMPT.fullmatch(receipt_id):
            raise ValueError("INVALID_RECEIPT_ID")
        with self._locked():
            receipts = self._all_receipts()
            receipt = next((item for item in receipts if item["receipt_id"] == receipt_id), None)
            if receipt is None:
                raise ValueError("UNKNOWN_RECEIPT")
            if receipt["state"] == "QUARANTINED" or not receipt["raw_complete"]:
                raise ValueError("ACK_NOT_PERMITTED")
            # Re-read verifies hash/size; return binds all immutable objects.
            self._blob(receipt["raw_sha256"])
            return {"ack_version": 1, "receipt_id": receipt_id,
                    "raw_sha256": receipt["raw_sha256"], "size_bytes": receipt["stored_bytes"],
                    "manifest_sha256": receipt["manifest_sha256"],
                    "receipt_sha256": receipt["receipt_sha256"],
                    "confirmed_at": _now(), "research_eligible": False,
                    "producer_deletion_authorized": False}
