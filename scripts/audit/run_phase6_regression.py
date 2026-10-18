"""Replay pinned PR integration in new disposable worktrees; never push/retarget.

Requires the pinned commits to exist in --repo and --python to have the project
test dependencies. All DB settings are removed. Logs stay under a NEW --work-dir.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
import xml.etree.ElementTree as ET


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def junit(path, code):
    cases = list(ET.parse(path).getroot().iter("testcase"))
    ids = [case.attrib["classname"] + "::" + case.attrib["name"] for case in cases]
    failed = sorted(identifier for identifier, case in zip(ids, cases)
                    if case.find("failure") is not None or case.find("error") is not None)
    skipped = sorted(identifier for identifier, case in zip(ids, cases) if case.find("skipped") is not None)
    if len(ids) != len(set(ids)):
        raise RuntimeError("DUPLICATE_TEST_IDS")
    return {"passed": len(ids)-len(failed)-len(skipped), "failed": len(failed), "skipped": len(skipped),
            "exit_code": code, "failure_ids": failed, "duplicate_test_ids": [],
            "collected_ids_sha256": hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest(),
            "junit_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}, set(ids)


def run_tests(tree, interpreter, output, label):
    environment = os.environ.copy()
    environment.pop("QED_PG_DSN", None)
    environment.pop("QED_MIGRATION_TEST_DSN", None)
    environment["PYTHONPATH"] = str(tree)
    xml = output / (label + ".xml")
    with (output / (label + ".log")).open("x") as log:
        completed = subprocess.run([str(interpreter), "-m", "pytest", "-q", "--junitxml=" + str(xml)],
                                   cwd=tree, env=environment, stdout=log, stderr=subprocess.STDOUT)
    return junit(xml, completed.returncode)


def run(repo, interpreter, manifest_path, work):
    manifest = json.loads(manifest_path.read_text())
    main = manifest["main"]
    prs = {item["number"]: item for item in manifest["prs"]}
    if not re.fullmatch(r"[0-9a-f]{40}", main) or set(prs) != set(range(1, 7)):
        raise ValueError("INVALID_PINNED_MANIFEST")
    if manifest["requested_order"] != [2, 1, 3, 4, 5, 6]:
        raise ValueError("UNEXPECTED_INTEGRATION_ORDER")
    for item in prs.values():
        if not re.fullmatch(r"[0-9a-f]{40}", item["head"]):
            raise ValueError("INVALID_PINNED_COMMIT")
        git(repo, "cat-file", "-e", item["head"] + "^{commit}")
    local_main = subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet", "refs/heads/main"])
    main_ref = "refs/heads/main" if local_main.returncode == 0 else "refs/remotes/origin/main"
    main_before = git(repo, "rev-parse", main_ref)
    work.mkdir(mode=0o700)  # fail rather than overwrite previous runs
    baseline, combined = work / "baseline", work / "combined"
    branch = "audit/phase6-replay-" + uuid.uuid4().hex
    git(repo, "worktree", "add", "--detach", str(baseline), main)
    git(repo, "worktree", "add", "-b", branch, str(combined), main)
    result = {"pinned_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
              "synthetic_test_environment": True, "postgres_exercised": False,
              "local_disposable_branch": branch, "merge_steps": []}
    baseline_result, baseline_ids = run_tests(baseline, interpreter, work, "main-no-db")
    result["main_no_db"] = baseline_result
    for number in manifest["requested_order"]:
        with (work / f"merge-pr{number}.log").open("x") as log:
            completed = subprocess.run(["git", "-C", str(combined), "-c", "commit.gpgsign=false",
                "merge", "--no-ff", "--no-edit", prs[number]["head"]], stdout=log, stderr=subprocess.STDOUT)
        if completed.returncode:
            raise RuntimeError("LOCAL_MERGE_CONFLICT_INSPECT_DISPOSABLE_WORKTREE")
        result["merge_steps"].append({"pr": number, "head": prs[number]["head"],
                                     "conflicts": [], "tree": git(combined, "rev-parse", "HEAD^{tree}")})
        if number in (5, 6):
            stage = "pr1_5_no_db" if number == 5 else "pr1_6_no_db"
            stage_result, stage_ids = run_tests(combined, interpreter, work, stage.replace("_", "-"))
            result[stage] = stage_result
            result[stage]["removed_baseline_test_ids"] = sorted(baseline_ids-stage_ids)
            if stage_result["failed"] or stage_result["exit_code"] or baseline_ids-stage_ids:
                raise RuntimeError("INTEGRATED_REGRESSION_FAILED")
    if git(repo, "rev-parse", main_ref) != main_before:
        raise RuntimeError("MAIN_REF_CHANGED")
    result["main_ref_unchanged"] = True
    result["main_ref_observed"] = main_ref
    result["original_pr_refs_written"] = False
    with (work / "result.json").open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.repo.resolve(), args.python.absolute(), args.manifest.resolve(),
                         args.work_dir.resolve()), indent=2))


if __name__ == "__main__":
    main()
