"""Synthetic Phase 8C demonstration in a NEW local directory. No real inputs.

Uses an explicit synthetic dictionary, never guesses actual snapshot fields.
No network, EC2, DB, FROST, research adoption or trading operations.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from analytics.python.data_contracts.identity_pit_audit import (
    ResearchBlocked, audit_received_artifact, require_received_research_preconditions,
)
from analytics.python.data_contracts.local_ingestion import _canonical, _now, _sha
from analytics.python.data_contracts.raw_json_artifact import RawJsonArtifactStore
from scripts.audit.phase7_artifact_roundtrip import write_new
from tests.fixtures.identity_pit_audit import synthetic_facts
from tests.fixtures.raw_json_artifact import artifact


def scenarios():
    yield 'consistent_synthetic_facts', synthetic_facts()
    for code, wrong in [('285A','2850'), ('417A','4170')]:
        facts = synthetic_facts()
        facts['observations'] = tuple(replace(o, source_symbol=wrong+'.T') if o.code == code else o
                                      for o in facts['observations'])
        yield code+'_wrong_symbol', facts
    facts = synthetic_facts()
    facts['observations'] = (replace(facts['observations'][0], code='2850'),) + facts['observations'][1:]
    yield 'zero_replacement_collision', facts
    facts = synthetic_facts()
    facts['observations'] = tuple(o for o in facts['observations'] if o.code != '285A')
    yield 'target_not_observed', facts
    facts = synthetic_facts()
    facts['universe'] = replace(facts['universe'], formally_provided=False, symbol_mapping_confirmed=False)
    yield 'authority_not_confirmed', facts
    facts = synthetic_facts()
    facts['indicators'] = (replace(facts['indicators'][0], reference_date=None, reference_date_evidence_sha256=None,
        available_at=None, availability_evidence_sha256=None, availability_basis='unknown', revision_status='unknown'),)
    yield 'undated_ovx', facts
    facts = synthetic_facts()
    facts['search'] = replace(facts['search'], n_raw=None, origin='unknown', evidence_sha256=None)
    yield 'unknown_exploration_n', facts
    facts = synthetic_facts()
    facts['search'] = replace(facts['search'], n_raw=1, origin='imputed', evidence_sha256=None)
    yield 'imputed_exploration_n', facts
    facts = synthetic_facts()
    facts['observations'] = tuple(replace(o, ohlcv=None) for o in facts['observations'])
    yield 'original_ohlcv_unavailable', facts


def run(work):
    work.mkdir(mode=0o700)
    RawJsonArtifactStore._sync_dir(work.parent)
    store = RawJsonArtifactStore(work/'receiver')
    reports = {}
    for name, facts in scenarios():
        raw = json.dumps({'synthetic_only':True, 'generated_at':facts['snapshot'].generated_at,
            'rows':[{'code':o.code, 'company_name':o.company_name, 'provider':o.provider,
                     'source_symbol':o.source_symbol, 'market_date':o.market_date} for o in facts['observations']],
            'OVX':[{'reference_date':i.reference_date, 'available_at':i.available_at} for i in facts['indicators']]},
            ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
        facts['snapshot'] = replace(facts['snapshot'], raw_sha256=_sha(raw))
        manifest = artifact(raw, identity='synthetic-'+name)[1]
        manifest['exported_at'] = _now()
        write_new(work/(name+'.original.json'), raw)
        write_new(work/(name+'.manifest.json'), _canonical(manifest))
        serialized_facts = {k:asdict(v) if hasattr(v,'__dataclass_fields__') else
            [asdict(x) if hasattr(x,'__dataclass_fields__') else x for x in v] if type(v) is tuple else v
            for k,v in facts.items()}
        write_new(work/(name+'.audit-facts.json'), _canonical(serialized_facts))
        receipt = store.receive(io.BytesIO(raw), manifest)
        report = audit_received_artifact(store, receipt['receipt_id'], **facts)
        try:
            preflight = require_received_research_preconditions(store, receipt['receipt_id'], **facts)
            assert name == 'consistent_synthetic_facts' and not preflight['research_eligible']
            report['synthetic_preflight'] = 'INDEPENDENT_REVIEW_REQUIRED'
        except ResearchBlocked:
            assert name != 'consistent_synthetic_facts'
            report['synthetic_preflight'] = 'BLOCKED'
        assert not report['research_eligible']
        assert (store.root/'blobs'/(_sha(raw)+'.raw')).read_bytes() == raw
        write_new(work/(name+'.derived-audit-report.json'), _canonical(report))
        reports[name] = report
    assert reports['285A_wrong_symbol']['targets'][0]['status'] == 'IDENTITY_MISMATCH'
    assert reports['417A_wrong_symbol']['targets'][2]['status'] == 'IDENTITY_MISMATCH'
    assert reports['zero_replacement_collision']['targets'][0]['status'] == 'NOT_OBSERVED'
    assert reports['target_not_observed']['targets'][0]['status'] == 'NOT_OBSERVED'
    assert reports['undated_ovx']['indicators'][0]['reference_date'] is None
    assert reports['undated_ovx']['indicators'][0]['strict_pit_status'] == 'EXCLUDED'
    assert reports['unknown_exploration_n']['n_raw'] is None
    result = {'synthetic_only':True, 'real_samples_received':0, 'real_identity_findings':'NOT_ASSESSED',
              'real_producer_interoperability':'HOLD_NOT_EXERCISED', 'research_eligible':False,
              'production_connection':False, 'scenarios':reports}
    write_new(work/'synthetic-probe-result.json', _canonical(result))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.work_dir.resolve()), indent=2))
