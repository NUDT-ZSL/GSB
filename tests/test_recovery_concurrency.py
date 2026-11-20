from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from shardpipe.scheduler import run_schedule
from shardpipe.storage import StateStore
from shardpipe.verification import verify_run

from .conftest import ROOT, store_with_document


def _run_cli(
    args: list[str],
    *,
    failpoint: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if failpoint:
        environment["SHARDPIPE_FAILPOINT"] = failpoint
    process = subprocess.run(
        [sys.executable, "-m", "shardpipe", *args],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if check and process.returncode != 0:
        raise AssertionError(
            f"command failed ({process.returncode}): {process.stderr}\n{process.stdout}"
        )
    return process


def test_resume_after_half_committed_blob(tmp_path, sample_document, sample_path):
    state_dir = tmp_path / "state"
    _run_cli(
        ["load", "--input", str(sample_path), "--state-dir", str(state_dir)]
    )
    crashed = _run_cli(
        [
            "schedule",
            "--state-dir",
            str(state_dir),
            "--job",
            "train-alpha",
            "--run-id",
            "crash-run",
        ],
        failpoint="after_blob_write",
        check=False,
    )
    assert crashed.returncode == 92

    store = StateStore(state_dir)
    before = store.get_snapshot("crash-run")
    assert before["state"]["next_round"] == 1
    manifest_count = len(list(store.blobs.manifest_dir.glob("*.json")))
    content_count_before = _scalar(
        store, "SELECT COUNT(*) AS value FROM shard_contents"
    )
    assert manifest_count > content_count_before

    resumed = _run_cli(
        ["resume", "--state-dir", str(state_dir), "--run-id", "crash-run"]
    )
    payload = json.loads(resumed.stdout)
    assert payload["status"] == "complete"
    assert payload["result"]["recovery_actions"]
    assert any(
        item.get("kind") == "half_committed_cache"
        for item in payload["result"]["recovery_actions"]
    )
    assert verify_run(store, run_id="crash-run")["ok"]
    ledger = store.memory_ledger("crash-run")
    assert sum(row["bytes"] for row in ledger) == 6144
    assert len({row["shard_key"] for row in ledger}) == len(ledger)


def test_resume_after_uncommitted_round(tmp_path, sample_path):
    state_dir = tmp_path / "state"
    _run_cli(
        ["load", "--input", str(sample_path), "--state-dir", str(state_dir)]
    )
    crashed = _run_cli(
        [
            "schedule",
            "--state-dir",
            str(state_dir),
            "--job",
            "train-alpha",
            "--run-id",
            "round-crash",
        ],
        failpoint="before_round_commit",
        check=False,
    )
    assert crashed.returncode == 91
    store = StateStore(state_dir)
    assert store.get_snapshot("round-crash")["state"]["next_round"] == 0
    resumed = _run_cli(
        ["resume", "--state-dir", str(state_dir), "--run-id", "round-crash"]
    )
    assert json.loads(resumed.stdout)["status"] == "complete"
    assert verify_run(store, run_id="round-crash")["ok"]


def test_two_processes_commit_shared_content_once(tmp_path, sample_path):
    state_dir = tmp_path / "state"
    _run_cli(
        ["load", "--input", str(sample_path), "--state-dir", str(state_dir)]
    )
    commands = [
        [
            sys.executable,
            "-m",
            "shardpipe",
            "schedule",
            "--state-dir",
            str(state_dir),
            "--job",
            "train-alpha",
            "--run-id",
            run_id,
        ]
        for run_id in ("parallel-a", "parallel-b")
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for command in commands
    ]
    outputs = [process.communicate(timeout=60) for process in processes]
    for process, (stdout, stderr) in zip(processes, outputs):
        assert process.returncode == 0, stderr
        assert json.loads(stdout)["status"] == "complete"

    store = StateStore(state_dir)
    assert verify_run(store, run_id="parallel-a")["ok"]
    assert verify_run(store, run_id="parallel-b")["ok"]
    content_rows = _scalar(
        store, "SELECT COUNT(*) AS value FROM shard_contents"
    )
    batch_keys = _scalar(
        store, "SELECT COUNT(*) AS value FROM batch_results"
    )
    assert content_rows == 4
    assert batch_keys == 12


def test_multiple_datasets_progress_in_parallel(tmp_path, sample_path):
    state_dir = tmp_path / "state"
    _run_cli(
        ["load", "--input", str(sample_path), "--state-dir", str(state_dir)]
    )
    commands = [
        [
            sys.executable,
            "-m",
            "shardpipe",
            "schedule",
            "--state-dir",
            str(state_dir),
            "--job",
            job,
            "--run-id",
            run_id,
        ]
        for job, run_id in (("train-alpha", "ds-a"), ("train-beta", "ds-b"))
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for command in commands
    ]
    outputs = [process.communicate(timeout=60) for process in processes]
    for process, (stdout, stderr) in zip(processes, outputs):
        assert process.returncode == 0, stderr
        assert json.loads(stdout)["status"] == "complete"
    store = StateStore(state_dir)
    verified_a = verify_run(store, run_id="ds-a")
    verified_b = verify_run(store, run_id="ds-b")
    assert verified_a["ok"], json.dumps(verified_a, indent=2, sort_keys=True)
    assert verified_b["ok"], json.dumps(verified_b, indent=2, sort_keys=True)


def _scalar(store: StateStore, query: str) -> int:
    connection = store._connect()
    try:
        return int(connection.execute(query).fetchone()["value"])
    finally:
        connection.close()
