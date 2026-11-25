from __future__ import annotations

import concurrent.futures
import sqlite3
import subprocess

from health_machine.cli import main
from health_machine.store import Store
from health_machine.verify import verify_store
from tests.test_recovery import run_cli_subprocess


def test_same_instance_parallel_probe_commits_once(workspace):
    db = workspace.root / "same-instance.sqlite"
    source = sqlite3.connect(workspace.db)
    target = sqlite3.connect(db)
    source.backup(target)
    target.close()
    source.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [
            pool.submit(run_cli_subprocess, "probe", "--db", str(db), "--instance", "inst-outage")
            for _ in range(6)
        ]
        results = [future.result(timeout=60) for future in futures]
    assert all(result.returncode == 0 for result in results), [r.stderr + r.stdout for r in results if r.returncode]

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM window_summaries WHERE instance_id='inst-outage'").fetchone()[0] == 10
    assert conn.execute("SELECT COUNT(*) FROM transition_log WHERE instance_id='inst-outage'").fetchone()[0] == 4
    assert conn.execute("SELECT failure_streak FROM instance_state WHERE instance_id='inst-outage'").fetchone()[0] == 0
    assert conn.execute("SELECT recovery_streak FROM instance_state WHERE instance_id='inst-outage'").fetchone()[0] == 4
    conn.close()
    assert verify_store(Store(db), "inst-outage")["ok"]


def test_different_instances_parallel_probe_is_consistent(workspace):
    db = workspace.root / "different-instances.sqlite"
    source = sqlite3.connect(workspace.db)
    target = sqlite3.connect(db)
    source.backup(target)
    target.close()
    source.close()

    instance_ids = ["inst-steady", "inst-flap", "inst-outage", "inst-timeout"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(run_cli_subprocess, "probe", "--db", str(db), "--instance", instance_id)
            for instance_id in instance_ids
        ]
        results = [future.result(timeout=60) for future in futures]
    assert all(result.returncode == 0 for result in results), [r.stderr + r.stdout for r in results if r.returncode]
    report = verify_store(Store(db))
    assert report["ok"], report


def test_concurrent_duplicate_load_is_idempotent(workspace):
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                run_cli_subprocess,
                "load", "--samples", str(workspace.samples), "--db", str(workspace.db),
            )
            for _ in range(4)
        ]
        results = [future.result(timeout=60) for future in futures]
    assert all(result.returncode == 0 for result in results), [r.stderr for r in results if r.returncode]
    conn = sqlite3.connect(workspace.db)
    assert conn.execute("SELECT COUNT(*) FROM observation_inbox").fetchone()[0] == 75
    conn.close()


def test_concurrent_merge_replaces_snapshot_version_once(workspace, tmp_path):
    merge_db = tmp_path / "concurrent-merge.sqlite"
    assert main(["load", "--samples", str(workspace.samples), "--db", str(merge_db)]) == 0
    outputs = [str(tmp_path / f"unified-{index}.json") for index in range(4)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                run_cli_subprocess,
                "merge", "--samples", str(workspace.samples), "--db", str(merge_db),
                "--merge-version", "concurrent", "--output", outputs[index],
            )
            for index in range(4)
        ]
        results = [future.result(timeout=60) for future in futures]
    assert all(result.returncode == 0 for result in results), [r.stdout + r.stderr for r in results if r.returncode]
    conn = sqlite3.connect(merge_db)
    assert conn.execute("SELECT COUNT(*) FROM snapshot_versions WHERE snapshot_version='concurrent'").fetchone()[0] == 1
    conn.close()
