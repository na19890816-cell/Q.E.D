"""Atomic S3 receipt + batch registration. Caller owns the outer transaction.

Unique constraints serialize concurrent imports, including relabeled evidence.
Any conflict rolls back the entire attempt, never leaving an orphan batch.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import psycopg

from analytics.python.frost.manual_trial_ledger import INVENTORY_HASH_VERSION, ManualTrialSubmission
from analytics.python.pg_io.postgres_lineage_bridge import _INSERT_BATCH_SQL


@dataclass(frozen=True)
class ManualImportResult:
    batch_id: str
    inserted: bool
    recorded_at: datetime


class ManualImportConflict(ValueError):
    """An identity or evidence digest has already been registered differently."""


def register_manual_trials(conn: psycopg.Connection, submission: ManualTrialSubmission) -> ManualImportResult:
    """Use database wall time, not transaction-start time or evidence time.

    Deliberately no recorded_at / as_of override and no fail-open on missing tables.
    recorded_at precedes commit: a wall-clock as_of alone cannot freeze MVCC history.
    """
    batch = submission.batch
    if batch.metadata.get("inventory_hash_version") != INVENTORY_HASH_VERSION:
        raise ValueError("unsupported inventory hash version; revalidate the original evidence")
    if not conn.autocommit and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
        # psycopg.transaction() on an idle connection commits a top-level
        # transaction. Establish the caller-owned transaction first so that
        # this helper only releases a savepoint, even on its first statement.
        conn.execute("SELECT 1")
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                # Same identity is serialized before checking its immutable receipt.
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (batch.batch_id,))
                # Old digests include completed_at and cannot be converted from
                # the digest alone. Never silently mix hash versions for a family.
                cur.execute(
                    "SELECT 1 FROM qed_manual_trial_imports r "
                    "JOIN qed_trial_batches b ON b.batch_id=r.batch_id "
                    "WHERE b.family_key=%s AND "
                    "b.metadata->>'inventory_hash_version' IS DISTINCT FROM %s LIMIT 1",
                    (batch.family_key, str(INVENTORY_HASH_VERSION)),
                )
                if cur.fetchone():
                    raise ManualImportConflict("legacy inventory hashes require audit before new imports for this family")
                cur.execute(
                    "SELECT batch_id, evidence_sha256, inventory_sha256, n_trials, recorded_at "
                    "FROM qed_manual_trial_imports WHERE source=%s AND research_run_id=%s",
                    (submission.source, submission.research_run_id),
                )
                existing = cur.fetchone()
                if existing:
                    if existing[:4] != (batch.batch_id, submission.evidence_sha256,
                                        submission.inventory_sha256, batch.n_trials):
                        raise ManualImportConflict("identity already registered with different evidence or N")
                    return ManualImportResult(batch.batch_id, False, existing[4])
                cur.execute("SELECT clock_timestamp()")
                recorded_at = cur.fetchone()[0]
                if submission.completed_at > recorded_at:
                    raise ValueError("completed_at is in the future according to the database clock")
                cur.execute(_INSERT_BATCH_SQL, (
                    batch.batch_id, batch.family_key, batch.run_id, batch.trace_id,
                    batch.source_type, batch.stage, batch.n_trials, 0, 0.0, 0.0,
                    batch.sr_periodicity, json.dumps(batch.family_spec), json.dumps(batch.metadata), recorded_at,
                ))
                if cur.rowcount != 1:
                    raise ManualImportConflict("batch identity already exists without a matching S3 receipt")
                cur.execute(
                    "INSERT INTO qed_manual_trial_imports "
                    "(source,research_run_id,batch_id,evidence_sha256,inventory_sha256,n_trials,"
                    "completed_at,recorded_at,submitted_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (submission.source, submission.research_run_id, batch.batch_id,
                     submission.evidence_sha256, submission.inventory_sha256, batch.n_trials,
                     submission.completed_at, recorded_at, submission.submitted_by),
                )
                return ManualImportResult(batch.batch_id, True, recorded_at)
    except psycopg.errors.UniqueViolation as exc:
        raise ManualImportConflict("evidence inventory already registered (possibly under another identity)") from exc
