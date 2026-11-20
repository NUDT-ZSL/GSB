from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from graphstore.storage import GraphStore

from conftest import ROOT, cli_path, popen_cli, run_cli


def _json_stdout(process: subprocess.CompletedProcess[str]) -> dict:
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def test_hard_kill_during_traversal_resumes_exactly(
    workspace_tmp: Path, clock, base_package
) -> None:
    baseline_db = workspace_tmp / "baseline.sqlite3"
    crash_db = workspace_tmp / "crash.sqlite3"
    for db in (baseline_db, crash_db):
        store = GraphStore(db, clock)
        store.replace(base_package)

    baseline = _json_stdout(
        run_cli(
            "--db",
            cli_path(baseline_db),
            "--clock",
            "samples/clock.json",
            "traverse",
            "--batch-id",
            "two-hop",
            "--kind",
            "reachable",
            "--node-id",
            "n1",
            "--depth",
            "2",
        )
    )
    crashed = run_cli(
        "--db",
        cli_path(crash_db),
        "--clock",
        "samples/clock.json",
        "traverse",
        "--batch-id",
        "two-hop",
        "--kind",
        "reachable",
        "--node-id",
        "n1",
        "--depth",
        "2",
        check=False,
        env={
            "GRAPHSTORE_ENABLE_TEST_FAILPOINTS": "1",
            "GRAPHSTORE_CRASH_AT": "traverse_after_layer_1",
        },
    )
    assert crashed.returncode != 0

    with sqlite3.connect(crash_db) as connection:
        row = connection.execute(
            "SELECT status, next_depth FROM traverse_batches WHERE batch_id='two-hop'"
        ).fetchone()
    assert row == ("running", 1)

    resumed = _json_stdout(
        run_cli(
            "--db",
            cli_path(crash_db),
            "--clock",
            "samples/clock.json",
            "resume",
            "--batch-id",
            "two-hop",
        )
    )
    assert resumed == baseline
    assert resumed["stats"] == {"emitted": 3, "truncated": 2, "visited": 4}

    repeated = _json_stdout(
        run_cli(
            "--db",
            cli_path(crash_db),
            "--clock",
            "samples/clock.json",
            "traverse",
            "--batch-id",
            "two-hop",
            "--kind",
            "reachable",
            "--node-id",
            "n1",
            "--depth",
            "2",
        )
    )
    assert repeated == baseline
    store = GraphStore(crash_db, clock)
    status = store.status()
    assert status["committed_traversals"]["batch_count"] == 1
    assert store.verify("two-hop")["ok"] is True


def test_hard_kill_during_replace_keeps_old_pointer_and_resumes(
    workspace_tmp: Path, clock, base_package
) -> None:
    db = workspace_tmp / "replace.sqlite3"
    store = GraphStore(db, clock)
    store.replace(base_package)

    crashed = run_cli(
        "--db",
        cli_path(db),
        "--clock",
        "samples/clock.json",
        "replace",
        "--package",
        "samples/packages/append/manifest.json",
        check=False,
        env={
            "GRAPHSTORE_ENABLE_TEST_FAILPOINTS": "1",
            "GRAPHSTORE_CRASH_AT": "replace_after_stage_nodes",
        },
    )
    assert crashed.returncode != 0

    status = store.status()
    assert status["current_index"]["index_version"] == 1
    assert status["current_index"]["node_count"] == 9
    assert status["pending_builds"][0]["stage_phase"] == "nodes"

    resumed = _json_stdout(
        run_cli(
            "--db",
            cli_path(db),
            "--clock",
            "samples/clock.json",
            "resume",
            "--package",
            "samples/packages/append/manifest.json",
        )
    )
    assert resumed["result"]["index_version"] == 2
    assert resumed["result"]["node_count"] == 10
    status = store.status()
    assert status["pending_builds"] == []
    assert status["current_index"]["node_count"] == 10
    assert store.verify()["ok"] is True


def test_batch_digest_mismatch_is_detected_without_poisoning_queries(
    workspace_tmp: Path, clock, base_package
) -> None:
    db = workspace_tmp / "digest.sqlite3"
    store = GraphStore(db, clock)
    store.replace(base_package)
    first = run_cli(
        "--db",
        cli_path(db),
        "--clock",
        "samples/clock.json",
        "traverse",
        "--batch-id",
        "digest-batch",
        "--kind",
        "exact",
        "--node-id",
        "n1",
    )
    assert first.returncode == 0
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE traverse_batches SET result_digest='broken' WHERE batch_id='digest-batch'"
        )
        connection.commit()
    verify = run_cli(
        "--db",
        cli_path(db),
        "--clock",
        "samples/clock.json",
        "verify",
        "--batch-id",
        "digest-batch",
        check=False,
    )
    assert verify.returncode == 2
    assert json.loads(verify.stderr)["error"]["code"] == "GS_BATCH_DIGEST_MISMATCH"

    still_works = run_cli(
        "--db",
        cli_path(db),
        "--clock",
        "samples/clock.json",
        "traverse",
        "--batch-id",
        "healthy-batch",
        "--kind",
        "exact",
        "--node-id",
        "n1",
    )
    assert still_works.returncode == 0


def test_multiple_processes_commit_once(
    workspace_tmp: Path, clock, base_package
) -> None:
    db = workspace_tmp / "concurrent.sqlite3"
    GraphStore(db, clock)
    processes = [
        popen_cli(
            "--db",
            cli_path(db),
            "--clock",
            "samples/clock.json",
            "load",
            "--package",
            "samples/packages/base/manifest.json",
        )
        for _ in range(4)
    ]
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        outputs.append(json.loads(stdout))
    assert sum(
        item["result"]["classification"] == "initial_load" for item in outputs
    ) == 1
    assert sum(item["result"]["classification"] == "no_op" for item in outputs) == 3
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM index_versions").fetchone()[0] == 1
        assert connection.execute("SELECT version FROM current_index").fetchone()[0] == 1

    processes = [
        popen_cli(
            "--db",
            cli_path(db),
            "--clock",
            "samples/clock.json",
            "traverse",
            "--batch-id",
            "concurrent-batch",
            "--kind",
            "reachable",
            "--node-id",
            "n1",
            "--depth",
            "2",
        )
        for _ in range(4)
    ]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert all(result == results[0] for result in results)
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM traverse_watermarks"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM traverse_batches"
        ).fetchone()[0] == 1


def test_replacement_and_distinct_traversals_can_run_concurrently(
    workspace_tmp: Path, clock, base_package
) -> None:
    db = workspace_tmp / "mixed-concurrency.sqlite3"
    store = GraphStore(db, clock)
    store.replace(base_package)
    processes = [
        popen_cli(
            "--db",
            cli_path(db),
            "--clock",
            "samples/clock.json",
            "replace",
            "--package",
            "samples/packages/append/manifest.json",
        )
    ]
    processes.extend(
        popen_cli(
            "--db",
            cli_path(db),
            "--clock",
            "samples/clock.json",
            "traverse",
            "--batch-id",
            f"parallel-{index}",
            "--kind",
            "reachable",
            "--node-id",
            "n1",
            "--depth",
            "2",
        )
        for index in range(4)
    )
    for process in processes:
        _stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr

    status = store.status()
    assert status["current_index"]["index_version"] == 2
    assert status["current_index"]["node_count"] == 10
    assert status["committed_traversals"]["batch_count"] == 4
    assert store.verify()["ok"] is True

