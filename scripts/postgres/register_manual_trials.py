#!/usr/bin/env python3
"""ADR-002 S3: validate evidence locally; --write-db atomically registers it."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from analytics.python.frost.manual_trial_ledger import INVENTORY_HASH_VERSION, parse_manual_evidence


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--n-trials", type=int, required=True)
    parser.add_argument("--submitted-by", required=True)
    parser.add_argument("--write-db", action="store_true", help="write using QED_PG_DSN (migration 086 required)")
    args = parser.parse_args(argv)
    try:
        submission = parse_manual_evidence(args.evidence.read_bytes(), args.n_trials, args.submitted_by)
        result = {"batch_id": submission.batch.batch_id, "family_key": submission.batch.family_key,
                  "n_trials": submission.batch.n_trials, "evidence_sha256": submission.evidence_sha256,
                  "inventory_hash_version": INVENTORY_HASH_VERSION,
                  "inventory_sha256": submission.inventory_sha256, "status": "validated_only"}
        if args.write_db:
            import psycopg
            from analytics.python.pg_io.postgres_manual_trial_bridge import register_manual_trials

            if not os.environ.get("QED_PG_DSN"):
                raise ValueError("QED_PG_DSN must be set for --write-db")
            try:
                with psycopg.connect(os.environ["QED_PG_DSN"]) as conn:
                    saved = register_manual_trials(conn, submission)
                    result.update(status="inserted" if saved.inserted else "already_registered",
                                  recorded_at=saved.recorded_at.isoformat())
            except psycopg.Error as exc:
                # DSNs may contain credentials; never echo raw connection errors.
                raise ValueError(f"database registration failed ({type(exc).__name__}); "
                                 "check connectivity, permissions and migrations 084/086") from None
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, UnicodeError) as exc:
        print(f"registration rejected: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
