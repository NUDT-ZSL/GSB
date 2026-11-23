from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .conftest import CLI, ROOT, clean_env, invoke, invoke_json


def _run_parallel(state: Path, args_list: list[list[str]]) -> list[subprocess.CompletedProcess[str]]:
    processes = [
        subprocess.Popen(
            [sys.executable, str(CLI), "--state", str(state), *args],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=clean_env(),
        )
        for args in args_list
    ]
    return [process.communicate() for process in processes]


def test_incremental_patch_is_idempotent_and_rejects_rollback(planned: Path) -> None:
    before = invoke_json(planned, ["status"])
    customer_profile = next(
        profile
        for profile in before["profiles"]
        if profile["result"]["candidate_paths"][0]["table"] == "customers"
    )
    applied = invoke_json(planned, ["snapshot"])
    assert applied["classification"] == "incremental"
    assert applied["version"] == 2
    assert len(applied["invalidation"]["affected_templates"]) == 3
    assert len(applied["invalidation"]["reusable_profiles"]) == 1
    after = invoke_json(planned, ["status"])
    unchanged = next(
        profile
        for profile in after["profiles"]
        if profile["template_id"] == customer_profile["template_id"]
    )
    assert unchanged == customer_profile
    assert len(after["statistics"]["snapshots"]) == 2

    duplicate = invoke_json(planned, ["snapshot"])
    assert duplicate["classification"] == "no_op"
    assert duplicate["changed"] is False
    assert len(invoke_json(planned, ["status"])["statistics"]["snapshots"]) == 2

    rollback_patch = planned.parent / "rollback.json"
    rollback_patch.write_text(
        json.dumps(
            {
                "version": 1,
                "effective_at_ms": 0,
                "mode": "merge",
                "covered_tables": ["orders"],
                "tables": {"orders": {"row_count": 1}},
            }
        ),
        encoding="utf-8",
    )
    rollback = invoke(
        planned, ["snapshot", "--patch", str(rollback_patch)], check=False
    )
    assert rollback.returncode == 1
    assert json.loads(rollback.stderr)["error"]["code"] == "ERR_SNAPSHOT_ROLLBACK"

    conflict_patch = planned.parent / "conflict_snapshot.json"
    conflict_patch.write_text(
        json.dumps(
            {
                "version": 2,
                "effective_at_ms": 2000,
                "mode": "merge",
                "covered_tables": ["orders"],
                "tables": {"orders": {"row_count": 999}},
            }
        ),
        encoding="utf-8",
    )
    conflict = invoke(
        planned, ["snapshot", "--patch", str(conflict_patch)], check=False
    )
    assert conflict.returncode == 1
    assert json.loads(conflict.stderr)["error"]["code"] == "ERR_SNAPSHOT_CONFLICT"
    assert invoke_json(planned, ["verify"])["ok"] is True


def test_patch_before_first_plan_uses_aggregate_dependencies(sampled: Path) -> None:
    result = invoke_json(sampled, ["snapshot"])
    assert result["classification"] == "incremental"
    assert len(result["invalidation"]["affected_templates"]) == 3
    assert len(result["invalidation"]["aggregates_to_recompute"]) == 3
    assert result["invalidation"]["profiles_to_recompute"] == []
    assert invoke_json(sampled, ["verify"])["ok"] is True


def test_complete_replacement_updates_pointer_atomically(planned: Path) -> None:
    base = json.loads((ROOT / "samples" / "stats_snapshot.json").read_text())
    replacement = {
        "version": 3,
        "effective_at_ms": 3000,
        "mode": "replace",
        "covered_tables": ["customers", "orders"],
        "tables": base["tables"],
    }
    replacement_path = planned.parent / "replacement.json"
    replacement_path.write_text(json.dumps(replacement), encoding="utf-8")
    result = invoke_json(
        planned, ["snapshot", "--patch", str(replacement_path)]
    )
    assert result["classification"] == "complete"
    assert result["version"] == 3
    status = invoke_json(planned, ["status"])
    assert status["statistics"]["current_version"] == 3
    assert [item["version"] for item in status["statistics"]["snapshots"]] == [1, 3]
    assert invoke_json(planned, ["verify"])["ok"] is True


def test_concurrent_sample_snapshot_and_plan_are_idempotent(initialized: Path) -> None:
    outputs = _run_parallel(
        initialized, [["sample"], ["sample"]]
    )
    for stdout, stderr in outputs:
        assert stderr == ""
        payload = json.loads(stdout)
        assert payload["ok"] is True
    assert sum(json.loads(stdout)["inserted"] for stdout, _ in outputs) == 12
    assert invoke_json(initialized, ["status"])["state"]["sample_watermark"]["last_seq"] == 12

    outputs = _run_parallel(
        initialized, [["snapshot"], ["snapshot"]]
    )
    for stdout, stderr in outputs:
        assert stderr == ""
        assert json.loads(stdout)["ok"] is True
    assert len(invoke_json(initialized, ["status"])["statistics"]["snapshots"]) == 2

    outputs = _run_parallel(
        initialized,
        [
            ["plan", "--max-workers", "2", "--memory-budget", "1024", "--memory-unit", "8"],
            ["plan", "--max-workers", "2", "--memory-budget", "1024", "--memory-unit", "8"],
        ],
    )
    for stdout, stderr in outputs:
        assert stderr == ""
        assert json.loads(stdout)["ok"] is True
    status = invoke_json(initialized, ["status"])
    assert len(status["profiles"]) == len(
        {(profile["template_id"], profile["snapshot_version"]) for profile in status["profiles"]}
    )
    assert invoke_json(initialized, ["verify"])["ok"] is True
