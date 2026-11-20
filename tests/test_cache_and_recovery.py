from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import (
    invoke,
    payload,
    read_manifest,
    run_sample,
    task_map,
    write_manifest,
)


def test_permission_change_invalidates_own_and_downstream_cache_only(
    sample_workspace: Path,
) -> None:
    data = read_manifest(sample_workspace)
    data["latency_budget_ms"] = 10_000
    write_manifest(sample_workspace, data)
    baseline = run_sample(sample_workspace)
    assert baseline.returncode == 0
    baseline_tasks = task_map(payload(baseline))

    data["tasks"][1]["timeout_ms"] = 2001
    write_manifest(sample_workspace, data)
    changed = run_sample(sample_workspace)
    assert changed.returncode == 0
    changed_summary = payload(changed)
    changed_tasks = task_map(changed_summary)

    assert changed_tasks["labels_agg"]["cache_hit"] is True
    assert changed_tasks["orders_south"]["cache_hit"] is True
    assert changed_tasks["refunds"]["cache_hit"] is True
    assert changed_tasks["orders_north"]["cache_hit"] is False
    assert changed_tasks["combined"]["cache_hit"] is False

    triggers = {
        item["task"]: item["trigger_source"]
        for item in changed_summary["cache_invalidations"]
    }
    assert triggers["orders_north"].startswith("permission_declaration:")
    assert triggers["combined"] == "upstream:orders_north"
    for name in ("labels_agg", "orders_south", "refunds"):
        assert (
            changed_tasks[name]["result_digest"]
            == baseline_tasks[name]["result_digest"]
        )

    stable = payload(run_sample(sample_workspace))
    stable_tasks = task_map(stable)
    assert all(task["cache_hit"] is True for task in stable_tasks.values())


def test_input_change_invalidates_downstream_but_not_unrelated_leaves(
    sample_workspace: Path,
) -> None:
    data = read_manifest(sample_workspace)
    data["latency_budget_ms"] = 10_000
    write_manifest(sample_workspace, data)
    assert run_sample(sample_workspace).returncode == 0
    before = task_map(payload(run_sample(sample_workspace)))

    path = sample_workspace / "samples/data/orders_north.json"
    orders = json.loads(path.read_text(encoding="utf-8"))
    orders["records"][0]["value"] = 11
    path.write_text(json.dumps(orders, indent=2) + "\n", encoding="utf-8")

    changed = payload(run_sample(sample_workspace))
    tasks = task_map(changed)
    assert tasks["orders_north"]["cache_hit"] is False
    assert tasks["combined"]["cache_hit"] is False
    assert tasks["orders_south"]["cache_hit"] is True
    assert tasks["refunds"]["cache_hit"] is True
    assert tasks["labels_agg"]["cache_hit"] is True
    triggers = {item["task"]: item["trigger_source"] for item in changed["cache_invalidations"]}
    assert triggers["orders_north"].startswith("input:")
    assert triggers["combined"] == "upstream:orders_north"
    assert (
        tasks["combined"]["output_digest"]
        != before["combined"]["output_digest"]
    )


def test_forced_termination_and_resume_match_uninterrupted_outputs(
    sample_workspace: Path,
) -> None:
    baseline = run_sample(
        sample_workspace,
        state="var/baseline/state.sqlite3",
        cache="var/baseline/cache",
    )
    assert baseline.returncode == 0
    expected = {
        path.name: path.read_bytes()
        for path in (sample_workspace / "artifacts/demo/results").glob("*.json")
    }

    crashed = run_sample(
        sample_workspace,
        state="var/resume/state.sqlite3",
        cache="var/resume/cache",
        extra=("--fault-inject", "prepared:orders_north"),
    )
    assert crashed.returncode == 86
    with sqlite3.connect(sample_workspace / "var/resume/state.sqlite3") as connection:
        phases = dict(
            connection.execute(
                "SELECT task_name, phase FROM task_states"
            ).fetchall()
        )
    assert phases["orders_north"] == "prepared"

    resumed = invoke(
        sample_workspace,
        "resume",
        "--manifest",
        "samples/manifest.json",
        "--state",
        "var/resume/state.sqlite3",
        "--cache",
        "var/resume/cache",
    )
    assert resumed.returncode == 0
    assert payload(resumed)["outcome"] == "success"
    for task in payload(resumed)["tasks"]:
        assert task["phase"] == "committed"

    verified = invoke(
        sample_workspace,
        "verify",
        "--manifest",
        "samples/manifest.json",
        "--state",
        "var/resume/state.sqlite3",
        "--cache",
        "var/resume/cache",
    )
    assert verified.returncode == 0
    assert payload(verified)["valid"] is True
    actual = {
        path.name: path.read_bytes()
        for path in (sample_workspace / "artifacts/demo/results").glob("*.json")
    }
    assert actual == expected


def test_forced_termination_while_running_is_retried(sample_workspace: Path) -> None:
    crashed = run_sample(
        sample_workspace,
        state="var/running/state.sqlite3",
        cache="var/running/cache",
        extra=("--fault-inject", "running:orders_south"),
    )
    assert crashed.returncode == 86
    with sqlite3.connect(sample_workspace / "var/running/state.sqlite3") as connection:
        phase = connection.execute(
            "SELECT phase FROM task_states WHERE task_name='orders_south'"
        ).fetchone()[0]
    assert phase == "running"
    resumed = invoke(
        sample_workspace,
        "resume",
        "--manifest",
        "samples/manifest.json",
        "--state",
        "var/running/state.sqlite3",
        "--cache",
        "var/running/cache",
    )
    assert resumed.returncode == 0
    assert payload(resumed)["outcome"] == "success"

