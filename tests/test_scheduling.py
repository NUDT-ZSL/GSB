from __future__ import annotations

import json
from pathlib import Path

from conftest import payload, read_manifest, run_sample, task_map, write_manifest


def test_parallel_budget_marks_unstarted_tasks_without_truncating_started_tasks(
    sample_workspace: Path,
) -> None:
    data = read_manifest(sample_workspace)
    data["latency_budget_ms"] = 70
    write_manifest(sample_workspace, data)
    result = run_sample(sample_workspace)
    assert result.returncode == 0
    summary = payload(result)
    assert summary["outcome"] == "partial"
    tasks = task_map(summary)
    assert tasks["combined"]["outcome"] == "not_started_budget"
    assert tasks["combined"]["phase"] == "not_started"
    assert tasks["combined"]["reason"] == "latency_budget_exhausted"
    assert tasks["orders_south"]["outcome"] == "succeeded"
    assert tasks["orders_south"]["logical_end_ms"] > data["latency_budget_ms"]
    assert tasks["orders_south"]["error_code"] is None

    intervals = [
        (task["logical_start_ms"], task["logical_end_ms"])
        for task in tasks.values()
        if task["logical_start_ms"] is not None
    ]
    maximum = 0
    for start, end in intervals:
        overlap = sum(
            1 for other_start, other_end in intervals if other_start <= start < other_end
        )
        maximum = max(maximum, overlap)
    assert maximum <= data["max_parallel"]
    assert maximum == 2


def test_failure_abandons_dependents_but_independent_tasks_finish(
    sample_workspace: Path,
) -> None:
    (sample_workspace / "samples/data/labels.json").write_text(
        "{not-json", encoding="utf-8"
    )
    result = run_sample(sample_workspace)
    tasks = task_map(payload(result))
    assert tasks["labels_agg"]["outcome"] == "failed"
    assert tasks["labels_agg"]["error_code"] == "E_INPUT_INVALID"
    assert tasks["combined"]["outcome"] == "abandoned"
    assert tasks["combined"]["reason"].startswith("dependency_failed:labels_agg")
    assert tasks["orders_north"]["outcome"] == "succeeded"
    assert tasks["orders_south"]["outcome"] == "succeeded"
    assert tasks["refunds"]["outcome"] == "succeeded"


def test_status_is_stably_sorted_and_reports_logical_latency(
    sample_workspace: Path,
) -> None:
    assert run_sample(sample_workspace).returncode == 0
    from conftest import invoke

    first = invoke(
        sample_workspace,
        "status",
        "--manifest",
        "samples/manifest.json",
        "--state",
        "var/state.sqlite3",
        "--cache",
        "var/cache",
    )
    second = invoke(
        sample_workspace,
        "status",
        "--manifest",
        "samples/manifest.json",
        "--state",
        "var/state.sqlite3",
        "--cache",
        "var/cache",
    )
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    status = json.loads(first.stdout)
    names = [task["task"] for task in status["tasks"]]
    assert names == sorted(names)
    assert all(task["latency_source"] == "injected_logical_clock" for task in status["tasks"])

