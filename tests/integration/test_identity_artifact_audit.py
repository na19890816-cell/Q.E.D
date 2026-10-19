"""Synthetic artifact/identity boundary tests, not real exporter verification."""
from dataclasses import replace
import hashlib
import io
import json

import pytest

from analytics.python.data_contracts.identity_pit_audit import (
    AuditInputError, ResearchBlocked, audit_received_artifact, require_research_preconditions,
    require_received_research_preconditions,
)
from analytics.python.data_contracts.local_ingestion import LocalAuditStore, StoreCorrupt
from analytics.python.data_contracts.raw_json_artifact import RawJsonArtifactStore
from tests.fixtures.identity_pit_audit import synthetic_facts
from tests.fixtures.raw_json_artifact import artifact


def synthetic_transfer(facts, identity='synthetic-identity'):
    # Explicit fixture dictionary, NOT an adapter for unprovided ProStock2 files.
    raw = json.dumps({'合成標本':True, 'generated_at':facts['snapshot'].generated_at,
        'rows':[{'code':o.code,'company_name':o.company_name,'provider':o.provider,
                 'source_symbol':o.source_symbol,'market_date':o.market_date} for o in facts['observations']],
        'OVX':[{'reference_date':v.reference_date,'available_at':v.available_at} for v in facts['indicators']]},
        ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
    facts = {**facts, 'snapshot':replace(facts['snapshot'], raw_sha256=hashlib.sha256(raw).hexdigest())}
    return raw, artifact(raw, identity=identity)[1], facts


def test_raw_utf8_nonascii_kept_byte_for_byte_and_receipt_replay_verified(tmp_path):
    raw, manifest, facts = synthetic_transfer(synthetic_facts())
    assert '合成標本'.encode('utf-8') in raw and b'\\u5408' not in raw
    store = RawJsonArtifactStore(tmp_path / 'store')
    receipt = store.receive(io.BytesIO(raw), manifest)
    audit = audit_received_artifact(store, receipt['receipt_id'], **facts)
    assert audit['receipt_binding']['raw_sha256'] == manifest['raw_sha256']
    assert audit['receipt_binding']['size_bytes'] == len(raw)
    assert (store.root/'blobs'/(manifest['raw_sha256']+'.raw')).read_bytes() == raw
    assert receipt['state'] == 'PROVENANCE_INCOMPLETE' and not audit['research_eligible']
    retry = RawJsonArtifactStore(store.root).receive(io.BytesIO(raw), manifest)
    assert retry['duplicate_of'] == receipt['receipt_id']
    assert audit_received_artifact(store, retry['receipt_id'], **facts)['input_facts_sha256'] == audit['input_facts_sha256']


def test_contaminated_original_is_retained_but_research_is_blocked(tmp_path):
    facts = synthetic_facts()
    bad = replace(facts['observations'][0], source_symbol='2850.T')
    facts['observations'] = (bad,) + facts['observations'][1:]
    raw, manifest, facts = synthetic_transfer(facts)
    store = RawJsonArtifactStore(tmp_path / 'store')
    receipt = store.receive(io.BytesIO(raw), manifest)
    assert receipt['state'] == 'PROVENANCE_INCOMPLETE'  # storage grammar passes
    audit = audit_received_artifact(store, receipt['receipt_id'], **facts)
    assert audit['targets'][0]['status'] == 'IDENTITY_MISMATCH'
    with pytest.raises(ResearchBlocked):
        require_received_research_preconditions(store, receipt['receipt_id'], **facts)
    assert (store.root/'blobs'/(receipt['raw_sha256']+'.raw')).read_bytes() == raw
    assert store.receipts()[0] == receipt  # audit has no mutation/promotion side effect


def test_receipt_for_a_different_original_cannot_authorize_audit_facts(tmp_path):
    raw, manifest, facts = synthetic_transfer(synthetic_facts())
    store = RawJsonArtifactStore(tmp_path / 'store')
    receipt = store.receive(io.BytesIO(raw), manifest)
    facts['snapshot'] = replace(facts['snapshot'], raw_sha256='f'*64)
    with pytest.raises(AuditInputError, match='AUDIT_RAW_HASH_MISMATCH'):
        audit_received_artifact(store, receipt['receipt_id'], **facts)


def test_one_byte_alteration_rejected_and_not_usable_for_identity_audit(tmp_path):
    raw, manifest, facts = synthetic_transfer(synthetic_facts())
    store = RawJsonArtifactStore(tmp_path / 'store')
    tampered = raw.replace(b'285A', b'2850', 1)
    receipt = store.receive(io.BytesIO(tampered), manifest)
    assert receipt['state'] == 'QUARANTINED' and 'RAW_INTEGRITY_MISMATCH' in receipt['reason_codes']
    with pytest.raises(ValueError, match='ACK_NOT_PERMITTED'):
        audit_received_artifact(store, receipt['receipt_id'], **facts)


def test_corrupt_raw_prevents_audit_even_with_previously_valid_receipt(tmp_path):
    raw, manifest, facts = synthetic_transfer(synthetic_facts())
    store = RawJsonArtifactStore(tmp_path / 'store')
    receipt = store.receive(io.BytesIO(raw), manifest)
    (store.root/'blobs'/(receipt['raw_sha256']+'.raw')).write_bytes(b'{}')
    with pytest.raises(StoreCorrupt, match='RAW_HASH_MISMATCH'):
        audit_received_artifact(store, receipt['receipt_id'], **facts)


def test_same_market_day_revisions_are_separate_immutable_artifacts(tmp_path):
    store = RawJsonArtifactStore(tmp_path / 'store')
    receipts = []
    for revision in ('revision-1','revision-2'):
        facts = synthetic_facts()
        raw, manifest, facts = synthetic_transfer(facts, identity=revision)
        manifest['provenance'].update(market_date='2026-10-16', source_revision=revision)
        receipt = store.receive(io.BytesIO(raw), manifest)
        receipts.append(receipt)
        assert not audit_received_artifact(store, receipt['receipt_id'], **facts)['research_eligible']
    assert receipts[0]['manifest_sha256'] != receipts[1]['manifest_sha256']
    assert all(r['duplicate_of'] is None for r in receipts)
    assert len(store.receipts()) == 2


def test_original_and_derived_samples_remain_distinguishable(tmp_path):
    raw, manifest, facts = synthetic_transfer(synthetic_facts())
    store = RawJsonArtifactStore(tmp_path / 'store')
    original = store.receive(io.BytesIO(raw), manifest)
    derived_raw = json.dumps(json.loads(raw), ensure_ascii=True).encode('ascii')
    parent = {'raw_sha256':manifest['raw_sha256'],'transformation_code_sha256':'1'*64,
              'spec_sha256':'2'*64,'settings_sha256':'3'*64}
    derived_manifest = artifact(derived_raw, identity='synthetic-derived', role='derived', parents=[parent])[1]
    derived = store.receive(io.BytesIO(derived_raw), derived_manifest)
    derived_facts = {**facts,'snapshot':replace(facts['snapshot'],raw_sha256=hashlib.sha256(derived_raw).hexdigest())}
    oa = audit_received_artifact(store, original['receipt_id'], **facts)
    da = audit_received_artifact(store, derived['receipt_id'], **derived_facts)
    assert oa['receipt_binding']['artifact_role'] == 'original'
    assert da['receipt_binding']['artifact_role'] == 'derived'
    assert da['receipt_binding']['parent_raw_sha256'] == [manifest['raw_sha256']]
    assert raw != derived_raw and not da['research_eligible']


def test_jsonl_store_cannot_be_used_as_raw_json_audit_boundary(tmp_path):
    with pytest.raises(AuditInputError, match='RAW_JSON_ARTIFACT_STORE_REQUIRED'):
        audit_received_artifact(LocalAuditStore(tmp_path/'jsonl'), '0'*32, **synthetic_facts())


def test_missing_pit_and_issuer_evidence_do_not_get_filled_from_durable_receipt(tmp_path):
    facts = synthetic_facts()
    facts['snapshot'] = replace(facts['snapshot'], observed_at=None, observation_evidence_sha256=None)
    facts['universe'] = None
    facts['indicators'] = (replace(facts['indicators'][0], reference_date=None, available_at=None),)
    facts['search'] = replace(facts['search'], n_raw=None, origin='unknown', evidence_sha256=None)
    raw, manifest, facts = synthetic_transfer(facts)
    store = RawJsonArtifactStore(tmp_path/'store')
    receipt = store.receive(io.BytesIO(raw), manifest)
    report = audit_received_artifact(store, receipt['receipt_id'], **facts)
    assert report['snapshot_observed_at'] is None and report['n_raw'] is None
    assert report['indicators'][0]['reference_date'] is None and report['indicators'][0]['available_at'] is None
    assert all(t['status'] == 'IDENTITY_UNVERIFIED' for t in report['targets'])
    assert report['review_status'] == 'BLOCKED' and not report['research_eligible']
