from __future__ import annotations

import json
from pathlib import Path

from conftest import (
    CLOCKS,
    INVALID,
    MONOLITH,
    REGISTRY,
    SHARDS,
    ledger,
    ledger_bytes,
    load_all,
    reconcile,
    run_cli,
    status,
)


def test_full_sample_state_machine_and_missing_hole(tmp_path: Path):
    workspace = tmp_path / "ws"
    loaded = load_all(workspace)
    assert loaded.returncode == 0, loaded.stdout + loaded.stderr
    result = reconcile(workspace)
    assert result.returncode == 0, result.stdout + result.stderr

    snapshot = ledger(workspace)
    assert snapshot["pointer_version"] == 19
    assert snapshot["buffer"]["count"] == 0
    by_id = {item["instance_id"]: item for item in snapshot["instances"]}

    checkout_a = by_id["checkout-a"]
    seq4 = next(
        item for item in checkout_a["history"] if item["obs_seq"] == 4
    )
    assert seq4["basis_kind"] == "missing"
    assert seq4["outcome"] == "missing"
    assert seq4["consecutive_failures"] == 1
    assert seq4["status"] == "suspect"
    assert checkout_a["consecutive_failures"] == 0
    assert checkout_a["status"] == "healthy"

    checkout_b_history = by_id["checkout-b"]["history"]
    lost = [item for item in checkout_b_history if item["status"] == "lost"]
    assert lost
    assert lost[0]["obs_seq"] == 3
    assert lost[0]["consecutive_failures"] == 3
    assert by_id["checkout-b"]["status"] == "healthy"

    inventory_seq2 = [
        item for item in by_id["inventory-a"]["history"] if item["obs_seq"] == 2
    ]
    assert len(inventory_seq2) == 1
    assert inventory_seq2[0]["outcome"] == "success"
    assert any(
        item["time_conversion"]["rounding"] == "floor"
        for item in snapshot["time_conversions"]
    )

    verify = run_cli(
        "verify",
        "--workspace",
        str(workspace),
        "--registry",
        str(REGISTRY),
        "--clocks",
        str(CLOCKS),
        "--shard-dir",
        str(SHARDS[0].parent),
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert json.loads(verify.stdout)["ok"] is True


def test_arbitrary_load_order_and_incremental_reconcile_converge(tmp_path: Path):
    plain = tmp_path / "plain"
    reverse = tmp_path / "reverse"
    incremental = tmp_path / "incremental"
    monolithic = tmp_path / "monolithic"

    assert load_all(plain).returncode == 0
    assert reconcile(plain).returncode == 0
    assert load_all(reverse, reverse=True).returncode == 0
    assert reconcile(reverse).returncode == 0
    assert ledger_bytes(plain) == ledger_bytes(reverse)

    base = [
        "load",
        "--workspace",
        str(incremental),
        "--registry",
        str(REGISTRY),
        "--clocks",
        str(CLOCKS),
    ]
    assert run_cli(*base).returncode == 0
    for shard in reversed(SHARDS):
        loaded = run_cli(*base, "--shard", str(shard))
        assert loaded.returncode == 0, loaded.stdout + loaded.stderr
        result = reconcile(incremental)
        assert result.returncode == 0, result.stdout + result.stderr
    assert ledger_bytes(plain) == ledger_bytes(incremental)

    mono_load = run_cli(
        "load",
        "--workspace",
        str(monolithic),
        "--registry",
        str(REGISTRY),
        "--clocks",
        str(CLOCKS),
        "--shard",
        str(MONOLITH),
    )
    assert mono_load.returncode == 0, mono_load.stdout + mono_load.stderr
    assert reconcile(monolithic).returncode == 0
    plain_snapshot = ledger(plain)
    mono_snapshot = ledger(monolithic)
    assert plain_snapshot["instances"] == mono_snapshot["instances"]
    assert plain_snapshot["instances_digest"] == mono_snapshot["instances_digest"]
    assert plain_snapshot["buffer"] == mono_snapshot["buffer"]
    assert plain_snapshot["time_conversions"] == mono_snapshot["time_conversions"]
    assert plain_snapshot["pointer_version"] == mono_snapshot["pointer_version"]


def test_conflict_and_version_conflict_are_isolated(tmp_path: Path):
    conflict_ws = tmp_path / "conflict"
    loaded = run_cli(
        "load",
        "--workspace",
        str(conflict_ws),
        "--registry",
        str(REGISTRY),
        "--shard",
        str(INVALID / "overlap-conflict-a.json"),
        "--shard",
        str(INVALID / "overlap-conflict-b.json"),
    )
    assert loaded.returncode == 3
    result = reconcile(conflict_ws)
    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert payload["transitions_committed"] == 0
    conflict = next(item for item in payload["diagnostics"] if item["code"] == "E_CONFLICT_OBS")
    assert conflict["digest_a"] and conflict["digest_b"]
    assert conflict["digest_a"] != conflict["digest_b"]
    assert "arrival_a" in conflict and "arrival_b" in conflict
    current = status(conflict_ws)["instances"][0]
    assert current["state_generation"] == 1
    assert current["review_required"] is True

    version_ws = tmp_path / "version"
    loaded = run_cli(
        "load",
        "--workspace",
        str(version_ws),
        "--registry",
        str(REGISTRY),
        "--shard",
        str(INVALID / "version-conflict-a.json"),
        "--shard",
        str(INVALID / "version-conflict-b.json"),
    )
    assert loaded.returncode == 3
    result = reconcile(version_ws)
    assert result.returncode == 3
    payload = json.loads(result.stdout)
    version = next(
        item for item in payload["diagnostics"] if item["code"] == "E_VERSION_CONFLICT"
    )
    assert version["details"]["difference_fields"]
    current = status(version_ws)["instances"][0]
    assert current["state_generation"] == 1
    assert current["version_conflict"] is True
    assert current["review_required"] is True


def test_clock_rollback_maps_to_business_error(tmp_path: Path):
    workspace = tmp_path / "clock"
    loaded = run_cli(
        "load",
        "--workspace",
        str(workspace),
        "--registry",
        str(REGISTRY),
        "--shard",
        str(INVALID / "clock-rollback.json"),
    )
    assert loaded.returncode == 0
    result = reconcile(workspace)
    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert any(item["code"] == "E_CLOCK_ROLLBACK" for item in payload["diagnostics"])


def test_status_is_byte_stable(tmp_path: Path):
    workspace = tmp_path / "ws"
    assert load_all(workspace).returncode == 0
    assert reconcile(workspace).returncode == 0
    first = run_cli("status", "--workspace", str(workspace))
    second = run_cli("status", "--workspace", str(workspace))
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    ids = [item["instance_id"] for item in json.loads(first.stdout)["instances"]]
    assert ids == sorted(ids)


def test_verify_detects_snapshot_tampering_and_resume_repairs(tmp_path: Path):
    workspace = tmp_path / "ws"
    assert load_all(workspace).returncode == 0
    assert reconcile(workspace).returncode == 0
    path = workspace / "ledger.json"
    path.write_bytes(path.read_bytes().replace(b'"pointer_version":19', b'"pointer_version":18'))

    checked = run_cli("verify", "--workspace", str(workspace))
    assert checked.returncode == 4
    payload = json.loads(checked.stdout)
    assert payload["ok"] is False
    assert any(item["code"] == "E_VERIFY_SNAPSHOT" for item in payload["errors"])

    resumed = run_cli("resume", "--workspace", str(workspace))
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    checked = run_cli("verify", "--workspace", str(workspace))
    assert checked.returncode == 0
