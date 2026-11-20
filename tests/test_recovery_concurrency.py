from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def load_command(db: Path) -> list[str]:
    return [
        "load",
        "--db",
        str(db),
        "--topology",
        str(SAMPLES / "topology.json"),
        "--policies",
        str(SAMPLES / "policies.json"),
        "--events",
        str(SAMPLES / "events.json"),
        "--window-ms",
        "900",
        "--quota-per-second",
        "2",
        "--clock-tolerance-ms",
        "10",
        "--reset",
    ]


def trace_views(
    db: Path,
    run_cli: Callable[..., Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for trace_id in (
        "trace-checkout-001",
        "trace-slow-003",
        "trace-policy-006",
        "trace-fast-000",
    ):
        completed = run_cli(
            ["query", "--db", str(db), "--trace-id", trace_id]
        )
        assert completed.returncode == 0, completed.stderr
        result[trace_id] = json.loads(completed.stdout)
    return result


def test_forced_exit_after_quota_deduction_recovers_without_double_charge(
    tmp_path: Path,
    run_cli: Callable[..., Any],
) -> None:
    baseline = tmp_path / "baseline.db"
    crashed = tmp_path / "crashed.db"
    assert run_cli(load_command(baseline)).returncode == 0
    assert run_cli(load_command(crashed)).returncode == 0
    done = run_cli(["sample", "--db", str(baseline), "--advance-to", "4000"])
    assert done.returncode == 0, done.stderr
    failure = run_cli(
        ["sample", "--db", str(crashed), "--advance-to", "4000"],
        env={"TAILSAMPLE_FAULT_POINT": "after_quota"},
    )
    assert failure.returncode == 97
    before_resume = json.loads(
        run_cli(["status", "--db", str(crashed)]).stdout
    )
    assert before_resume["decisions"]["total"] == 0
    assert before_resume["quota"]["used"] == 0
    resumed = run_cli(["resume", "--db", str(crashed)])
    assert resumed.returncode == 0, resumed.stderr
    resume_payload = json.loads(resumed.stdout)
    assert resume_payload["events_replayed"] > 0
    assert resume_payload["decisions_replayed"] == 7
    assert resume_payload["recovery_runs"] == 1
    assert trace_views(baseline, run_cli) == trace_views(crashed, run_cli)
    baseline_verify = json.loads(
        run_cli(["verify", "--db", str(baseline)]).stdout
    )
    recovered_verify = json.loads(
        run_cli(["verify", "--db", str(crashed)]).stdout
    )
    assert baseline_verify["ok"] is True
    assert recovered_verify["ok"] is True
    assert recovered_verify["summary"]["decisions"] == 7
    status = json.loads(run_cli(["status", "--db", str(crashed)]).stdout)
    assert status["recovery"]["runs"] == 1
    assert status["decisions"]["kept"] == 5
    assert status["quota"]["used"] == 0


@pytest.mark.parametrize(
    ("fault_point", "decisions_before_fault", "decisions_after_resume"),
    [
        ("after_decision_insert", 0, 7),
        ("during_window_close", 5, 2),
    ],
)
def test_other_forced_exit_points_roll_back_to_a_consistent_state(
    tmp_path: Path,
    run_cli: Callable[..., Any],
    fault_point: str,
    decisions_before_fault: int,
    decisions_after_resume: int,
) -> None:
    baseline = tmp_path / f"baseline-{fault_point}.db"
    crashed = tmp_path / f"crashed-{fault_point}.db"
    assert run_cli(load_command(baseline)).returncode == 0
    assert run_cli(load_command(crashed)).returncode == 0
    assert (
        run_cli(["sample", "--db", str(baseline), "--advance-to", "4000"]).returncode
        == 0
    )
    failure = run_cli(
        ["sample", "--db", str(crashed), "--advance-to", "4000"],
        env={"TAILSAMPLE_FAULT_POINT": fault_point},
    )
    assert failure.returncode == 97
    before = json.loads(run_cli(["status", "--db", str(crashed)]).stdout)
    assert before["decisions"]["total"] == decisions_before_fault
    resumed = run_cli(["resume", "--db", str(crashed)])
    assert resumed.returncode == 0, resumed.stderr
    resume_payload = json.loads(resumed.stdout)
    assert resume_payload["decisions_replayed"] == decisions_after_resume
    assert resume_payload["committed_skipped"] == decisions_before_fault
    assert trace_views(baseline, run_cli) == trace_views(crashed, run_cli)
    assert json.loads(run_cli(["verify", "--db", str(crashed)]).stdout)["ok"] is True


def test_concurrent_sampling_is_serialized_and_idempotent(
    loaded_db: Path,
    run_cli: Callable[..., Any],
) -> None:
    command = [
        sys.executable,
        "-m",
        "tailsample",
        "sample",
        "--db",
        str(loaded_db),
        "--advance-to",
        "3500",
    ]
    environment = {**os.environ, "PYTHONHASHSEED": "0"}
    first = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    second = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    first_output, first_error = first.communicate(timeout=30)
    second_output, second_error = second.communicate(timeout=30)
    assert first.returncode == 0, first_error
    assert second.returncode == 0, second_error
    completed_events = json.loads(first_output)["processed_events"] + json.loads(
        second_output
    )["processed_events"]
    assert completed_events == 15
    status = json.loads(run_cli(["status", "--db", str(loaded_db)]).stdout)
    assert status["decisions"] == {"total": 7, "kept": 5, "dropped": 2}
    assert status["quota"]["used"] == 0
    verify = json.loads(run_cli(["verify", "--db", str(loaded_db)]).stdout)
    assert verify["ok"] is True

