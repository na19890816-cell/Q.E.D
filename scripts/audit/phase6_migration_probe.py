"""Actual ledger migrations in a NEW disposable local PostgreSQL database.

Uses a caller-selected Unix socket and the dedicated control DB qed_phase6.
Never reads a general-purpose DSN, uses TCP, or creates external-parent stubs.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import subprocess
from types import SimpleNamespace
import uuid


NAMES = ("084_qed_trial_ledger.sql", "086_qed_manual_trial_imports.sql",
         "087_qed_trial_snapshot_captures.sql", "088_qed_trial_ledger_no_truncate.sql",
         "089_qed_manual_trial_evaluations.sql")
TABLES = ("qed_trial_batches", "qed_lineage_edges", "qed_manual_trial_imports",
          "qed_trial_snapshot_captures", "qed_manual_trial_evaluations")


def run(tree, socket_dir, audit_root):
    import psycopg
    from psycopg import sql
    sys.path.insert(0, str(tree))
    from analytics.python.alpha.eml.eml_search import SearchStats
    from analytics.python.alpha.eml.eml_lineage import trial_batches_from_eml_output
    from analytics.python.frost.frost_run_lineage import trial_batches_from_frost_output
    from analytics.python.frost.manual_trial_ledger import parse_manual_evidence
    from analytics.python.pg_io.postgres_lineage_bridge import insert_trial_batch, fetch_trial_snapshot
    from analytics.python.pg_io.postgres_manual_trial_bridge import register_manual_trials
    from analytics.python.pg_io.postgres_trial_snapshot_capture import capture_trial_snapshot, get_snapshot_capture
    from tests.unit.test_adr002_s3_manual_ledger import evidence_doc, raw
    from tests.fixtures.frost_synthetic import make_synthetic_candidates
    from analytics.python.audit.exploration_events import ExplorationAudit, evidence_hash
    from analytics.python.frost.frost_config import FrostConfig
    from analytics.python.frost.frost_runner import run_frost_pipeline
    from analytics.python.frost.policy_spec import policy_spec_from_frost_config
    from analytics.python.frost.frost_lineage import make_family_key

    if not socket_dir.is_absolute() or not (socket_dir / ".s.PGSQL.5432").is_socket():
        raise ValueError("LOCAL_TEST_SOCKET_REQUIRED")
    kwargs = dict(host=str(socket_dir), port=5432, user="postgres", dbname="qed_phase6", autocommit=True)
    name = "qed_phase6_probe_" + uuid.uuid4().hex
    result = {"synthetic_only": True, "external_parent_stubs": False,
              "migration_order": list(NAMES)}
    created = False
    with psycopg.connect(**kwargs) as control:
        try:
            control.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            created = True
            with psycopg.connect(**{**kwargs, "dbname": name}) as conn:
                conn.execute("CREATE TABLE _migrations(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())")
                missing = {}
                for filename in NAMES[1:]:
                    try:
                        with conn.transaction(force_rollback=True):
                            conn.execute((tree / "qedschema/migrations" / filename).read_text())
                    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedFunction) as error:
                        missing[filename] = type(error).__name__
                    else:
                        raise AssertionError("MISSING_PREREQUISITE_ACCEPTED")
                result["missing_prerequisites_rejected"] = missing
                conn.execute((tree / "qedschema/migrations" / NAMES[0]).read_text())
                conn.execute((tree / "qedschema/migrations" / NAMES[2]).read_text())
                doc = evidence_doc()
                doc["family"].update(universe="SYNTHETIC_MATRIX", target="probe")
                submission = parse_manual_evidence(raw(doc), 2, "synthetic-probe")
                first = capture_trial_snapshot(conn, submission.batch.family_key,
                                               datetime(2026, 1, 1, tzinfo=timezone.utc))
                assert first.snapshot.n_trials == 0
                for filename in (NAMES[1], NAMES[3], NAMES[4]):
                    conn.execute((tree / "qedschema/migrations" / filename).read_text())

                st = SearchStats(stage="exhaustive", n_generated=5, n_evaluated=5,
                    n_invalid=0, n_fitness_failed=0, n_returned=1, n_distinct_expr=3)
                eml = SimpleNamespace(run_id="synthetic-s1", trace_id="synthetic", target_horizon="5d",
                    universe="SYNTHETIC_MATRIX", target_name="probe", terminal_set_hash="",
                    search_stats=[st], candidates=[], eval_results=[], batch_label="synthetic")
                s1 = trial_batches_from_eml_output(eml)
                candidates, evaluations = [], []
                for index, key in enumerate(("a", "a", "b", "eml-excluded")):
                    candidate_id = f"synthetic-candidate-{index}"
                    candidates.append(SimpleNamespace(candidate_id=candidate_id, candidate_hash=key,
                        source_type="eml" if index == 3 else "technical", horizon="5d",
                        feature_spec_json={"universe": "SYNTHETIC_MATRIX", "target": "probe"}))
                    evaluations.append(SimpleNamespace(candidate_id=candidate_id, oos_sharpe=0.5))
                s2 = trial_batches_from_frost_output(SimpleNamespace(run_id="synthetic-s2", trace_id="synthetic",
                    candidates=candidates, evaluations=evaluations, dry_run=True))
                assert [batch.n_trials for batch in s1] == [3]
                assert [batch.n_trials for batch in s2] == [2]
                assert s1[0].sr_stats.count == 0 and s2[0].sr_stats.count == 2
                assert s2[0].metadata["excluded_eml_candidates"] == 1
                for batch in s1 + s2:
                    assert insert_trial_batch(conn, batch)
                assert register_manual_trials(conn, submission).inserted
                cutoff = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                frozen = capture_trial_snapshot(conn, submission.batch.family_key, cutoff)
                assert frozen.snapshot.n_trials == 7 and frozen.snapshot.sr_stats.count == 2

                def rows():
                    return {table: conn.execute(sql.SQL("SELECT to_jsonb(x) FROM {} x ORDER BY to_jsonb(x)::text")
                            .format(sql.Identifier(table))).fetchall() for table in TABLES}

                before = rows()
                for _ in range(2):
                    for filename in NAMES:
                        conn.execute((tree / "qedschema/migrations" / filename).read_text())
                assert before == rows()
                assert get_snapshot_capture(conn, frozen.capture_id).to_dict() == frozen.to_dict()
                assert get_snapshot_capture(conn, first.capture_id).to_dict() == first.to_dict()
                assert not register_manual_trials(conn, submission).inserted
                assert all(not insert_trial_batch(conn, batch) for batch in s1 + s2)

                # recorded_at is not commit visibility time: only saved capture is frozen.
                delayed = evidence_doc()
                delayed["family"] = dict(doc["family"])
                delayed.update(source="synthetic-delayed", research_run_id="independent-delayed", n_trials=1)
                delayed["trials"] = [{**delayed["trials"][0], "formula": "independent_formula"}]
                with psycopg.connect(**{**kwargs, "dbname": name, "autocommit": False}) as writer:
                    late = parse_manual_evidence(raw(delayed), 1, "synthetic-probe")
                    register_manual_trials(writer, late)
                    late_cutoff = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                    held = capture_trial_snapshot(conn, submission.batch.family_key, late_cutoff)
                    assert held.snapshot.n_trials == 7
                    writer.commit()
                assert fetch_trial_snapshot(conn, submission.batch.family_key, as_of=late_cutoff).n_trials == 8
                assert capture_trial_snapshot(conn, submission.batch.family_key, late_cutoff).to_dict() == held.to_dict()
                assert get_snapshot_capture(conn, held.capture_id).to_dict() == held.to_dict()

                # Actual FROST writer + S2 ledger, not the writer stubs in software
                # equivalence tests. Both runs use independent synthetic IDs.
                pipeline_migrations = ("060_frost_runs.sql", "061_frost_fitness_candidates.sql",
                    "062_frost_evaluations.sql", "063_frost_selection_decisions.sql",
                    "082_qed_policies.sql", "083_frost_runs_policy_hash.sql")
                for filename in pipeline_migrations:
                    conn.execute((tree / "qedschema/migrations" / filename).read_text())
                config = FrostConfig(dry_run=True)
                audit_root.mkdir(mode=0o700)
                signatures, ledger_stats, statuses = [], [], []
                for mode in (0, 1):
                    run_id = str(uuid.uuid4())
                    candidates = make_synthetic_candidates(6)
                    by_id = {}
                    for index, candidate in enumerate(candidates):
                        candidate.candidate_id = str(uuid.UUID(int=1000*mode+index+1))
                        candidate.run_id, candidate.trace_id = run_id, "synthetic-trace"
                        candidate.source_type = "technical" if index < 2 else "eml"
                        candidate.feature_spec_json = {"universe": "SYNTHETIC_MATRIX", "target": "pipeline_probe"}
                        by_id[candidate.candidate_id] = candidate.candidate_hash
                    audit = None
                    if mode:
                        audit = ExplorationAudit.create(audit_root, search_id="synthetic-pipeline-attempt",
                            run_id=run_id, family_key=make_family_key("5d", "SYNTHETIC_MATRIX", "pipeline_probe", ""),
                            policy_hash=policy_spec_from_frost_config(config).policy_hash,
                            code_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tree, text=True).strip(),
                            input_evidence_sha256=evidence_hash([asdict(c) for c in candidates]),
                            settings={"synthetic_only": True}, historical_n_raw=None)
                    output = run_frost_pipeline(candidates, config, conn=conn, run_id=run_id,
                                               trace_id="synthetic-trace", audit=audit)
                    assert not output.error_message and output.lineage["status"] == "recorded"
                    assert output.lineage["n_trials"] == 2 and output.promotion_count == 0
                    normalized = []
                    for evaluation in output.evaluations:
                        values = asdict(evaluation)
                        for key in ("evaluation_id", "run_id", "trace_id", "candidate_id", "created_at", "updated_at"):
                            values.pop(key, None)
                        normalized.append((by_id[evaluation.candidate_id], values))
                    decisions = [(by_id[d.candidate_id], d.decision, d.decision_rank, d.frost_score,
                                  d.promotion_eligible, d.gate_failures) for d in output.decisions]
                    signatures.append((normalized, decisions))
                    statuses.append(output.status)
                    ledger_stats.append(conn.execute("SELECT family_key,stage,n_trials,sr_count,sr_mean,sr_m2 "
                        "FROM qed_trial_batches WHERE run_id=%s ORDER BY family_key,stage", (run_id,)).fetchall())
                    assert len(ledger_stats[-1]) == 1 and ledger_stats[-1][0][2] == 2
                    if audit:
                        assert audit.finish()["ledger_writes"] == 0
                assert signatures[0] == signatures[1] and ledger_stats[0] == ledger_stats[1]
                assert statuses[0] == statuses[1]
                result.update(passed=True, s1_n_raw=3, s2_n_raw=2, s3_n_raw=2,
                    combined_n_raw=7, sr_sufficient_stat_count=2,
                    s2_duplicate_hash_counted_once=True, s2_eml_excluded=True,
                    snapshot_without_086=True, reapplications=2, row_values_unchanged=True,
                    capture_replay_hash_verified=True, exact_retry_idempotent=True,
                    live_asof_changes_after_late_commit=True, saved_capture_remains_fixed=True,
                    frost_audit_on_off_real_writer_and_s2_equal=True,
                    frost_comparison_normalizes_only_ids_and_timestamps=True,
                    frost_pipeline_migrations=list(pipeline_migrations),
                    frost_per_run_s2_n_raw=2, frost_real_promotion_count=0)
        finally:
            if created:
                control.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
                result["owned_disposable_database_dropped"] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--socket-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("OUTPUT_ALREADY_EXISTS")
    result = run(args.tree.resolve(), args.socket_dir, args.output.with_suffix(".events"))
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
