from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from .conftest import ROOT, cli_json, run_cli


def _start(*args: str, state_path: Path) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        "-m",
        "shardroute",
        *args,
        "--state",
        str(state_path),
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def test_concurrent_identical_queries_commit_once(tmp_path: Path):
    state_path = tmp_path / "state.db"
    cli_json(
        "load",
        "--sample",
        "samples/sample.json",
        "--clock",
        "1000",
        state_path=state_path,
    )
    processes = [
        _start(
            "query",
            "--template",
            "orders_all",
            "--clock",
            "1100",
            state_path=state_path,
        )
        for _ in range(6)
    ]
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        outputs.append(json.loads(stdout))
    assert all(output == outputs[0] for output in outputs)
    connection = sqlite3.connect(state_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM query_results").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM queries").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM shard_outcomes").fetchone()[0] == 4
    finally:
        connection.close()


def test_queries_and_rule_update_can_run_concurrently(tmp_path: Path):
    state_path = tmp_path / "state.db"
    cli_json(
        "load",
        "--sample",
        "samples/sample.json",
        "--clock",
        "1000",
        state_path=state_path,
    )
    query_processes = [
        _start(
            "query",
            "--template",
            "orders_eq",
            "--param",
            f"shard_key={value}",
            "--clock",
            "1100",
            state_path=state_path,
        )
        for value in range(8)
    ]
    apply_process = _start(
        "apply",
        "--sample",
        "samples/sample.json",
        "--version",
        "r2",
        "--clock",
        "2000",
        state_path=state_path,
    )
    query_results = []
    for process in query_processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        query_results.append(json.loads(stdout))
    apply_stdout, apply_stderr = apply_process.communicate(timeout=60)
    assert apply_process.returncode == 0, apply_stderr
    applied = json.loads(apply_stdout)
    assert applied["activated"] is True
    assert {result["rule_version"] for result in query_results} <= {"r1", "r2"}
    assert all(result["overall_status"] == "SUCCESS" for result in query_results)
    assert len({result["query_id"] for result in query_results}) == 8
    connection = sqlite3.connect(state_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM query_results").fetchone()[0] == 8
    finally:
        connection.close()
    assert cli_json("verify", state_path=state_path)["ok"] is True


def test_status_is_stable_json_under_concurrency(tmp_path: Path):
    state_path = tmp_path / "state.db"
    cli_json(
        "load",
        "--sample",
        "samples/sample.json",
        "--clock",
        "1000",
        state_path=state_path,
    )
    processes = [
        _start("status", state_path=state_path)
        for _ in range(4)
    ]
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        outputs.append(json.loads(stdout))
    assert all(output == outputs[0] for output in outputs)
    assert set(outputs[0]) >= {
        "completed_queries",
        "current_rule_version",
        "mapping_digest",
        "shard_mapping",
    }


def test_concurrent_resume_claims_unfinished_query_once(tmp_path: Path):
    state_path = tmp_path / "state.db"
    cli_json(
        "load",
        "--sample",
        "samples/sample.json",
        "--clock",
        "1000",
        state_path=state_path,
    )
    crashed = run_cli(
        "query",
        "--template",
        "orders_sum_count",
        "--clock",
        "1100",
        "--crash-at",
        "after_execute",
        state_path=state_path,
        check=False,
    )
    assert crashed.returncode == 75
    processes = [
        _start("resume", "--clock", "1200", state_path=state_path)
        for _ in range(2)
    ]
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        outputs.append(json.loads(stdout))
    assert sorted(output["resumed_count"] for output in outputs) == [0, 1]
    connection = sqlite3.connect(state_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM query_results").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM query_staging").fetchone()[0] == 0
    finally:
        connection.close()
    assert cli_json("verify", state_path=state_path)["ok"] is True

