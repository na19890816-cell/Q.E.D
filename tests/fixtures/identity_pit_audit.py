"""Wholly synthetic issuers, provenance and N; never real ProStock2 history."""
from analytics.python.data_contracts.identity_pit_audit import (
    IdentityEntry, UniverseEvidence, OhlcvMatch, Observation, SnapshotEvidence,
    PitValue, SearchEvidence,
)


def synthetic_facts():
    entries = tuple(IdentityEntry(code, '合成企業' + code, 'synthetic-provider', code + '.T')
                    for code in ('285A', '2850', '417A', '4170'))
    observations = tuple(Observation(code, code, entry.company_name, entry.provider,
        entry.source_symbol, '2026-10-16', OhlcvMatch('b'*64, 'c'*64, code, entry.provider,
        entry.source_symbol, '2026-10-16', True)) for code, entry in zip(('285A','2850','417A','4170'), entries))
    return {
        'snapshot': SnapshotEvidence('a'*64, '2026-10-19T00:10:00Z', 'd'*64, '2026-10-19T00:09:00Z',
                                     True, '3'*64),
        'observations': observations, 'target_codes': ('285A','2850','417A','4170'),
        'universe': UniverseEvidence('synthetic-universe', 'e'*64, '2026-10-01', '2026-10-31', entries, True, True,
                                     'Asia/Tokyo', 'synthetic-calendar'),
        'indicators': (PitValue('OVX', '2026-10-15', 'f'*64, '2026-10-16T00:00:00Z', '1'*64,
                                'source_evidence', False, 'history_verified', 'America/New_York'),),
        'required_series': ('OVX',), 'decision_at':'2026-10-19T00:30:00Z',
        'search': SearchEvidence(8, 'ledger_verified', '2'*64),
    }
