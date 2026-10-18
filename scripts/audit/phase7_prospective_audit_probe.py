"""Opt-in local synthetic preflight probe against a checkout containing PR #6.

No production launcher is changed. The independent registry below is a test
fixture, not a deployed run inventory or external trusted checkpoint.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile


class PreflightRejected(RuntimeError):
    pass


def require_started_audit(run_id, registry, audit, read_events):
    """Before calling a research operation, match a separately supplied registry.

    This standalone example is NOT wired into EML/FROST or a trading launcher.
    The registry must be fixed before runs; unknown/unregistered runs fail closed.
    """
    expected = registry.get(run_id)
    if expected is None:
        raise PreflightRejected("RUN_NOT_REGISTERED")
    if audit is None:
        raise PreflightRejected("AUDIT_REQUIRED")
    try:
        audit.require_run(run_id)
        events = read_events(audit.path)
        if (len(events) != 1 or events[0]["event_type"] != "RUN_STARTED"
                or events[0]["context"] != expected["context"]
                or events[0]["data"]["settings"] != expected["settings"]):
            raise PreflightRejected("AUDIT_START_MISMATCH")
    except PreflightRejected:
        raise
    except Exception:
        raise PreflightRejected("AUDIT_UNVERIFIABLE") from None


def run(tree):
    sys.path.insert(0, str(tree))
    from analytics.python.audit.exploration_events import ExplorationAudit, inspect_attempt, read_events
    from analytics.python.audit import exploration_events as journal
    results = {}
    with tempfile.TemporaryDirectory(prefix="qed-phase7-prospective-") as directory:
        root = Path(directory)
        context = dict(search_id="synthetic-start", run_id="synthetic-run", family_key="fam:" + "a"*32,
                       policy_hash="b"*64, code_commit="c"*40, input_evidence_sha256="d"*64,
                       historical_n_raw=None)
        settings = {"synthetic_only": True, "weight": 1}
        registry = {context["run_id"]: {"context": context, "settings": settings},
                    "synthetic-missing-run": {"context": {}, "settings": {}}}
        calls = []
        def operation(run_id, audit):
            require_started_audit(run_id, registry, audit, read_events)
            calls.append(run_id)

        for label, run_id in [("unspecified_audit_blocked", context["run_id"]),
                              ("unregistered_run_blocked", "unknown")]:
            try:
                operation(run_id, None)
                raise AssertionError("preflight incorrectly allowed operation")
            except PreflightRejected:
                results[label] = True
        assert not calls
        audit = ExplorationAudit.create(root, settings=settings, **context)
        operation(context["run_id"], audit)
        assert calls == [context["run_id"]]
        results["durable_start_before_operation"] = True
        report = audit.finish()
        assert report["historical_n_raw"] is None and report["ledger_writes"] == 0
        results["unknown_historical_n_preserved"] = True
        present_runs = {read_events(audit.path)[0]["context"]["run_id"]}
        missing = sorted(set(registry)-present_runs)
        assert missing == ["synthetic-missing-run"]
        results["missing_audit_run_detected"] = missing
        research_released = not missing
        assert not research_released
        results["missing_run_stops_research_release"] = True
        try:
            operation(context["run_id"], audit)
            raise AssertionError("completed audit reused")
        except PreflightRejected:
            results["completed_attempt_reuse_blocked"] = True
        # Real journal tampering must prevent the callback, not merely warn.
        before = len(calls)
        next_context = {**context, "search_id":"synthetic-tamper", "run_id":"synthetic-tamper"}
        registry[next_context["run_id"]] = {"context": next_context, "settings": settings}
        damaged = ExplorationAudit.create(root, settings=settings, **next_context)
        first = next(damaged.path.glob("*.json"))
        first.chmod(0o600)
        first.write_bytes(b'{}')
        try:
            operation(next_context["run_id"], damaged)
            raise AssertionError("damaged audit allowed")
        except PreflightRejected:
            results["corrupt_start_blocks_operation"] = len(calls) == before
        # Inject a real writer publication failure; no subsequent evaluation.
        failure_context = {**context, "search_id":"synthetic-write-fail", "run_id":"synthetic-write-fail"}
        failed = ExplorationAudit.create(root, settings=settings, **failure_context)
        scope = failed.start_scope("synthetic", settings={})
        def io_failure(*args, **kwargs):
            raise OSError("injected synthetic storage fault")
        evaluated = []
        real_link = journal.os.link
        try:
            journal.os.link = io_failure
            try:
                failed.start_trial(scope, "trial", evidence={}, counting_owner="S1", n_raw_role="candidate")
                evaluated.append(True)
            except journal.AuditError:
                pass
        finally:
            journal.os.link = real_link
        assert not evaluated
        try:
            failed.note("EVALUATION_STARTED", {})
            raise AssertionError("writer reopened after failure")
        except journal.AuditError:
            pass
        results["trial_start_write_failure_blocks_evaluation"] = True
        results["synthetic_only"] = True
        results["operational_connection"] = False
        results["global_unregistered_run_detection"] = False
        results["external_registry_authentication"] = False
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.tree.resolve()), indent=2))
