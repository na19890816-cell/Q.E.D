"""Synthetic integration/recovery probe, explicitly targeting a combined checkout.

No DB, network, real exporter, adoption decision, or trading I/O. A local copy is
not an off-host backup, and a local checkpoint is not an external trusted anchor.
Run with --tree containing PRs 1..6 and --work-dir a NEW disposable directory.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def inventory(root):
    return {str(path.relative_to(root)): digest(path.read_bytes())
            for path in sorted(root.rglob("*")) if path.is_file()}


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_tree(root):
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    for path in sorted((p for p in root.rglob("*") if p.is_dir()),
                       key=lambda p: len(p.parts), reverse=True):
        sync_directory(path)
    sync_directory(root)
    sync_directory(root.parent)


def write_new(path, value):
    with path.open("xb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def run(tree, work):
    sys.path.insert(0, str(tree))
    import numpy as np
    import pandas as pd
    from analytics.python.data_contracts.local_ingestion import LocalAuditStore
    from analytics.python.data_contracts.prostock2_v1 import coverage_key
    from analytics.python.audit.exploration_events import ExplorationAudit, inspect_attempt, read_events
    from analytics.python.alpha.eml.eml_search import exhaustive_search
    from analytics.python.frost.frost_lineage import make_family_key
    from analytics.python.frost.frost_config import FrostConfig
    from analytics.python.frost.policy_spec import policy_spec_from_frost_config
    from tests.fixtures.prostock2_contract import ohlcv_record

    work.mkdir(mode=0o700)  # no reuse or overwrite of previous evidence
    source = work / "source"
    source.mkdir()
    intake = LocalAuditStore(source / "intake")
    record = ohlcv_record()
    raw = canonical(record) + b"\n"
    manifest = {"transport_schema_version": 1, "source_system": "prostock2",
                "transfer_id": "synthetic-phase6", "record_contract": "prostock2_audit_v1",
                "raw_sha256": digest(raw), "size_bytes": len(raw), "record_count": 1,
                "expected_observations": [list(coverage_key(record))]}
    receipt = intake.receive(io.BytesIO(raw), manifest)
    ack = intake.acknowledge(receipt["receipt_id"])
    assert receipt["state"] == "PROVENANCE_INCOMPLETE"
    assert not ack["research_eligible"] and not ack["producer_deletion_authorized"]
    before_c = inventory(intake.root)

    audit_root = source / "exploration"
    audit_root.mkdir()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tree, text=True).strip()
    run_id = "synthetic-audited-run"
    sink = ExplorationAudit.create(audit_root, search_id="synthetic-attempt", run_id=run_id,
        family_key=make_family_key("5d", "SYNTHETIC", "probe", ""),
        policy_hash=policy_spec_from_frost_config(FrostConfig()).policy_hash,
        code_commit=commit, input_evidence_sha256=digest(raw),
        settings={"synthetic_only": True}, historical_n_raw=None)
    data = pd.DataFrame({"x": np.arange(64, dtype=float)})
    target = pd.Series(np.arange(64, dtype=float))
    plain_stats, audited_stats = [], []
    plain = exhaustive_search(["x"], 2, "synthetic-unaudited-run", "synthetic-trace", data,
                              target, lambda *args: 0.5, top_k=2, stats_out=plain_stats)
    observed = exhaustive_search(["x"], 2, run_id, "synthetic-trace", data, target,
                                 lambda *args: 0.5, top_k=2, stats_out=audited_stats, audit=sink)
    signature = lambda candidates: [(c.compiled_expr, c.fitness_score, c.status) for c in candidates]
    assert signature(plain) == signature(observed) and plain_stats == audited_stats
    complete = sink.finish()
    assert complete["historical_n_raw"] is None and complete["ledger_writes"] == 0
    assert before_c == inventory(intake.root)
    before_d = inventory(audit_root)
    retry = intake.receive(io.BytesIO(raw), manifest)
    assert retry["duplicate_of"] == receipt["receipt_id"] and retry["new_record_count"] == 0
    assert before_d == inventory(audit_root)

    # A pretty single JSON document is NOT reserialized or called JSONL.
    pretty = json.dumps({"synthetic_snapshot": {"x": 1}}, indent=2).encode() + b"\n"
    legacy_manifest = {**manifest, "transfer_id": "synthetic-single-json",
                       "record_contract": "legacy_audit_archive_v1",
                       "raw_sha256": digest(pretty), "size_bytes": len(pretty),
                       "record_count": len(pretty.splitlines()), "expected_observations": None}
    quarantined = intake.receive(io.BytesIO(pretty), legacy_manifest)
    assert quarantined["state"] == "QUARANTINED"
    assert (intake.root / "blobs" / (digest(pretty) + ".raw")).read_bytes() == pretty
    assert before_d == inventory(audit_root) and inspect_attempt(sink.path)["state"] == "COMPLETED"

    # Simulate a quiescent backup and restore to distinct LOCAL directories.
    snapshot = inventory(source)
    backup = work / "backup-local-simulation"
    shutil.copytree(source, backup)
    sync_tree(backup)
    assert inventory(backup) == snapshot
    checkpoint = {"checkpoint_version": 1, "synthetic_only": True,
                  "inventory_sha256": digest(canonical(snapshot)),
                  "intake_ack": ack, "event_count": complete["events"],
                  "last_event_sha256": complete["last_event_sha256"]}
    write_new(work / "checkpoint-local-simulation.json", checkpoint)
    restored = work / "restore-local-simulation"
    shutil.copytree(backup, restored)
    sync_tree(restored)
    assert inventory(restored) == snapshot
    restored_intake = LocalAuditStore(restored / "intake")
    restored_intake.recover()
    assert restored_intake.acknowledge(receipt["receipt_id"])["raw_sha256"] == digest(raw)
    restored_attempt = restored / "exploration" / sink.path.name
    replay = inspect_attempt(restored_attempt)
    assert replay["last_event_sha256"] == checkpoint["last_event_sha256"]
    assert replay["events"] == checkpoint["event_count"]

    # A valid prefix chain alone cannot detect removal of its entire suffix.
    tampered = work / "truncated-tail-local-simulation"
    shutil.copytree(restored_attempt, tampered)
    sorted(tampered.glob("*.json"))[-1].unlink()
    prefix = inspect_attempt(tampered)
    assert prefix["last_event_sha256"] != checkpoint["last_event_sha256"]
    assert prefix["state"] == "INCOMPLETE"
    expected_runs = {"synthetic-audited-run", "synthetic-unaudited-run"}
    present_runs = {event["context"]["run_id"] for event in read_events(sink.path)}
    missing_runs = sorted(expected_runs - present_runs)
    assert missing_runs == ["synthetic-unaudited-run"]

    # Measure bounded local software I/O; this is not device IOPS or production sizing.
    bench = work / "fsync-benchmark"
    bench.mkdir()
    fsync_ms, directory_ms = [], []
    start = time.perf_counter()
    for index in range(64):
        path = bench / f"{index:04d}.bin"
        with path.open("xb") as stream:
            stream.write(b"S" * 4096)
            stream.flush()
            stamp = time.perf_counter()
            os.fsync(stream.fileno())
            fsync_ms.append((time.perf_counter() - stamp) * 1000)
        stamp = time.perf_counter()
        sync_directory(bench)
        directory_ms.append((time.perf_counter() - stamp) * 1000)
    elapsed = time.perf_counter() - start
    space = os.statvfs(work)

    def quantiles(values):
        ordered = sorted(values)
        return {"p05": ordered[int((len(ordered)-1)*0.05)], "p50": statistics.median(ordered),
                "p95": ordered[int((len(ordered)-1)*0.95)]}

    return {"synthetic_only": True, "combined_tree": commit,
            "probe_sha256": digest(Path(__file__).read_bytes()),
            "research_eligible": False, "c_d_independent": True,
            "same_original_retry_idempotent": True, "pretty_single_json_quarantined_unchanged": True,
            "eml_results_and_s1_stats_equal": True, "s1_n_raw": plain_stats[0].n_trials,
            "local_backup_restore_verified": True, "off_host_backup_tested": False,
            "local_checkpoint_detected_suffix_removal": True, "external_anchor_implemented": False,
            "detected_unaudited_synthetic_runs": missing_runs,
            "run_registry_is_independent_fixture": True,
            "fsync_measurement": {"samples": 64, "bytes_per_sample": 4096,
                "file_ms": quantiles(fsync_ms), "directory_ms": quantiles(directory_ms),
                "logical_sync_pairs_per_second": 64/elapsed, "physical_device_iops": None,
                "allocated_file_bytes": sum(p.stat().st_blocks*512 for p in bench.iterdir()),
                "filesystem_available_bytes_at_measurement": space.f_bavail*space.f_frsize,
                "filesystem_available_inodes_at_measurement": space.f_favail}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.tree.resolve(), args.work_dir.resolve())
    write_new(args.work_dir / "result.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
