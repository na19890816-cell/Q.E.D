#!/usr/bin/env python3
"""Capture observed ledger inputs once, or replay an append-only saved capture."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from analytics.python.pg_io.postgres_trial_snapshot_capture import (
    capture_context, capture_trial_snapshot, get_snapshot_capture,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    capture = actions.add_parser("capture", help="save the first visible result for an explicit read context")
    capture.add_argument("--family-key", required=True)
    capture.add_argument("--as-of", required=True, help="ISO-8601 timestamp with explicit timezone")
    capture.add_argument("--sr-periodicity")
    capture.add_argument("--direct-only", action="store_true", help="exclude ancestor families")
    replay = actions.add_parser("show", help="read saved inputs by capture ID")
    replay.add_argument("--capture-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "capture":
            cutoff = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
            capture_context(args.family_key, cutoff, args.sr_periodicity, not args.direct_only)
        if not os.environ.get("QED_PG_DSN"):
            raise ValueError("QED_PG_DSN must be set (migrations 084/087 required)")
        import psycopg
        try:
            with psycopg.connect(os.environ["QED_PG_DSN"], autocommit=True) as conn:
                if args.action == "capture":
                    result = capture_trial_snapshot(conn, args.family_key, cutoff,
                                                    args.sr_periodicity, not args.direct_only)
                else:
                    result = get_snapshot_capture(conn, args.capture_id)
        except psycopg.Error as exc:
            raise ValueError(f"snapshot database operation failed ({type(exc).__name__}); "
                             "check connectivity, permissions and migrations 084/087") from None
        print(json.dumps(result.to_dict(), sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, LookupError, OSError) as exc:
        print(f"snapshot capture rejected: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
