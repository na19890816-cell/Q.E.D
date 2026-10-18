"""Synthetic local POSIX interoperability and fault tests; no real producer."""
from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

from analytics.python.data_contracts.raw_json_artifact import RawJsonArtifactStore, ArtifactLimits
from analytics.python.data_contracts.local_ingestion import LocalAuditStore, StorageError, StoreCorrupt, _canonical, _sha
from tests.fixtures.raw_json_artifact import artifact


def receive(store, raw=None, manifest=None):
    if raw is None:
        raw, manifest = artifact()
    return store.receive(io.BytesIO(raw), manifest)


def assert_original(store, receipt, raw):
    assert (store.root / 'blobs' / (receipt['raw_sha256'] + '.raw')).read_bytes() == raw
    assert receipt['raw_sha256'] == hashlib.sha256(raw).hexdigest()
    assert not receipt['research_eligible'] and 'RESEARCH_ELIGIBLE' not in receipt['state_history']


def test_manifest_raw_durable_receipt_replay_and_lost_ack(tmp_path):
    store = RawJsonArtifactStore(tmp_path / 'store')
    raw, manifest = artifact()
    first = receive(store, raw, manifest)
    assert first['state'] == 'PROVENANCE_INCOMPLETE'
    assert all(first['checks'].values())
    assert_original(store, first, raw)
    assert first['manifest_sha256'] == _sha(_canonical(manifest))
    assert not first['logical_record_count_verified']
    restarted = RawJsonArtifactStore(store.root)
    assert restarted.recover() == []
    replay = receive(restarted, raw, manifest)
    assert replay['duplicate_of'] == first['receipt_id']
    ack = restarted.acknowledge(replay['receipt_id'])
    assert ack['raw_sha256'] == manifest['raw_sha256'] and ack['size_bytes'] == len(raw)
    assert not ack['research_eligible'] and not ack['producer_deletion_authorized']
    assert manifest['provenance']['available_at'] is None
    assert replay['received_at'] != manifest['exported_at']


def test_one_byte_tamper_and_same_id_new_content(tmp_path):
    store = RawJsonArtifactStore(tmp_path / 'store')
    raw, manifest = artifact()
    first = receive(store, raw, manifest)
    tampered = raw.replace(b'TEST', b'BEST')
    bad = receive(store, tampered, manifest)
    assert 'RAW_INTEGRITY_MISMATCH' in bad['reason_codes']
    changed, changed_manifest = artifact(tampered)
    conflict = receive(store, changed, changed_manifest)
    assert 'ARTIFACT_ID_CONFLICT' in conflict['reason_codes']
    assert_original(store, first, raw)
    assert_original(store, conflict, tampered)
    with pytest.raises(ValueError, match='ACK_NOT_PERMITTED'):
        store.acknowledge(conflict['receipt_id'])


def test_same_date_different_revision_and_metadata_conflict(tmp_path):
    store = RawJsonArtifactStore(tmp_path / 'store')
    for identity, revision in [('observation-1', 'rev1'), ('observation-2', 'rev2')]:
        raw, manifest = artifact(identity=identity)
        manifest['provenance'].update(market_date='2026-10-16', source_revision=revision)
        assert receive(store, raw, manifest)['state'] == 'PROVENANCE_INCOMPLETE'
    manifest['provenance']['source_revision'] = 'rev3'
    assert 'ARTIFACT_ID_CONFLICT' in receive(store, raw, manifest)['reason_codes']


@pytest.mark.parametrize('raw', [b'{"a":', b'{"x":NaN}', b'{"x":1,"x":2}', b'{}\n{}\n'])
def test_invalid_document_stays_raw_and_quarantined(tmp_path, raw):
    store = RawJsonArtifactStore(tmp_path / 'store')
    raw, manifest = artifact(raw)
    receipt = receive(store, raw, manifest)
    assert receipt['state'] == 'QUARANTINED'
    assert_original(store, receipt, raw)


def test_invalid_manifest_is_durably_rejected(tmp_path):
    store = RawJsonArtifactStore(tmp_path / 'store')
    raw, manifest = artifact()
    del manifest['complete']
    receipt = receive(store, raw, manifest)
    assert receipt['state'] == 'QUARANTINED' and receipt['manifest_sha256'] is None
    assert_original(store, receipt, raw)


def test_derived_requires_durable_parent_and_preserves_both_bytes(tmp_path):
    store = RawJsonArtifactStore(tmp_path / 'store')
    original, original_manifest = artifact()
    parent = {'raw_sha256':_sha(original), 'transformation_code_sha256':'a'*64,
              'spec_sha256':'b'*64, 'settings_sha256':'c'*64}
    derived, manifest = artifact(b'{"derived":true}', identity='derived', role='derived', parents=[parent])
    failed = receive(store, derived, manifest)
    assert failed['state'] == 'QUARANTINED' and failed['missing_parent_sha256'] == [_sha(original)]
    first = receive(store, original, original_manifest)
    retry = receive(store, derived, manifest)
    assert retry['state'] == 'PROVENANCE_INCOMPLETE' and retry['parent_raw_sha256'] == [_sha(original)]
    assert_original(store, first, original)
    assert_original(store, retry, derived)


class Interrupted:
    def __init__(self, prefix, failure):
        self.prefix, self.failure = prefix, failure

    def read(self, maximum):
        if self.prefix:
            prefix, self.prefix = self.prefix, b''
            return prefix
        raise self.failure


@pytest.mark.parametrize('failure', [OSError('SECRET must not leak'), KeyboardInterrupt()])
def test_interruption_prefix_recovery_and_full_retry(tmp_path, failure):
    store = RawJsonArtifactStore(tmp_path / 'store')
    raw, manifest = artifact()
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            store.receive(Interrupted(raw[:12], failure), manifest)
    else:
        assert store.receive(Interrupted(raw[:12], failure), manifest)['state'] == 'QUARANTINED'
    restarted = RawJsonArtifactStore(store.root)
    restarted.recover()
    receipts = restarted.receipts()
    assert len(receipts) == 1 and receipts[0]['state'] == 'QUARANTINED'
    assert_original(store, receipts[0], raw[:12])
    assert 'SECRET' not in json.dumps(receipts)
    assert receive(restarted, raw, manifest)['state'] == 'PROVENANCE_INCOMPLETE'


def test_complete_pending_recovered_after_finish_crash(tmp_path, monkeypatch):
    store = RawJsonArtifactStore(tmp_path / 'store')
    with monkeypatch.context() as patch:
        patch.setattr(store, '_finish', lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            receive(store)
    recovered = RawJsonArtifactStore(store.root).recover()
    assert len(recovered) == 1 and recovered[0]['recovered']
    assert recovered[0]['state'] == 'PROVENANCE_INCOMPLETE'


@pytest.mark.parametrize('directory', ['blobs', 'receipts'])
def test_directory_fsync_failure_does_not_ack_until_retry(tmp_path, monkeypatch, directory):
    store = RawJsonArtifactStore(tmp_path / 'store')
    original = store._sync_dir
    with monkeypatch.context() as patch:
        def fail(path):
            if Path(path).name == directory:
                raise OSError(errno.EIO, 'injected')
            original(path)
        patch.setattr(store, '_sync_dir', fail)
        with pytest.raises(StorageError, match='STORAGE_IO_FAILURE'):
            receive(store)
    restarted = RawJsonArtifactStore(store.root)
    restarted.recover()
    assert restarted.acknowledge(restarted.receipts()[0]['receipt_id'])['research_eligible'] is False


def test_enospc_leaves_recoverable_intent(tmp_path, monkeypatch):
    store = RawJsonArtifactStore(tmp_path / 'store')
    original = store._publish
    with monkeypatch.context() as patch:
        def fail(directory, name, data):
            if directory == 'blobs':
                raise OSError(errno.ENOSPC, 'injected')
            original(directory, name, data)
        patch.setattr(store, '_publish', fail)
        with pytest.raises(StorageError):
            receive(store)
    assert len(list((store.root / 'intents').iterdir())) == 1
    assert not list((store.root / 'receipts').iterdir())
    assert RawJsonArtifactStore(store.root).recover()[0]['state'] == 'PROVENANCE_INCOMPLETE'


def test_capacity_fail_closed(tmp_path):
    store = RawJsonArtifactStore(tmp_path / 'store', limits=ArtifactLimits(store_bytes=1000, reserve_bytes=0))
    with pytest.raises(StorageError, match='INSUFFICIENT_STORAGE'):
        receive(store)
    assert store.receipts() == []


def test_thread_concurrency_serialized_and_idempotent(tmp_path):
    root = tmp_path / 'store'
    RawJsonArtifactStore(root)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: receive(RawJsonArtifactStore(root)), range(8)))
    assert len([r for r in receipts if r['duplicate_of'] is None]) == 1
    assert len({r['raw_sha256'] for r in receipts}) == 1


@pytest.mark.parametrize('directory', ['blobs', 'manifests', 'receipts', 'intents'])
def test_tamper_detection(tmp_path, directory):
    store = RawJsonArtifactStore(tmp_path / 'store')
    receive(store)
    path = next((store.root / directory).iterdir())
    path.write_bytes(b'{}')
    with pytest.raises(StoreCorrupt):
        RawJsonArtifactStore(store.root).recover()


def test_forged_research_receipt_rejected_even_with_new_checksum(tmp_path):
    store = RawJsonArtifactStore(tmp_path / 'store')
    receipt = receive(store)
    path = store.root / 'receipts' / (receipt['receipt_id'] + '.json')
    receipt['state'] = 'RESEARCH_ELIGIBLE'
    receipt.pop('receipt_sha256')
    receipt['receipt_sha256'] = _sha(_canonical(receipt))
    path.write_bytes(_canonical(receipt))
    with pytest.raises(StoreCorrupt):
        store.receipts()


def test_local_copy_restore_and_ack(tmp_path):
    store = RawJsonArtifactStore(tmp_path / 'source')
    receipt = receive(store)
    shutil.copytree(store.root, tmp_path / 'restored')
    restored = RawJsonArtifactStore(tmp_path / 'restored')
    restored.recover()
    assert restored.acknowledge(receipt['receipt_id'])['receipt_sha256'] == receipt['receipt_sha256']
    assert restored.receipts() == store.receipts()


def test_receipt_publication_succeeded_but_reply_lost(tmp_path, monkeypatch):
    store = RawJsonArtifactStore(tmp_path / 'store')
    publish = store._publish
    with monkeypatch.context() as patch:
        def lost_reply(directory, name, data):
            publish(directory, name, data)
            if directory == 'receipts':
                raise KeyboardInterrupt
        patch.setattr(store, '_publish', lost_reply)
        with pytest.raises(KeyboardInterrupt):
            receive(store)
    restarted = RawJsonArtifactStore(store.root)
    assert restarted.recover() == []
    receipt = restarted.receipts()[0]
    assert receive(restarted)['duplicate_of'] == receipt['receipt_id']


def test_interrupted_raw_file_fsync_and_retry(tmp_path, monkeypatch):
    store = RawJsonArtifactStore(tmp_path / 'store')
    real_fsync = os.fsync
    with monkeypatch.context() as patch:
        def fail(fd):
            target = Path(os.readlink('/proc/self/fd/' + str(fd)))
            if target.parent.name == 'pending' and not target.name.startswith('publish-'):
                raise OSError(errno.EIO, 'synthetic write barrier failure')
            real_fsync(fd)
        patch.setattr(os, 'fsync', fail)
        with pytest.raises(StorageError):
            receive(store)
    restarted = RawJsonArtifactStore(store.root)
    recovered = restarted.recover()
    assert recovered[0]['raw_complete'] and recovered[0]['recovered']
    assert receive(restarted)['duplicate_of'] == recovered[0]['receipt_id']


def test_oversize_prefix_is_quarantined(tmp_path):
    store = RawJsonArtifactStore(tmp_path / 'store', limits=ArtifactLimits(file_bytes=8))
    raw, manifest = artifact(b'{"value":12345}')
    receipt = receive(store, raw, manifest)
    assert receipt['state'] == 'QUARANTINED' and not receipt['raw_complete']
    assert receipt['stored_bytes'] == 9
    assert_original(store, receipt, raw[:9])


def test_damaged_parent_prevents_derived_receipt_and_ack(tmp_path):
    store = RawJsonArtifactStore(tmp_path / 'store')
    raw, manifest = artifact()
    receipt = receive(store, raw, manifest)
    (store.root / 'blobs' / (receipt['raw_sha256'] + '.raw')).write_bytes(b'{}')
    with pytest.raises(StoreCorrupt, match='RAW_HASH_MISMATCH'):
        store.acknowledge(receipt['receipt_id'])
    with pytest.raises(StoreCorrupt):
        receive(store)


def test_concurrent_independent_processes(tmp_path):
    root = tmp_path / 'store'
    RawJsonArtifactStore(root)
    worker = '''
import io, json, sys
from analytics.python.data_contracts.raw_json_artifact import RawJsonArtifactStore
from tests.fixtures.raw_json_artifact import artifact
raw, manifest = artifact()
print(json.dumps(RawJsonArtifactStore(sys.argv[1]).receive(io.BytesIO(raw), manifest)))
'''
    processes = [subprocess.Popen([sys.executable, '-c', worker, str(root)],
        cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
    try:
        receipts = []
        for process in processes:
            output, error = process.communicate(timeout=10)
            assert process.returncode == 0, error.decode()
            receipts.append(json.loads(output))
        assert sum(receipt['duplicate_of'] is None for receipt in receipts) == 1
        assert len(RawJsonArtifactStore(root).receipts()) == 4
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)


def test_cannot_adopt_nonempty_jsonl_store(tmp_path):
    root = tmp_path / 'store'
    LocalAuditStore(root)
    (root / 'intents' / 'foreign.json').write_bytes(b'{}')
    with pytest.raises(StoreCorrupt, match='NONEMPTY_FOREIGN_STORE'):
        RawJsonArtifactStore(root)


@pytest.mark.parametrize('complete', [False, True])
def test_actual_sigkill_and_restart(tmp_path, complete):
    # The worker signals only after fsync of a complete or partial stream file.
    root = tmp_path / 'store'
    ready = tmp_path / 'ready'
    worker = r'''
import io, os, sys, time
from pathlib import Path
from analytics.python.data_contracts.raw_json_artifact import RawJsonArtifactStore
from tests.fixtures.raw_json_artifact import artifact
store = RawJsonArtifactStore(sys.argv[1])
ready = Path(sys.argv[2])
raw, manifest = artifact()
def block(*args, **kwargs):
    ready.write_text('ready')
    while True: time.sleep(.02)
if sys.argv[3] == 'complete':
    store._finish = block
    store.receive(io.BytesIO(raw), manifest)
else:
    class Source:
        sent = False
        def read(self, maximum):
            if not self.sent:
                self.sent = True
                return raw[:12]
            path = next((store.root/'pending').glob('*.part'))
            # The Python writer buffer is not flushed during a blocked read.
            # Force a crash before flush: recovery must retain only disk bytes.
            block()
    store.receive(Source(), manifest)
'''
    process = subprocess.Popen([sys.executable, '-c', worker, str(root), str(ready),
                                'complete' if complete else 'partial'], cwd=Path(__file__).resolve().parents[2],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), process.communicate(timeout=1)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        recovered = RawJsonArtifactStore(root).recover()
        assert len(recovered) == 1 and recovered[0]['recovered']
        assert recovered[0]['state'] == ('PROVENANCE_INCOMPLETE' if complete else 'QUARANTINED')
        assert receive(RawJsonArtifactStore(root))['state'] == 'PROVENANCE_INCOMPLETE'
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
