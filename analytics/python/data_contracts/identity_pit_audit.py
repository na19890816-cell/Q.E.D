"""Opt-in, deny-first identity/PIT audit facts, independent of wire contracts.

Inputs are explicit reviewed audit facts, NOT guessed fields of ProStock2 JSON.
Evidence hashes bind supplied facts; they cannot establish authority/completeness.
No DB/network, repair of raw data, N imputation, or research grant is performed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import hashlib
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class AuditInputError(ValueError):
    """Fixed code only; do not leak raw content/source exception messages."""


class ResearchBlocked(RuntimeError):
    def __init__(self, reasons):
        self.reason_codes = tuple(sorted(set(reasons)))
        super().__init__("RESEARCH_INPUTS_BLOCKED")


@dataclass(frozen=True)
class IdentityEntry:
    code: str
    company_name: str
    provider: str
    source_symbol: str


@dataclass(frozen=True)
class UniverseEvidence:
    version: str
    raw_sha256: str
    valid_from: str
    valid_through: str
    entries: tuple[IdentityEntry, ...]
    formally_provided: bool = False
    symbol_mapping_confirmed: bool = False
    market_timezone: str | None = None
    calendar_version: str | None = None


@dataclass(frozen=True)
class OhlcvMatch:
    raw_sha256: str | None
    comparison_evidence_sha256: str | None
    code: str | None
    provider: str | None
    source_symbol: str | None
    market_date: str | None
    matched: bool = False


@dataclass(frozen=True)
class Observation:
    record_id: str
    code: str
    company_name: str | None
    provider: str | None
    source_symbol: str | None
    market_date: str | None
    ohlcv: OhlcvMatch | None = None


@dataclass(frozen=True)
class SnapshotEvidence:
    raw_sha256: str
    observed_at: str | None
    observation_evidence_sha256: str | None
    generated_at: str | None = None
    inventory_complete: bool = False
    extraction_spec_sha256: str | None = None


@dataclass(frozen=True)
class PitValue:
    series: str
    reference_date: str | None
    reference_date_evidence_sha256: str | None
    available_at: str | None
    availability_evidence_sha256: str | None
    availability_basis: str  # source_evidence / generated_at_only / unknown
    provisional: bool
    revision_status: str  # history_verified / unknown
    reference_timezone: str | None = None


@dataclass(frozen=True)
class SearchEvidence:
    n_raw: int | None
    origin: str  # ledger_verified / unknown / imputed
    evidence_sha256: str | None


def _text(value, *, optional=False):
    if optional and value is None:
        return
    if type(value) is not str or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise AuditInputError("INVALID_AUDIT_TEXT")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise AuditInputError("INVALID_AUDIT_TEXT") from None


def _hash(value, *, optional=False):
    if optional and value is None:
        return
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise AuditInputError("INVALID_EVIDENCE_HASH")


def _date(value):
    if value is None:
        return None
    try:
        if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise AuditInputError("INVALID_MARKET_DATE") from None


def _time(value):
    if value is None:
        return None
    try:
        if type(value) is not str or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value
        ):
            raise ValueError
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AuditInputError("INVALID_UTC_TIMESTAMP") from None


def _zone(value):
    if value is None:
        return None
    _text(value)
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise AuditInputError("INVALID_REFERENCE_TIMEZONE") from None


def code_mutation_signals(expected, observed):
    """Detect lossy transforms for diagnostics; NEVER return a corrected code.

    Alpha removal and digits-only extraction have indistinguishable outputs for
    some codes. Report both possibilities, without claiming which operation ran.
    """
    _text(expected)
    _text(observed)
    if expected == observed or not re.fullmatch(r"[0-9A-Z]{4}", expected):
        return ()
    letters = any("A" <= c <= "Z" for c in expected)
    reasons = []
    if letters and observed == "".join(c for c in expected if "0" <= c <= "9"):
        reasons.extend(("ALPHA_CHARACTERS_DROPPED", "DIGITS_ONLY_EXTRACTION_POSSIBLE"))
    if letters and observed == "".join("0" if "A" <= c <= "Z" else c for c in expected):
        reasons.append("ALPHA_REPLACED_WITH_ZERO")
    return tuple(reasons)


def _universe_maps(universe):
    if universe is None:
        return {}, {}
    if type(universe) is not UniverseEvidence or type(universe.entries) is not tuple:
        raise AuditInputError("INVALID_UNIVERSE")
    _text(universe.version)
    _hash(universe.raw_sha256)
    start, end = _date(universe.valid_from), _date(universe.valid_through)
    if start is None or end is None or start > end:
        raise AuditInputError("INVALID_UNIVERSE_PERIOD")
    if type(universe.formally_provided) is not bool or type(universe.symbol_mapping_confirmed) is not bool:
        raise AuditInputError("INVALID_UNIVERSE_VERIFICATION")
    _zone(universe.market_timezone)
    _text(universe.calendar_version, optional=True)
    codes, symbols = {}, {}
    for entry in universe.entries:
        if type(entry) is not IdentityEntry:
            raise AuditInputError("INVALID_UNIVERSE_ENTRY")
        for item in (entry.code, entry.company_name, entry.provider, entry.source_symbol):
            _text(item)
        if not re.fullmatch(r"[0-9A-Z]{4}", entry.code):
            raise AuditInputError("INVALID_OFFICIAL_CODE")
        key = (entry.provider, entry.source_symbol)
        if entry.code in codes or key in symbols:
            raise AuditInputError("AMBIGUOUS_UNIVERSE")
        codes[entry.code], symbols[key] = entry, entry
    return codes, symbols


def _identity(observation, universe, codes, symbols):
    reasons = []
    if type(observation) is not Observation:
        raise AuditInputError("INVALID_OBSERVATION")
    for item in (observation.record_id, observation.code):
        _text(item)
    for item in (observation.company_name, observation.provider, observation.source_symbol):
        _text(item, optional=True)
    market_date = _date(observation.market_date)
    formal = bool(universe and universe.formally_provided and universe.symbol_mapping_confirmed)
    if not formal:
        return "IDENTITY_UNVERIFIED", ["FORMAL_UNIVERSE_OR_SYMBOL_MAPPING_UNVERIFIED"]
    if market_date is None or not _date(universe.valid_from) <= market_date <= _date(universe.valid_through):
        return "IDENTITY_UNVERIFIED", ["IDENTITY_MARKET_DATE_UNVERIFIED"]
    expected = codes.get(observation.code)
    named = [e for e in codes.values() if e.company_name == observation.company_name]
    source = symbols.get((observation.provider, observation.source_symbol))
    if expected is None:
        reasons.append("CODE_NOT_IN_FORMAL_UNIVERSE")
        for entry in named:
            reasons.extend(code_mutation_signals(entry.code, observation.code))
        # A known issuer/name with a different stored code is a contradiction.
        return ("IDENTITY_MISMATCH" if named else "IDENTITY_UNVERIFIED"), sorted(set(reasons))
    mismatch = False
    if observation.company_name is None:
        reasons.append("COMPANY_NAME_UNVERIFIED")
    elif observation.company_name != expected.company_name:
        mismatch = True
        reasons.append("COMPANY_NAME_MISMATCH")
        for entry in named:
            reasons.extend(code_mutation_signals(entry.code, observation.code))
            if entry.code != observation.code:
                reasons.append("COLLISION_WITH_OTHER_OFFICIAL_CODE")
    if source is not None and source.code != expected.code:
        mismatch = True
        reasons.extend(("SOURCE_SYMBOL_MISMATCH", "COLLISION_WITH_OTHER_OFFICIAL_CODE"))
        reasons.extend(code_mutation_signals(expected.code, source.code))
    elif (observation.provider != expected.provider or observation.source_symbol != expected.source_symbol):
        reasons.append("SOURCE_SYMBOL_UNVERIFIED")
    if mismatch:
        return "IDENTITY_MISMATCH", sorted(set(reasons))
    return ("IDENTITY_UNVERIFIED", reasons) if reasons else ("CONSISTENT_WITH_SUPPLIED_EVIDENCE", [])


def _ohlcv(observation):
    match = observation.ohlcv
    if match is None:
        return ["ORIGINAL_OHLCV_UNVERIFIED"]
    if type(match) is not OhlcvMatch or type(match.matched) is not bool:
        raise AuditInputError("INVALID_OHLCV_MATCH")
    _hash(match.raw_sha256, optional=True)
    _hash(match.comparison_evidence_sha256, optional=True)
    for value in (match.code, match.provider, match.source_symbol):
        _text(value, optional=True)
    _date(match.market_date)
    if any(getattr(match, field) != getattr(observation, field)
           for field in ("code", "provider", "source_symbol", "market_date")):
        return ["OHLCV_IDENTITY_OR_DATE_MISMATCH"]
    if (not match.matched or match.raw_sha256 is None or match.comparison_evidence_sha256 is None
            or any(getattr(match, field) is None for field in ("code", "provider", "source_symbol", "market_date"))):
        return ["ORIGINAL_OHLCV_UNVERIFIED"]
    return []


def audit_snapshot(*, snapshot, observations, target_codes, universe, indicators,
                   required_series, decision_at, search):
    """Pure classifications and denial reasons; no positive research grant.

    Formal/proof flags are reviewed caller assertions. Raw receipt or a hash alone
    cannot supply them. Unknown actual file layouts must not be guessed here.
    """
    if type(snapshot) is not SnapshotEvidence or type(search) is not SearchEvidence:
        raise AuditInputError("INVALID_AUDIT_CONTEXT")
    for sequence in (observations, target_codes, indicators, required_series):
        if type(sequence) is not tuple or len(sequence) > 10_000:
            raise AuditInputError("INVALID_AUDIT_SEQUENCE")
    for code in target_codes:
        _text(code)
        if not re.fullmatch(r"[0-9A-Z]{4}", code):
            raise AuditInputError("INVALID_TARGET_CODE")
    for series in required_series:
        _text(series)
    if not target_codes or len(set(target_codes)) != len(target_codes) or len(set(required_series)) != len(required_series):
        raise AuditInputError("INVALID_EXPECTED_SET")
    _hash(snapshot.raw_sha256)
    _hash(snapshot.observation_evidence_sha256, optional=True)
    _hash(snapshot.extraction_spec_sha256, optional=True)
    if type(snapshot.inventory_complete) is not bool:
        raise AuditInputError("INVALID_INVENTORY_STATUS")
    inventory_complete = snapshot.inventory_complete and snapshot.extraction_spec_sha256 is not None
    observed, generated, decision = _time(snapshot.observed_at), _time(snapshot.generated_at), _time(decision_at)
    if decision is None:
        raise AuditInputError("DECISION_TIME_REQUIRED")
    blockers = set()
    if not inventory_complete:
        blockers.add("OBSERVATION_INVENTORY_UNVERIFIED")
    if observed is None or snapshot.observation_evidence_sha256 is None:
        blockers.add("SNAPSHOT_OBSERVATION_TIME_UNVERIFIED")
    if observed is not None and observed > decision:
        blockers.add("SNAPSHOT_OBSERVED_AFTER_DECISION")
    codes, symbols = _universe_maps(universe)
    market_zone = _zone(universe.market_timezone) if universe else None
    if market_zone is None or universe.calendar_version is None:
        blockers.add("MARKET_CALENDAR_UNVERIFIED")
    rows = []
    record_ids, natural_keys = set(), set()
    for observation in observations:
        status, reasons = _identity(observation, universe, codes, symbols)
        natural = (observation.code, observation.market_date)
        if observation.record_id in record_ids or natural in natural_keys:
            reasons.append("DUPLICATE_OBSERVATION")
        record_ids.add(observation.record_id)
        natural_keys.add(natural)
        reasons.extend(_ohlcv(observation))
        if observation.market_date is None:
            reasons.append("MARKET_DATE_UNVERIFIED")
        elif market_zone is not None:
            market_day = _date(observation.market_date)
            if (market_day > decision.astimezone(market_zone).date()
                    or (observed is not None and market_day > observed.astimezone(market_zone).date())):
                reasons.append("MARKET_DATE_AFTER_OBSERVATION_OR_DECISION")
        if "OHLCV_IDENTITY_OR_DATE_MISMATCH" in reasons:
            status = "IDENTITY_MISMATCH"
        blockers.update(reasons)
        rows.append({"record_id": observation.record_id, "code": observation.code,
                     "market_date": observation.market_date, "identity_status": status,
                     "reason_codes": sorted(set(reasons))})
    targets = []
    for code in target_codes:
        matching = [row for row in rows if row["code"] == code]  # exact stored code only
        if not matching:
            status = "NOT_OBSERVED" if inventory_complete else "OBSERVATION_COVERAGE_UNVERIFIED"
            blockers.add("TARGET_NOT_OBSERVED" if inventory_complete else "OBSERVATION_INVENTORY_UNVERIFIED")
        elif any(row["identity_status"] == "IDENTITY_MISMATCH" for row in matching):
            status = "IDENTITY_MISMATCH"
        elif any(row["identity_status"] == "IDENTITY_UNVERIFIED" for row in matching):
            status = "IDENTITY_UNVERIFIED"
        else:
            status = "CONSISTENT_WITH_SUPPLIED_EVIDENCE"
        targets.append({"code": code, "status": status, "observed_records": len(matching)})
    series_results, seen_series = [], set()
    for indicator in indicators:
        if type(indicator) is not PitValue:
            raise AuditInputError("INVALID_PIT_VALUE")
        _text(indicator.series)
        reference = _date(indicator.reference_date)
        reference_zone = _zone(indicator.reference_timezone)
        available = _time(indicator.available_at)
        _hash(indicator.reference_date_evidence_sha256, optional=True)
        _hash(indicator.availability_evidence_sha256, optional=True)
        if (type(indicator.provisional) is not bool or indicator.availability_basis not in
                ("source_evidence", "generated_at_only", "unknown") or indicator.revision_status not in
                ("history_verified", "unknown")):
            raise AuditInputError("INVALID_PIT_STATUS")
        reasons = []
        if indicator.series in seen_series:
            reasons.append("DUPLICATE_SERIES")
        seen_series.add(indicator.series)
        if reference is None or indicator.reference_date_evidence_sha256 is None:
            reasons.append("REFERENCE_DATE_UNVERIFIED")
        if reference_zone is None:
            reasons.append("REFERENCE_TIMEZONE_UNVERIFIED")
        elif reference is not None and any(reference > stamp.astimezone(reference_zone).date()
                                           for stamp in (available, observed, decision) if stamp is not None):
            reasons.append("REFERENCE_DATE_AFTER_AVAILABLE_EVIDENCE")
        if (available is None or indicator.availability_evidence_sha256 is None
                or indicator.availability_basis != "source_evidence"):
            reasons.append("AVAILABILITY_UNVERIFIED")
        if available is not None and (available > decision or (observed is not None and available > observed)):
            reasons.append("AVAILABLE_AFTER_OBSERVATION_OR_DECISION")
        if indicator.provisional:
            reasons.append("PROVISIONAL_VALUE")
        if indicator.revision_status != "history_verified":
            reasons.append("REVISION_HISTORY_UNVERIFIED")
        blockers.update(reasons)
        series_results.append({"series": indicator.series, "reference_date": indicator.reference_date,
                               "available_at": indicator.available_at,
                               "strict_pit_status": "EXCLUDED" if reasons else "CONSISTENT_WITH_SUPPLIED_EVIDENCE",
                               "reason_codes": sorted(set(reasons))})
    for missing in sorted(set(required_series) - seen_series):
        blockers.add("REQUIRED_SERIES_NOT_OBSERVED")
        series_results.append({"series": missing, "reference_date": None, "available_at": None,
                               "strict_pit_status": "NOT_OBSERVED", "reason_codes": ["REQUIRED_SERIES_NOT_OBSERVED"]})
    _hash(search.evidence_sha256, optional=True)
    if search.n_raw is not None and (type(search.n_raw) is not int or search.n_raw < 1):
        raise AuditInputError("INVALID_EXPLORATION_N")
    if search.origin not in ("ledger_verified", "unknown", "imputed"):
        raise AuditInputError("INVALID_EXPLORATION_N_ORIGIN")
    if search.origin == "imputed":
        blockers.add("IMPUTED_EXPLORATION_N")
    if search.n_raw is None or search.origin != "ledger_verified" or search.evidence_sha256 is None:
        blockers.add("EXPLORATION_N_UNVERIFIED")
    facts = {"snapshot": asdict(snapshot), "observations": [asdict(o) for o in observations],
             "target_codes": target_codes, "universe": asdict(universe) if universe else None,
             "indicators": [asdict(i) for i in indicators], "required_series": required_series,
             "decision_at": decision_at, "search": asdict(search)}
    facts_hash = hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()
    return {"audit_contract": "urn:qed:audit:identity-pit:1", "audit_schema_version": 1,
            "input_facts_sha256": facts_hash, "facts_authenticity_verified": False,
            "raw_sha256": snapshot.raw_sha256,
            "observation_inventory_complete": inventory_complete,
            "snapshot_observed_at": snapshot.observed_at, "generated_at": snapshot.generated_at,
            "decision_at": decision_at, "targets": targets, "observations": rows, "indicators": series_results,
            "universe_version": universe.version if universe else None,
            "market_timezone": universe.market_timezone if universe else None,
            "calendar_version": universe.calendar_version if universe else None,
            "n_raw": search.n_raw, "n_raw_origin": search.origin,
            "blocker_codes": sorted(blockers), "research_eligible": False,
            "review_status": "BLOCKED" if blockers else "INDEPENDENT_REVIEW_REQUIRED"}


def require_research_preconditions(**facts):
    """Recompute from facts; never trust a supplied report's flags or ready state."""
    result = audit_snapshot(**facts)
    if result["blocker_codes"]:
        raise ResearchBlocked(result["blocker_codes"])
    return result  # still research_eligible=False; separate authorization needed


def audit_received_artifact(store, receipt_id, **facts):
    """Bind audit facts to PR #8's verified durable copy; do not infer PIT facts."""
    from .raw_json_artifact import RawJsonArtifactStore
    if not isinstance(store, RawJsonArtifactStore):
        raise AuditInputError("RAW_JSON_ARTIFACT_STORE_REQUIRED")
    ack = store.acknowledge(receipt_id)  # quarantined/corrupt/incomplete input fails closed
    if type(facts.get("snapshot")) is not SnapshotEvidence or facts["snapshot"].raw_sha256 != ack["raw_sha256"]:
        raise AuditInputError("AUDIT_RAW_HASH_MISMATCH")
    result = audit_snapshot(**facts)
    result["receipt_binding"] = {key: ack[key] for key in
        ("receipt_id", "receipt_sha256", "manifest_sha256", "raw_sha256", "size_bytes")}
    receipt = next(r for r in store.receipts() if r["receipt_id"] == receipt_id)
    result["receipt_binding"].update(artifact_role=receipt["artifact_role"],
                                     parent_raw_sha256=receipt["parent_raw_sha256"])
    return result


def require_received_research_preconditions(store, receipt_id, **facts):
    """One opt-in preflight binds the receipt and denies contaminated inputs.

    Passing is not authorization: the returned report still cannot grant research
    eligibility. Existing FROST paths are not silently reconfigured.
    """
    result = audit_received_artifact(store, receipt_id, **facts)
    if result["blocker_codes"]:
        raise ResearchBlocked(result["blocker_codes"])
    return result
