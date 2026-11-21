from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from serverless_engine.engine import Engine
from serverless_engine.util import stable_json

from conftest import ROOT, SAMPLES, cli_json, db_rows


def load_args() -> list[str]:
    return [
        "load",
        "--functions",
        "samples/functions.json",
        "--layers",
        "samples/layers.json",
        "--snapshot-v1",
        "samples/snapshots/v1.json",
        "--snapshot-v2-index",
        "samples/snapshots/v2/index.json",
    ]


def test_forced_termination_and_resume_invalidates_old_binding(tmp_path: Path) -> None:
    state = tmp_path / "state"
    cli_json(state, *load_args())
    interrupted_invoke = subprocess.run(
        [
            sys.executable,
            "-m",
            "serverless_engine",
            "invoke",
            "--state-dir",
            str(state),
            "--trace",
            "samples/traces/crash-report.json",
            "--failpoint",
            "init-started",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert interrupted_invoke.returncode != 0
    assert not interrupted_invoke.stdout.strip()

    interrupted_apply = subprocess.run(
        [
            sys.executable,
            "-m",
            "serverless_engine",
            "apply",
            "--state-dir",
            str(state),
            "--patch",
            "samples/patches/runtime-update.json",
            "--failpoint",
            "mid-apply",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert interrupted_apply.returncode != 0
    assert not interrupted_apply.stdout.strip()

    before_resume = cli_json(state, "status")
    assert before_resume["config_version"] == 1
    assert before_resume["call_stats"]["INITIALIZING"] == 1

    resume = Engine(state).resume()
    assert resume["unresolved_calls"] == 0
    assert any(item["action"] == "reapplied_patch" for item in resume["recovered_jobs"])
    assert any(
        call["outcome"] == "COLD_START_SUCCEEDED" for call in resume["drained_calls"]
    )

    after_resume = cli_json(state, "status")
    assert after_resume["config_version"] == 2
    report_old = [
        instance
        for instance in after_resume["instances"]
        if instance["function"] == "report-csv"
        and instance["bound_layer_versions"].get("base-runtime") == 3
    ]
    assert report_old
    assert report_old[0]["stage"] == "INVALIDATED"

    post = cli_json(
        state,
        "invoke",
        "--trace",
        "samples/traces/crash-report-after.json",
    )
    call = post["calls"][0]
    assert call["outcome"] == "COLD_START_SUCCEEDED"
    assert call["cache_hit"] is False
    assert call["config_version"] == 2
    final_status = cli_json(state, "status")
    new_report = next(
        instance
        for instance in final_status["instances"]
        if instance["instance_id"] == call["instance_id"]
    )
    assert new_report["bound_layer_versions"]["base-runtime"] == 4
    assert cli_json(state, "verify")["ok"] is True


def test_multiple_processes_are_idempotent_and_do_not_oversell(tmp_path: Path) -> None:
    state = tmp_path / "state"
    cli_json(state, *load_args())
    command = [
        sys.executable,
        "-m",
        "serverless_engine",
        "invoke",
        "--state-dir",
        str(state),
        "--trace",
        "samples/traces/before.json",
    ]
    first = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out1, err1 = first.communicate(timeout=30)
    out2, err2 = second.communicate(timeout=30)
    assert first.returncode == 0, err1
    assert second.returncode == 0, err2
    result1 = json.loads(out1)
    result2 = json.loads(out2)
    assert stable_json(result1) == stable_json(result2)
    calls = db_rows(
        Engine(state).store.db_path,
        "SELECT COUNT(*) AS n FROM calls",
    )
    assert calls[0]["n"] == 10
    active = db_rows(
        Engine(state).store.db_path,
        "SELECT COALESCE(SUM(occupancy), 0) AS n FROM concurrency_leases WHERE released = 0",
    )
    assert active[0]["n"] == 0
    assert cli_json(state, "verify")["ok"] is True


def test_demo_runs_twice_with_identical_output() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/demo.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    summary = json.loads(result.stdout)
    assert summary["runs_identical"] is True
    assert summary["ok"] is True
    assert all(summary["checks"].values())
