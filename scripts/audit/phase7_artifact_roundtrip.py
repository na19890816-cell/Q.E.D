"""Reproduce synthetic raw bytes -> durable receipts/ACK in a NEW local root.

No input from EC2, network, real producer, DB, research or trading operations.
Keep --work-dir for inspection; never pass a production storage directory.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from analytics.python.data_contracts.raw_json_artifact import RawJsonArtifactStore
from analytics.python.data_contracts.local_ingestion import _canonical, _now, _sha
from tests.fixtures.raw_json_artifact import artifact


def write_new(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    RawJsonArtifactStore._sync_dir(path.parent)


def run(work):
    work.mkdir(mode=0o700)  # no reuse/overwrite of previous evidence
    RawJsonArtifactStore._sync_dir(work.parent)
    raw, manifest = artifact(identity='synthetic-phase7-roundtrip')
    manifest['exported_at'] = _now()
    write_new(work / 'synthetic-original.json', raw)
    write_new(work / 'producer-complete-manifest.json', _canonical(manifest))
    store = RawJsonArtifactStore(work / 'receiver')
    receipt = store.receive(io.BytesIO(raw), manifest)
    assert receipt['raw_sha256'] == manifest['raw_sha256'] and receipt['stored_bytes'] == len(raw)
    ack = store.acknowledge(receipt['receipt_id'])
    restarted = RawJsonArtifactStore(store.root)
    assert restarted.recover() == []
    replay = restarted.receive(io.BytesIO(raw), manifest)
    assert replay['duplicate_of'] == receipt['receipt_id']
    altered = raw.replace(b'TEST', b'BEST')
    bad = restarted.receive(io.BytesIO(altered), manifest)
    assert bad['state'] == 'QUARANTINED'
    assert (store.root/'blobs'/(receipt['raw_sha256']+'.raw')).read_bytes() == raw
    result = {'synthetic_only':True, 'raw_sha256':_sha(raw), 'size_bytes':len(raw),
              'manifest_sha256':receipt['manifest_sha256'], 'receipt':receipt, 'durable_ack':ack,
              'replay_receipt':replay, 'one_byte_tamper_receipt':bad,
              'real_producer_interoperability':'NOT_EXERCISED', 'research_eligible':False}
    write_new(work / 'roundtrip-result.json', _canonical(result))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.work_dir.resolve()), indent=2))
