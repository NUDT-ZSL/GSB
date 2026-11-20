from __future__ import annotations

import json
from pathlib import Path

from conftest import payload, read_manifest, run_sample, task_map, write_manifest


def test_sandbox_denials_are_kernel_enforced(sample_workspace: Path) -> None:
    secret_before = (sample_workspace / "samples/data/secret.txt").read_bytes()
    protected_before = (sample_workspace / "samples/data/protected.txt").read_bytes()

    read_result = run_sample(
        sample_workspace,
        manifest="samples/security_read_manifest.json",
        state="var/read/state.sqlite3",
        cache="var/read/cache",
    )
    assert read_result.returncode == 3
    assert payload(read_result)["errors"][0]["code"] == "E_SANDBOX_READ_DENIED"
    assert (sample_workspace / "samples/data/secret.txt").read_bytes() == secret_before

    write_result = run_sample(
        sample_workspace,
        manifest="samples/security_write_manifest.json",
        state="var/write/state.sqlite3",
        cache="var/write/cache",
    )
    assert write_result.returncode == 4
    assert payload(write_result)["errors"][0]["code"] == "E_SANDBOX_WRITE_DENIED"
    assert (
        sample_workspace / "samples/data/protected.txt"
    ).read_bytes() == protected_before


def test_only_whitelisted_environment_is_injected(sample_workspace: Path) -> None:
    data = read_manifest(sample_workspace)
    leaf = next(task for task in data["tasks"] if task["name"] == "orders_north")
    leaf["env_allowlist"] = ["VISIBLE_VALUE"]
    write_manifest(sample_workspace, data)
    result = run_sample(
        sample_workspace,
        env={"VISIBLE_VALUE": "injected", "HIDDEN_VALUE": "do-not-leak"},
    )
    assert result.returncode == 0
    output = json.loads(
        (sample_workspace / "artifacts/demo/results/orders_north.json").read_text(
            encoding="utf-8"
        )
    )
    assert output["environment"] == {"VISIBLE_VALUE": "injected"}
    assert "HIDDEN_VALUE" not in json.dumps(output)


def test_serial_and_sandbox_results_are_byte_identical(sample_workspace: Path) -> None:
    result = run_sample(sample_workspace)
    assert result.returncode == 0
    tasks = task_map(payload(result))
    assert all(task["outcome"] == "succeeded" for task in tasks.values())
    for name in ("labels_agg", "orders_north", "orders_south", "refunds", "combined"):
        output = sample_workspace / f"artifacts/demo/results/{name}.json"
        raw = output.read_bytes()
        assert raw.endswith(b"\n")
        parsed = json.loads(raw)
        assert parsed["task"] == name
        assert tasks[name]["output_digest"] == __import__("hashlib").sha256(raw).hexdigest()


def test_timeout_is_a_started_failure_not_budget_abandonment(
    sample_workspace: Path,
) -> None:
    data = read_manifest(sample_workspace)
    north = next(task for task in data["tasks"] if task["name"] == "orders_north")
    north["timeout_ms"] = 50
    north["compute_delay_ms"] = 500
    write_manifest(sample_workspace, data)
    result = run_sample(sample_workspace)
    assert result.returncode == 5
    tasks = task_map(payload(result))
    assert tasks["orders_north"]["outcome"] == "failed"
    assert tasks["orders_north"]["error_code"] == "E_TASK_TIMEOUT"
    assert tasks["combined"]["outcome"] == "abandoned"
    assert tasks["combined"]["reason"].startswith("dependency_failed:orders_north")
    assert tasks["labels_agg"]["outcome"] == "succeeded"

