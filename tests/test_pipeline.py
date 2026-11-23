from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .conftest import invoke, invoke_json


def _version_by_seq(state: Path) -> dict[int, int]:
    connection = sqlite3.connect(state)
    try:
        rows = connection.execute(
            "SELECT seq, snapshot_version FROM samples ORDER BY seq"
        ).fetchall()
    finally:
        connection.close()
    return {row[0]: row[1] for row in rows}


def test_sample_aggregation_is_idempotent_and_deterministic(sampled: Path) -> None:
    status = invoke_json(sampled, ["status"])
    assert status["state"]["sample_watermark"] == {
        "last_seq": 12,
        "last_clock_ms": 2700,
        "window_ms": 1000,
    }
    assert [window["sample_count"] for window in status["windows"]] == [5, 2, 5]
    customer_template = next(
        aggregate
        for aggregate in status["aggregates"]
        if aggregate["sample_count"] == 3
        and aggregate["min_seq"] == 4
    )
    assert customer_template["p50"] == 130.0
    assert customer_template["p95"] == 139.0
    second = invoke_json(sampled, ["sample"])
    assert second["inserted"] == 0
    assert second["duplicates"] == 12
    assert second["committed_samples"] == 12


def test_parallel_profiles_equal_serial_expectations(planned: Path) -> None:
    verification = invoke_json(planned, ["verify"])
    assert verification["ok"] is True
    checks = {check["name"]: check["ok"] for check in verification["checks"]}
    assert checks["parallel_profiles_match_serial_estimates"] is True
    status = invoke_json(planned, ["status"])
    assert len(status["profiles"]) == 4
    assert all(
        profile["result"]["candidate_paths"]
        and profile["result"]["snapshot_version"] == 1
        for profile in status["profiles"]
    )
    first_render = invoke(planned, ["status"]).stdout
    second_render = invoke(planned, ["status"]).stdout
    assert first_render == second_render


def test_new_sample_cannot_mutate_a_committed_profile_group(planned: Path) -> None:
    late_stream = planned.parent / "late.json"
    late_stream.write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "seq": 13,
                        "logical_clock_ms": 2800,
                        "sql": (
                            "SELECT id, amount FROM orders "
                            "WHERE customer_id = 42 AND status IN ('new','paid')"
                        ),
                        "duration_ms": 1200,
                        "scanned_rows": 9,
                        "returned_rows": 2,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = invoke(
        planned,
        ["sample", "--samples", str(late_stream)],
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"]["code"] == "ERR_WINDOW_SEALED"
    assert invoke_json(planned, ["status"])["state"]["sample_watermark"]["last_seq"] == 12


def test_duplicate_runs_have_bit_identical_profiles(tmp_path: Path) -> None:
    snapshots = []
    for name, memory_unit in (("a", "8"), ("b", "16")):
        state = tmp_path / name / "state.db"
        invoke_json(state, ["load", "--reset"])
        invoke_json(state, ["sample"])
        invoke_json(
            state,
            [
                "plan",
                "--max-workers",
                "3",
                "--memory-budget",
                "1024",
                "--memory-unit",
                memory_unit,
            ],
        )
        snapshots.append(invoke_json(state, ["status"]))
    assert snapshots[0]["windows"] == snapshots[1]["windows"]
    assert snapshots[0]["aggregates"] == snapshots[1]["aggregates"]
    assert [
        (profile["template_id"], profile["result"])
        for profile in snapshots[0]["profiles"]
    ] == [
        (profile["template_id"], profile["result"])
        for profile in snapshots[1]["profiles"]
    ]


def test_budget_abandonment_and_explicit_retry(sampled: Path) -> None:
    outcome = invoke_json(
        sampled,
        [
            "plan",
            "--max-workers",
            "2",
            "--memory-budget",
            "40",
            "--memory-unit",
            "8",
        ],
    )
    assert outcome["status"] == "completed_with_abandonment"
    assert [task["status"] for task in outcome["tasks"]] == [
        "committed",
        "committed",
        "abandoned",
        "abandoned",
    ]
    for task in outcome["tasks"][2:]:
        assert task["abandon_reason"]["code"] == "ERR_MEMORY_BUDGET"
        assert task["charged"] is False
    retried = invoke_json(
        sampled,
        [
            "resume",
            "--skip-sample",
            "--retry-abandoned",
            "--memory-budget",
            "160",
        ],
    )["planning"]
    assert retried["status"] == "completed"
    assert all(task["status"] == "committed" for task in retried["tasks"])
    assert invoke_json(sampled, ["verify"])["ok"] is True


def test_resume_after_interrupted_task_does_not_double_charge(sampled: Path) -> None:
    interrupted = invoke(
        sampled,
        [
            "plan",
            "--max-workers",
            "2",
            "--memory-budget",
            "1024",
            "--memory-unit",
            "8",
        ],
        check=False,
        env={"SLOWQUERY_FAILPOINT": "after_claim:1"},
    )
    assert interrupted.returncode == 86
    status = invoke_json(sampled, ["status"])
    latest = status["runs"][-1]
    assert latest["status"] == "running"
    assert latest["memory_charged_units"] == 16
    assert [task["status"] for task in latest["tasks"]] == [
        "running",
        "pending",
        "pending",
        "pending",
    ]
    resumed = invoke_json(
        sampled, ["resume", "--skip-sample"]
    )["planning"]
    assert resumed["status"] == "completed"
    assert resumed["memory"]["charged_units"] == 72
    assert all(task["charged"] for task in resumed["tasks"])
    assert invoke_json(sampled, ["verify"])["ok"] is True


def test_crash_before_window_commit_rolls_back(initialized: Path) -> None:
    result = invoke(
        initialized,
        ["sample"],
        check=False,
        env={"SLOWQUERY_FAILPOINT": "before_window_commit:1"},
    )
    assert result.returncode == 86
    connection = sqlite3.connect(initialized)
    try:
        assert connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT last_seq FROM sample_watermark WHERE singleton=1"
            ).fetchone()[0]
            is None
        )
    finally:
        connection.close()
    recovered = invoke_json(initialized, ["sample"])
    assert recovered["inserted"] == 12
    assert recovered["committed_samples"] == 12


def test_crash_after_first_window_commit_then_resume(initialized: Path) -> None:
    result = invoke(
        initialized,
        ["sample"],
        check=False,
        env={"SLOWQUERY_FAILPOINT": "after_window:1"},
    )
    assert result.returncode == 86
    connection = sqlite3.connect(initialized)
    try:
        assert connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 5
        assert (
            connection.execute(
                "SELECT last_seq FROM sample_watermark WHERE singleton=1"
            ).fetchone()[0]
            == 5
        )
    finally:
        connection.close()
    resumed_sample = invoke_json(initialized, ["sample"])
    assert resumed_sample["inserted"] == 7
    assert resumed_sample["committed_samples"] == 12
    invoke_json(initialized, ["plan"])
    assert invoke_json(initialized, ["verify"])["ok"] is True


def test_samples_are_bound_by_effective_clock_and_arrival_version(sampled: Path) -> None:
    invoke_json(sampled, ["snapshot"])
    invoke_json(
        sampled,
        [
            "plan",
            "--max-workers",
            "2",
            "--memory-budget",
            "1024",
            "--memory-unit",
            "8",
        ],
    )
    invoke_json(
        sampled,
        ["sample", "--samples", "samples/slow_queries_after_patch.json"],
    )
    invoke_json(
        sampled,
        [
            "plan",
            "--max-workers",
            "2",
            "--memory-budget",
            "1024",
            "--memory-unit",
            "8",
        ],
    )
    versions = _version_by_seq(sampled)
    assert versions[8] == 1
    assert versions[20] == 2
    assert invoke_json(sampled, ["verify"])["ok"] is True
