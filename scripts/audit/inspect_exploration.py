#!/usr/bin/env python3
"""Verify an opt-in local exploration journal. Does not connect to any DB."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from analytics.python.audit.exploration_events import AuditError, inspect_attempt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("attempt_directory", type=Path)
    args = parser.parse_args(argv)
    try:
        report = inspect_attempt(args.attempt_directory)
    except (AuditError, OSError):
        print(json.dumps({"status": "INVALID", "reason": "AUDIT_INTEGRITY_FAILED"}))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["state"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
