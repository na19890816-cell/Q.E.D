"""Synthetic Linux/POSIX fault/restart/concurrency tests; no production evidence.

Injected write/fsync errors model interrupted system calls, not a power-loss
certification of a particular filesystem. Subprocess tests also exercise actual
SIGKILL and independent flock owners without a database or network connection.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from threading import Barrier
import time

import pytest

from analytics.python.data_contracts import local_ingestion as intake
from analytics.python.data_contracts.prostock2_v1 import coverage_key
from tests.fixtures.prostock2_contract import ohlcv_record, rehash


REPO_ROOT = Path(__file__).resolve().parents[2]


def transfer(records=None, *, transfer_id="synthetic-transfer"):
    records = [ohlcv_record()] if records is None else records
    raw = b"".join(
        json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
        for r in records
    )
    return raw, {
        "transport_schema_version": 1,
        "source_system": "prostock2",
        "transfer_id": transfer_id,
        "record_contract": "prostock2_audit_v1",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "record_count": len(records),
        "expected_observations": [list(coverage_key(r)) for r in records],
    }


class FailingSource:
    def __init__(self, prefix, error):
        # A zero-byte read is EOF; an interruption before any byte must raise
        # on the first read rather than emit an EOF marker first.
        self.prefix, self.error = prefix or None, error

    def read(self, _):
        if self.prefix is not None:
            prefix, self.prefix = self.prefix, None
            return prefix
        raise self.error


def only_receipt(store):
    receipts = store.receipts()
    assert len(receipts) == 1
    return receipts[0]


def assert_archived_bytes(store, receipt, expected):
    assert (store.root / "blobs" / (receipt["raw_sha256"] + ".raw")).read_bytes() == expected
    assert receipt["stored_bytes"] == len(expected)
    assert receipt["raw_sha256"] == hashlib.sha256(expected).hexdigest()
    assert "RESEARCH_ELIGIBLE" not in receipt["state_history"]


def test_source_exception_quarantines_exact_prefix_and_cannot_ack(tmp_path):
    raw, manifest = transfer()
    store = intake.LocalAuditStore(tmp_path / "store")
    prefix = raw[:137]
    receipt = store.receive(FailingSource(prefix, OSError("synthetic source failure")), manifest)
    assert receipt["state"] == "QUARANTINED"
    assert "INTERRUPTED_TRANSFER" in receipt["reason_codes"]
    assert not receipt["raw_complete"]
    assert_archived_bytes(store, receipt, prefix)
    assert (store.root / "quarantine" / (receipt["receipt_id"] + ".raw")).read_bytes() == prefix
    with pytest.raises(ValueError, match="ACK_NOT_PERMITTED"):
        store.acknowledge(receipt["receipt_id"])
    assert store.recover() == []


@pytest.mark.parametrize("prefix_length", [0, 137])
def test_keyboard_interrupt_leaves_intent_and_restart_quarantines_prefix(tmp_path, prefix_length):
    raw, manifest = transfer()
    store = intake.LocalAuditStore(tmp_path / "store")
    with pytest.raises(KeyboardInterrupt):
        store.receive(FailingSource(raw[:prefix_length], KeyboardInterrupt()), manifest)
    assert len(list((store.root / "intents").glob("*.json"))) == 1
    assert not list((store.root / "receipts").glob("*.json"))
    restarted = intake.LocalAuditStore(store.root)
    recovered = restarted.recover()
    assert len(recovered) == 1
    receipt = only_receipt(restarted)
    assert receipt["recovered"] and receipt["state"] == "QUARANTINED"
    assert_archived_bytes(restarted, receipt, raw[:prefix_length])
    assert not list((store.root / "pending").glob("[0-9a-f]" * 32 + ".part"))
    assert restarted.recover() == []


def test_complete_fsynced_raw_is_revalidated_after_finish_crash(tmp_path, monkeypatch):
    raw, manifest = transfer()
    store = intake.LocalAuditStore(tmp_path / "store")
    with monkeypatch.context() as patch:
        def crash(*args, **kwargs):
            raise KeyboardInterrupt
        patch.setattr(store, "_finish", crash)
        with pytest.raises(KeyboardInterrupt):
            store.receive(io.BytesIO(raw), manifest)
    restarted = intake.LocalAuditStore(store.root)
    recovered = restarted.recover()
    assert len(recovered) == 1
    receipt = only_receipt(restarted)
    assert receipt["state"] == "PROVENANCE_INCOMPLETE"
    assert receipt["recovered"] and receipt["raw_complete"]
    # Synthetic market_data_hash is not a received upstream artifact; contract
    # validation must not be mistaken for independent provenance verification.
    assert all(receipt["checks"][key] for key in
               ("raw_integrity", "strict_jsonl", "record_contract", "dependencies", "coverage"))
    assert not receipt["checks"]["source_artifacts"]
    assert "SOURCE_ARTIFACTS_UNVERIFIED" in receipt["reason_codes"]
    assert receipt["new_record_count"] == 1
    assert_archived_bytes(restarted, receipt, raw)
    assert restarted.recover() == []


def test_receipt_durable_before_response_crash_is_not_registered_twice(tmp_path, monkeypatch):
    raw, manifest = transfer()
    store = intake.LocalAuditStore(tmp_path / "store")
    publish = store._publish
    with monkeypatch.context() as patch:
        def crash_after_receipt(directory, filename, data):
            publish(directory, filename, data)
            if directory == "receipts":
                raise KeyboardInterrupt
        patch.setattr(store, "_publish", crash_after_receipt)
        with pytest.raises(KeyboardInterrupt):
            store.receive(io.BytesIO(raw), manifest)
    original = only_receipt(store)
    serialized = (store.root / "receipts" / (original["receipt_id"] + ".json")).read_bytes()
    assert intake.LocalAuditStore(store.root).recover() == []
    assert only_receipt(store) == original
    retry = store.receive(io.BytesIO(raw), manifest)
    assert retry["new_record_count"] == 0 and retry["reused_record_count"] == 1
    assert retry["duplicate_of"] == original["receipt_id"]
    assert (store.root / "receipts" / (original["receipt_id"] + ".json")).read_bytes() == serialized
    assert store.acknowledge(original["receipt_id"])["producer_deletion_authorized"] is False


def _raw_part_fd(fd):
    name = Path(os.readlink(f"/proc/self/fd/{fd}")).name
    return re.fullmatch(r"[0-9a-f]{32}\.part", name) is not None


@pytest.mark.parametrize("failure", [OSError(errno.ENOSPC, "synthetic disk full"), KeyboardInterrupt()])
def test_raw_fsync_failure_leaves_recoverable_complete_bytes(tmp_path, monkeypatch, failure):
    raw, manifest = transfer()
    store = intake.LocalAuditStore(tmp_path / "store")
    fsync, injected = intake.os.fsync, []
    with monkeypatch.context() as patch:
        def fail_raw_fsync(fd):
            if not injected and _raw_part_fd(fd):
                injected.append(True)
                raise failure
            fsync(fd)
        patch.setattr(intake.os, "fsync", fail_raw_fsync)
        expected_error = intake.StorageError if isinstance(failure, OSError) else KeyboardInterrupt
        with pytest.raises(expected_error):
            store.receive(io.BytesIO(raw), manifest)
    assert injected
    restarted = intake.LocalAuditStore(store.root)
    restarted.recover()
    receipt = only_receipt(restarted)
    assert receipt["state"] == "PROVENANCE_INCOMPLETE" and receipt["recovered"]
    assert_archived_bytes(restarted, receipt, raw)


def test_enospc_mid_raw_write_preserves_prefix_for_restart(tmp_path, monkeypatch):
    raw, manifest = transfer()
    store = intake.LocalAuditStore(tmp_path / "store")
    fdopen = intake.os.fdopen
    written = []

    class InterruptedWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, data):
            prefix = data[:137]
            self.stream.write(prefix)
            written.append(prefix)
            raise OSError(errno.ENOSPC, "synthetic disk full")

        def __getattr__(self, name):
            return getattr(self.stream, name)

    with monkeypatch.context() as patch:
        def interrupt_fdopen(fd, *args, **kwargs):
            is_raw = _raw_part_fd(fd)
            stream = fdopen(fd, *args, **kwargs)
            return InterruptedWriter(stream) if is_raw else stream
        patch.setattr(intake.os, "fdopen", interrupt_fdopen)
        with pytest.raises(intake.StorageError):
            store.receive(io.BytesIO(raw), manifest)
    assert written == [raw[:137]]
    restarted = intake.LocalAuditStore(store.root)
    restarted.recover()
    receipt = only_receipt(restarted)
    assert receipt["state"] == "QUARANTINED" and receipt["recovered"]
    assert_archived_bytes(restarted, receipt, raw[:137])


def test_manifest_directory_fsync_failure_does_not_publish_a_receipt(tmp_path, monkeypatch):
    raw, manifest = transfer()
    store = intake.LocalAuditStore(tmp_path / "store")
    sync_dir = store._sync_dir
    with monkeypatch.context() as patch:
        def fail_manifest_directory(path):
            if Path(path).name == "manifests":
                raise OSError(errno.EIO, "synthetic sync failure")
            sync_dir(path)
        patch.setattr(store, "_sync_dir", fail_manifest_directory)
        with pytest.raises(intake.StorageError):
            store.receive(io.BytesIO(raw), manifest)
    assert not list((store.root / "receipts").iterdir())
    assert store.recover() == []
    receipt = store.receive(io.BytesIO(raw), manifest)
    assert receipt["state"] == "PROVENANCE_INCOMPLETE"


@pytest.mark.parametrize("directory", ["manifests", "blobs", "receipts", "quarantine"])
def test_link_visible_after_failed_directory_sync_is_resynced_before_reuse(tmp_path, monkeypatch, directory):
    """A visible link is not evidence that the preceding fsync succeeded.

    Fail after publication creates its exclusive link, then require recovery or
    retry to sync that existing file and its directory without replacing bytes.
    """
    raw, manifest = transfer()
    store = intake.LocalAuditStore(tmp_path / "store")
    original_sync, failed = store._sync_dir, []
    source = FailingSource(raw[:137], OSError("synthetic source failure")) if directory == "quarantine" else io.BytesIO(raw)
    with monkeypatch.context() as patch:
        def fail_publication_sync(path):
            if Path(path) == store.root / directory and not failed:
                assert list(Path(path).iterdir()), "fault must follow an actual link"
                failed.append(True)
                raise OSError(errno.EIO, "synthetic directory sync interruption")
            original_sync(path)
        patch.setattr(store, "_sync_dir", fail_publication_sync)
        with pytest.raises(intake.StorageError):
            store.receive(source, manifest)
    assert failed
    linked = next((store.root / directory).iterdir())
    before, inode = linked.read_bytes(), linked.stat().st_ino
    restarted = intake.LocalAuditStore(store.root)
    fsync, sync_dir = intake.os.fsync, restarted._sync_dir
    file_barriers, directory_barriers = [], []
    with monkeypatch.context() as patch:
        def watch_file_sync(fd):
            if Path(os.readlink(f"/proc/self/fd/{fd}")) == linked:
                file_barriers.append(True)
            fsync(fd)
        def watch_directory_sync(path):
            if Path(path) == linked.parent:
                directory_barriers.append(True)
            sync_dir(path)
        patch.setattr(intake.os, "fsync", watch_file_sync)
        patch.setattr(restarted, "_sync_dir", watch_directory_sync)
        if directory == "manifests":
            receipt = restarted.receive(io.BytesIO(raw), manifest)
        else:
            restarted.recover()
            receipt = only_receipt(restarted)
    assert file_barriers, "reusing an existing target must retry its file fsync"
    assert directory_barriers, "reusing an existing target must retry directory fsync"
    assert linked.read_bytes() == before and linked.stat().st_ino == inode
    assert receipt["state"] == ("QUARANTINED" if directory == "quarantine" else "PROVENANCE_INCOMPLETE")


@pytest.mark.parametrize("directory", ["blobs", "manifests", "receipts"])
def test_existing_immutable_object_tamper_blocks_reads_ack_and_new_receive(tmp_path, directory):
    raw, manifest = transfer()
    store = intake.LocalAuditStore(tmp_path / "store")
    receipt = store.receive(io.BytesIO(raw), manifest)
    path = next((store.root / directory).iterdir())
    original = path.read_bytes()
    if directory == "receipts":
        damaged = json.loads(original)
        damaged["stored_bytes"] += 1
        path.write_text(json.dumps(damaged), encoding="utf-8")
    else:
        path.write_bytes(original + b" ")
    damaged_bytes = path.read_bytes()
    for operation in [store.receipts, store.recover,
                      lambda: store.acknowledge(receipt["receipt_id"]),
                      lambda: store.receive(io.BytesIO(raw), manifest)]:
        with pytest.raises(intake.StorageError):
            operation()
    assert path.read_bytes() == damaged_bytes
    assert len(list((store.root / "receipts").glob("*.json"))) == 1


@pytest.mark.parametrize("damage", [
    "array", "null", "missing_field", "invalid_version", "nonboolean_error",
    "naive_timestamp", "invalid_date", "invalid_manifest_hash", "mismatched_attempt", "checksum",
])
def test_corrupt_restart_intent_is_rejected_without_finishing_or_deleting_source(tmp_path, monkeypatch, damage):
    raw, manifest = transfer()
    store = intake.LocalAuditStore(tmp_path / "store")
    with monkeypatch.context() as patch:
        def interrupt_finish(*args, **kwargs):
            raise KeyboardInterrupt
        patch.setattr(store, "_finish", interrupt_finish)
        with pytest.raises(KeyboardInterrupt):
            store.receive(io.BytesIO(raw), manifest)
    intent_path = next((store.root / "intents").glob("*.json"))
    intent = json.loads(intent_path.read_bytes())
    partial = store.root / "pending" / (intent["receipt_id"] + ".part")
    before = partial.read_bytes()
    if damage == "array":
        corrupted = []
    elif damage == "null":
        corrupted = None
    else:
        corrupted = dict(intent)
        if damage == "missing_field":
            del corrupted["manifest_sha256"]
        elif damage == "invalid_version":
            corrupted["intent_version"] = True
        elif damage == "nonboolean_error":
            corrupted["manifest_error"] = "false"
        elif damage == "naive_timestamp":
            corrupted["received_at"] = "2026-10-17T12:00:00.000000"
        elif damage == "invalid_date":
            corrupted["received_at"] = "2026-02-30T12:00:00.000000Z"
        elif damage == "invalid_manifest_hash":
            corrupted["manifest_sha256"] = "../not-a-content-hash"
        elif damage == "mismatched_attempt":
            corrupted["receipt_id"] = "0" * 32
        else:
            corrupted["intent_sha256"] = "f" * 64
        if damage != "checksum":
            # Exercise structural validation even with a self-consistent digest;
            # hashes do not authenticate arbitrary metadata supplied by an owner.
            content = {k: v for k, v in corrupted.items() if k != "intent_sha256"}
            canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
            corrupted["intent_sha256"] = hashlib.sha256(canonical).hexdigest()
    intent_path.write_text(json.dumps(corrupted), encoding="utf-8")
    with pytest.raises(intake.StoreCorrupt, match="CORRUPT_INTENT"):
        intake.LocalAuditStore(store.root).recover()
    assert partial.read_bytes() == before
    assert not list((store.root / "receipts").iterdir())


def test_independent_threads_share_one_record_claim_and_immutable_raw(tmp_path):
    raw, manifest = transfer()
    root = tmp_path / "store"
    intake.LocalAuditStore(root)
    barrier = Barrier(6)
    def receive_together():
        store = intake.LocalAuditStore(root)
        barrier.wait(timeout=10)
        return store.receive(io.BytesIO(raw), manifest)
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(receive_together) for _ in range(6)]
        receipts = [f.result(timeout=15) for f in futures]
    assert all(r["state"] == "PROVENANCE_INCOMPLETE" for r in receipts)
    assert sum(r["new_record_count"] for r in receipts) == 1
    assert sum(r["reused_record_count"] for r in receipts) == 5
    assert len({r["receipt_id"] for r in receipts}) == 6
    assert len(list((root / "blobs").iterdir())) == 1
    original = next(r for r in receipts if r["new_record_count"])
    assert {r["duplicate_of"] for r in receipts if not r["new_record_count"]} == {original["receipt_id"]}


@pytest.mark.parametrize("conflict", ["transfer", "record", "natural_key"])
def test_concurrent_conflict_preserves_one_winner_and_quarantines_other(tmp_path, conflict):
    first = ohlcv_record()
    second = deepcopy(first)
    if conflict in ("transfer", "natural_key"):
        second["record_id"] += "-other"
    if conflict in ("transfer", "record"):
        second["payload"]["close"] = "102.50"
        rehash(second)
    a = transfer([first], transfer_id="first")
    b = transfer([second], transfer_id="first" if conflict == "transfer" else "second")
    root = tmp_path / "store"
    intake.LocalAuditStore(root)
    barrier = Barrier(2)
    def receive_together(raw, manifest):
        store = intake.LocalAuditStore(root)
        barrier.wait(timeout=10)
        return store.receive(io.BytesIO(raw), manifest)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(receive_together, raw, manifest) for raw, manifest in [a, b]]
        receipts = [f.result(timeout=15) for f in futures]
    assert sorted(r["state"] for r in receipts) == ["PROVENANCE_INCOMPLETE", "QUARANTINED"]
    winner = next(r for r in receipts if r["state"] == "PROVENANCE_INCOMPLETE")
    rejected = next(r for r in receipts if r["state"] == "QUARANTINED")
    assert winner["new_record_count"] == 1 and rejected["new_record_count"] == 0
    expected = {"transfer": "TRANSFER_ID_CONFLICT", "record": "RECORD_ID_CONFLICT", "natural_key": "NATURAL_KEY_CONFLICT"}[conflict]
    assert expected in rejected["reason_codes"]
    assert len(intake.LocalAuditStore(root).receipts()) == 2


RECEIVER = """
import io,json,sys,time
from pathlib import Path
from analytics.python.data_contracts.local_ingestion import LocalAuditStore
root,raw_path,manifest_path,gate_path,ready_path=sys.argv[1:]
store=LocalAuditStore(root)
Path(ready_path).write_bytes(b'ready')
while not Path(gate_path).exists(): time.sleep(.005)
receipt=store.receive(io.BytesIO(Path(raw_path).read_bytes()), json.loads(Path(manifest_path).read_text()))
print(json.dumps({k:receipt[k] for k in ['receipt_id','state','new_record_count','reused_record_count']}))
"""


def _process_env():
    return {**os.environ, "PYTHONPATH": str(REPO_ROOT)}


def test_independent_processes_serialize_without_duplicate_record_claims(tmp_path):
    raw, manifest = transfer()
    root = tmp_path / "store"
    intake.LocalAuditStore(root)
    raw_path, manifest_path = tmp_path / "transfer.jsonl", tmp_path / "manifest.json"
    gate = tmp_path / "start"
    ready_paths = [tmp_path / f"ready-{index}" for index in range(4)]
    raw_path.write_bytes(raw)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    children = [subprocess.Popen([sys.executable, "-c", RECEIVER, str(root), str(raw_path), str(manifest_path), str(gate), str(ready_path)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_process_env(), cwd=REPO_ROOT)
                for ready_path in ready_paths]
    receipts = []
    try:
        deadline = time.monotonic() + 10
        while not all(path.exists() for path in ready_paths) and time.monotonic() < deadline:
            assert all(child.poll() is None for child in children), "receiver failed before the start barrier"
            time.sleep(0.01)
        assert all(path.exists() for path in ready_paths), "receivers did not reach the start barrier"
        gate.write_bytes(b"start")
        for child in children:
            output, error = child.communicate(timeout=20)
            assert child.returncode == 0, error.decode("utf-8", errors="replace")
            receipts.append(json.loads(output))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
    assert sum(r["new_record_count"] for r in receipts) == 1
    assert sum(r["reused_record_count"] for r in receipts) == 3
    assert len({r["receipt_id"] for r in receipts}) == 4
    assert all(r["state"] == "PROVENANCE_INCOMPLETE" for r in receipts)
    assert len(intake.LocalAuditStore(root).receipts()) == 4


KILL_RECEIVER = """
import io,json,signal,sys
from pathlib import Path
from analytics.python.data_contracts.local_ingestion import LocalAuditStore
root,raw_path,manifest_path,marker_path,mode=sys.argv[1:]
marker=Path(marker_path)
raw=Path(raw_path).read_bytes()
def wait_for_kill():
 marker.write_bytes(b'ready')
 while True: signal.pause()
class CrashStore(LocalAuditStore):
 def _finish(self,*args,**kwargs):
  if mode=='complete': wait_for_kill()
  return super()._finish(*args,**kwargs)
class PrefixStream:
 def __init__(self): self.sent=False
 def read(self,n):
  if not self.sent:
   self.sent=True
   return raw[:10000]
  wait_for_kill()
source=io.BytesIO(raw) if mode=='complete' else PrefixStream()
CrashStore(root).receive(source,json.loads(Path(manifest_path).read_text()))
"""


@pytest.mark.parametrize("mode", ["partial", "complete"])
def test_sigkill_releases_lock_and_restart_revalidates_exact_bytes(tmp_path, mode):
    records = []
    for index in range(20):
        record = ohlcv_record()
        record["record_id"] += f"-{index}"
        record["symbol"] = f"TEST{index}"
        records.append(record)
    raw, manifest = transfer(records)
    assert len(raw) > 10000
    root = tmp_path / "store"
    raw_path, manifest_path, marker = tmp_path / "transfer.jsonl", tmp_path / "manifest.json", tmp_path / "ready"
    raw_path.write_bytes(raw)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    child = subprocess.Popen([sys.executable, "-c", KILL_RECEIVER, str(root), str(raw_path), str(manifest_path), str(marker), mode],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_process_env(), cwd=REPO_ROOT)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), "child did not reach the durable/raw-prefix crash boundary"
        child.kill()
        _, error = child.communicate(timeout=10)
        assert child.returncode == -signal.SIGKILL, error.decode("utf-8", errors="replace")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    restarted = intake.LocalAuditStore(root)
    recovered = restarted.recover()
    assert len(recovered) == 1
    receipt = only_receipt(restarted)
    assert receipt["recovered"]
    expected = raw if mode == "complete" else raw[:10000]
    assert_archived_bytes(restarted, receipt, expected)
    assert receipt["state"] == ("PROVENANCE_INCOMPLETE" if mode == "complete" else "QUARANTINED")
    assert restarted.recover() == []
