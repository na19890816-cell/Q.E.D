"""Identity contamination/PIT denial tests, explicit synthetic reviewed facts."""
from copy import deepcopy
from dataclasses import replace

import pytest

from analytics.python.data_contracts.identity_pit_audit import (
    AuditInputError, ResearchBlocked, audit_snapshot, code_mutation_signals,
    require_research_preconditions,
)
from tests.fixtures.identity_pit_audit import synthetic_facts


def row(result, code):
    return next(r for r in result['observations'] if r['code'] == code)


def test_all_four_codes_are_distinct_and_no_positive_research_grant():
    facts = synthetic_facts()
    before = deepcopy(facts)
    result = require_research_preconditions(**facts)
    assert {r['code'] for r in result['targets']} == {'285A','2850','417A','4170'}
    assert all(r['status'] == 'CONSISTENT_WITH_SUPPLIED_EVIDENCE' for r in result['targets'])
    assert result['review_status'] == 'INDEPENDENT_REVIEW_REQUIRED'
    assert not result['research_eligible'] and not result['facts_authenticity_verified']
    assert facts == before


@pytest.mark.parametrize('letter,numeric', [('285A','2850'), ('417A','4170')])
def test_cross_issuer_market_symbol_is_mismatch_and_blocks_research(letter, numeric):
    facts = synthetic_facts()
    changed = replace(facts['observations'][0 if letter == '285A' else 2], source_symbol=numeric+'.T')
    facts['observations'] = (changed,)
    result = audit_snapshot(**facts)
    assert result['observations'][0]['identity_status'] == 'IDENTITY_MISMATCH'
    assert 'COLLISION_WITH_OTHER_OFFICIAL_CODE' in result['blocker_codes']
    assert 'ALPHA_REPLACED_WITH_ZERO' in result['blocker_codes']
    with pytest.raises(ResearchBlocked) as error:
        require_research_preconditions(**facts)
    assert 'SOURCE_SYMBOL_MISMATCH' in error.value.reason_codes


@pytest.mark.parametrize('expected,stored,signal', [
    ('285A','285','ALPHA_CHARACTERS_DROPPED'), ('417A','417','ALPHA_CHARACTERS_DROPPED'),
    ('285A','2850','ALPHA_REPLACED_WITH_ZERO'), ('417A','4170','ALPHA_REPLACED_WITH_ZERO'),
    ('285A','285','DIGITS_ONLY_EXTRACTION_POSSIBLE'), ('417A','417','DIGITS_ONLY_EXTRACTION_POSSIBLE')])
def test_lossy_stored_code_is_not_repaired_or_observed_as_original(expected, stored, signal):
    facts = synthetic_facts()
    original = facts['observations'][0 if expected == '285A' else 2]
    facts['observations'] = (replace(original, code=stored),)
    facts['target_codes'] = (expected,)
    result = audit_snapshot(**facts)
    assert result['targets'] == [{'code':expected, 'status':'NOT_OBSERVED', 'observed_records':0}]
    assert result['observations'][0]['code'] == stored
    assert result['observations'][0]['identity_status'] == 'IDENTITY_MISMATCH'
    assert signal in result['blocker_codes']


@pytest.mark.parametrize('code', ['285A','2850','417A','4170'])
def test_absent_target_not_observed_not_a_verified_clean_issuer(code):
    facts = synthetic_facts()
    facts['observations'] = tuple(o for o in facts['observations'] if o.code != code)
    result = audit_snapshot(**facts)
    assert next(t for t in result['targets'] if t['code'] == code)['status'] == 'NOT_OBSERVED'
    assert 'TARGET_NOT_OBSERVED' in result['blocker_codes']


@pytest.mark.parametrize('context', ['no_universe','unconfirmed_universe','unconfirmed_symbol_map','expired_universe'])
def test_missing_authority_never_infers_normal_identity(context):
    facts = synthetic_facts()
    if context == 'no_universe':
        facts['universe'] = None
    else:
        change = {'unconfirmed_universe':{'formally_provided':False},
                  'unconfirmed_symbol_map':{'symbol_mapping_confirmed':False},
                  'expired_universe':{'valid_through':'2026-10-15'}}[context]
        facts['universe'] = replace(facts['universe'], **change)
    result = audit_snapshot(**facts)
    assert all(t['status'] == 'IDENTITY_UNVERIFIED' for t in result['targets'])
    with pytest.raises(ResearchBlocked):
        require_research_preconditions(**facts)


@pytest.mark.parametrize('change', [dict(company_name='合成企業2850'), dict(source_symbol=None),
                                   dict(provider=None), dict(company_name=None), dict(source_symbol='285.T')])
def test_missing_name_or_source_is_never_silently_normalized(change):
    facts = synthetic_facts()
    facts['observations'] = (replace(facts['observations'][0], **change),)
    facts['target_codes'] = ('285A',)
    result = audit_snapshot(**facts)
    assert result['targets'][0]['status'] in ('IDENTITY_UNVERIFIED','IDENTITY_MISMATCH')
    assert result['review_status'] == 'BLOCKED'


@pytest.mark.parametrize('code', ['２８５Ａ','285a','285A ', '0285A'])
def test_confusables_case_whitespace_and_padding_do_not_alias_official_code(code):
    facts = synthetic_facts()
    facts['observations'] = (replace(facts['observations'][0], code=code),)
    facts['target_codes'] = ('285A',)
    result = audit_snapshot(**facts)
    assert result['targets'][0]['status'] == 'NOT_OBSERVED'
    assert result['observations'][0]['code'] == code


def test_ambiguous_formal_provider_mapping_fails_closed():
    facts = synthetic_facts()
    entries = list(facts['universe'].entries)
    entries[1] = replace(entries[1], source_symbol=entries[0].source_symbol)
    facts['universe'] = replace(facts['universe'], entries=tuple(entries))
    with pytest.raises(AuditInputError, match='AMBIGUOUS_UNIVERSE'):
        require_research_preconditions(**facts)


def test_duplicate_observation_not_deduplicated_into_success():
    facts = synthetic_facts()
    facts['observations'] += (facts['observations'][0],)
    result = audit_snapshot(**facts)
    assert len(result['observations']) == 5 and 'DUPLICATE_OBSERVATION' in result['blocker_codes']


@pytest.mark.parametrize('change,reason', [
    (None,'ORIGINAL_OHLCV_UNVERIFIED'), ({'matched':False},'ORIGINAL_OHLCV_UNVERIFIED'),
    ({'raw_sha256':None},'ORIGINAL_OHLCV_UNVERIFIED'),
    ({'comparison_evidence_sha256':None},'ORIGINAL_OHLCV_UNVERIFIED'),
    ({'code':'2850'},'OHLCV_IDENTITY_OR_DATE_MISMATCH'),
    ({'source_symbol':'2850.T'},'OHLCV_IDENTITY_OR_DATE_MISMATCH'),
    ({'market_date':'2026-10-15'},'OHLCV_IDENTITY_OR_DATE_MISMATCH')])
def test_unverified_or_wrong_ohlcv_blocks_even_when_record_identity_looks_clean(change, reason):
    facts = synthetic_facts()
    observation = facts['observations'][0]
    match = replace(observation.ohlcv, **change) if change is not None else None
    facts['observations'] = (replace(observation, ohlcv=match),)
    facts['target_codes'] = ('285A',)
    assert reason in audit_snapshot(**facts)['blocker_codes']
    with pytest.raises(ResearchBlocked):
        require_research_preconditions(**facts)


def test_snapshot_time_market_date_and_ovx_reference_date_remain_separate():
    result = audit_snapshot(**synthetic_facts())
    assert result['snapshot_observed_at'] == '2026-10-19T00:10:00Z'
    assert {r['market_date'] for r in result['observations']} == {'2026-10-16'}
    assert result['indicators'][0]['reference_date'] == '2026-10-15'
    assert not result['blocker_codes']


@pytest.mark.parametrize('change,reason', [
    ({'reference_date':None},'REFERENCE_DATE_UNVERIFIED'),
    ({'reference_date_evidence_sha256':None},'REFERENCE_DATE_UNVERIFIED'),
    ({'available_at':None},'AVAILABILITY_UNVERIFIED'),
    ({'availability_evidence_sha256':None},'AVAILABILITY_UNVERIFIED'),
    ({'availability_basis':'generated_at_only'},'AVAILABILITY_UNVERIFIED'),
    ({'provisional':True},'PROVISIONAL_VALUE'),
    ({'revision_status':'unknown'},'REVISION_HISTORY_UNVERIFIED'),
    ({'available_at':'2026-10-19T00:31:00Z'},'AVAILABLE_AFTER_OBSERVATION_OR_DECISION')])
def test_strict_pit_excludes_unknown_dates_unproven_availability_and_revisions(change, reason):
    facts = synthetic_facts()
    facts['indicators'] = (replace(facts['indicators'][0], **change),)
    result = audit_snapshot(**facts)
    assert result['indicators'][0]['strict_pit_status'] == 'EXCLUDED'
    assert reason in result['blocker_codes']
    assert result['indicators'][0]['reference_date'] == facts['indicators'][0].reference_date


def test_generated_at_is_never_fallback_for_observed_or_available_time():
    facts = synthetic_facts()
    facts['snapshot'] = replace(facts['snapshot'], observed_at=None, observation_evidence_sha256=None)
    facts['indicators'] = (replace(facts['indicators'][0], reference_date=None, available_at=None),)
    result = audit_snapshot(**facts)
    assert result['snapshot_observed_at'] is None
    assert result['indicators'][0]['available_at'] is None and result['indicators'][0]['reference_date'] is None
    assert result['generated_at'] == facts['snapshot'].generated_at
    assert result['review_status'] == 'BLOCKED'


def test_equal_generated_and_available_timestamp_requires_independent_evidence():
    facts = synthetic_facts()
    facts['indicators'] = (replace(facts['indicators'][0], available_at=facts['snapshot'].generated_at),)
    assert not audit_snapshot(**facts)['blocker_codes']
    facts['indicators'] = (replace(facts['indicators'][0], availability_basis='generated_at_only'),)
    assert 'AVAILABILITY_UNVERIFIED' in audit_snapshot(**facts)['blocker_codes']


@pytest.mark.parametrize('field', ['observed_at','generated_at'])
@pytest.mark.parametrize('value', ['2026-10-19T00:10:00', '2026-10-19T00:10:00+09:00'])
def test_naive_or_noncanonical_timestamps_fail_closed(field, value):
    facts = synthetic_facts()
    facts['snapshot'] = replace(facts['snapshot'], **{field:value})
    with pytest.raises(AuditInputError, match='INVALID_UTC_TIMESTAMP'):
        require_research_preconditions(**facts)


def test_missing_required_ovx_not_synthesized_from_snapshot_date():
    facts = synthetic_facts()
    facts['indicators'] = ()
    result = audit_snapshot(**facts)
    assert result['indicators'][0]['strict_pit_status'] == 'NOT_OBSERVED'
    assert result['indicators'][0]['reference_date'] is None


@pytest.mark.parametrize('n,origin,proof', [(None,'unknown',None), (1,'imputed','2'*64),
                                         (1,'unknown',None), (8,'ledger_verified',None)])
def test_unknown_or_imputed_search_n_blocks_and_is_not_filled(n, origin, proof):
    facts = synthetic_facts()
    facts['search'] = replace(facts['search'], n_raw=n, origin=origin, evidence_sha256=proof)
    result = audit_snapshot(**facts)
    assert result['n_raw'] == n and result['n_raw_origin'] == origin
    with pytest.raises(ResearchBlocked):
        require_research_preconditions(**facts)


@pytest.mark.parametrize('n', [0, -1, True, 1.0])
def test_invalid_n_not_accepted_as_success(n):
    facts = synthetic_facts()
    facts['search'] = replace(facts['search'], n_raw=n)
    with pytest.raises(AuditInputError, match='INVALID_EXPLORATION_N'):
        audit_snapshot(**facts)


def test_mutating_a_report_cannot_bypass_recomputed_research_preconditions():
    facts = synthetic_facts()
    facts['universe'] = None
    report = audit_snapshot(**facts)
    report['blocker_codes'] = []
    report['research_eligible'] = True
    with pytest.raises(ResearchBlocked):
        require_research_preconditions(**facts)


@pytest.mark.parametrize('change,reason', [
    ({'reference_timezone':None},'REFERENCE_TIMEZONE_UNVERIFIED'),
    ({'reference_date':'2026-10-20'},'REFERENCE_DATE_AFTER_AVAILABLE_EVIDENCE')])
def test_ovx_reference_date_cannot_be_future_dated_or_have_unknown_timezone(change, reason):
    facts = synthetic_facts()
    facts['indicators'] = (replace(facts['indicators'][0], **change),)
    report = audit_snapshot(**facts)
    assert report['indicators'][0]['strict_pit_status'] == 'EXCLUDED'
    assert reason in report['blocker_codes']


@pytest.mark.parametrize('field', ['market_timezone','calendar_version'])
def test_unconfirmed_market_calendar_blocks_without_inferring_utc_dates(field):
    facts = synthetic_facts()
    facts['universe'] = replace(facts['universe'], **{field:None})
    assert 'MARKET_CALENDAR_UNVERIFIED' in audit_snapshot(**facts)['blocker_codes']


def test_future_market_day_ohlcv_does_not_pass_using_only_issuer_identity():
    facts = synthetic_facts()
    observation = facts['observations'][0]
    facts['observations'] = (replace(observation, market_date='2026-10-20',
        ohlcv=replace(observation.ohlcv, market_date='2026-10-20')),)
    facts['target_codes'] = ('285A',)
    assert 'MARKET_DATE_AFTER_OBSERVATION_OR_DECISION' in audit_snapshot(**facts)['blocker_codes']


def test_japan_market_day_is_compared_in_declared_market_timezone():
    facts = synthetic_facts()
    # 00:10 UTC is not required: Japan's next market date may precede UTC midnight.
    facts['snapshot'] = replace(facts['snapshot'], observed_at='2026-10-18T23:10:00Z')
    facts['decision_at'] = '2026-10-18T23:30:00Z'
    observation = facts['observations'][0]
    facts['observations'] = (replace(observation, market_date='2026-10-19',
        ohlcv=replace(observation.ohlcv, market_date='2026-10-19')),)
    facts['target_codes'] = ('285A',)
    assert not audit_snapshot(**facts)['blocker_codes']


@pytest.mark.parametrize('code', [2850, True, None])
def test_nonstring_stored_code_is_rejected_without_coercion(code):
    facts = synthetic_facts()
    facts['observations'] = (replace(facts['observations'][0], code=code),)
    with pytest.raises(AuditInputError):
        audit_snapshot(**facts)


@pytest.mark.parametrize('change', [{'inventory_complete':False}, {'extraction_spec_sha256':None}])
def test_unprovided_or_partial_inventory_does_not_prove_target_absence(change):
    facts = synthetic_facts()
    facts['snapshot'] = replace(facts['snapshot'], **change)
    facts['observations'] = ()
    report = audit_snapshot(**facts)
    assert all(t['status'] == 'OBSERVATION_COVERAGE_UNVERIFIED' for t in report['targets'])
    assert not report['observation_inventory_complete']
