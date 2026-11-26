from __future__ import annotations

from pathlib import Path

import pytest

from cold_diag.errors import PolicyVersionRollbackError
from cold_diag.policy_packaging import write_v2_package
from cold_diag.policy_store import PolicyPackageManager
from cold_diag.sample_data import generate_sample
from cold_diag.store import StateStore


def test_full_replace_incremental_closure_and_rollback(tmp_path: Path) -> None:
    data_root = Path(generate_sample(tmp_path / "data")["root"])
    store = StateStore(tmp_path / "policy.sqlite")
    manager = PolicyPackageManager(store, dependencies={"gateway": ("gamma",)})

    baseline = manager.apply_package(data_root / "policies_v2", clock_ms=100)
    assert baseline["kind"] == "full"
    assert baseline["version"] == 2

    v3_dir = write_v2_package(
        tmp_path / "v3",
        [
            {
                "key": "pool:alpha",
                "function_name": "alpha",
                "generation": 3,
                "effective_at_ms": 40_000,
            },
            {
                "key": "pool:beta",
                "function_name": "beta",
                "generation": 2,
                "effective_at_ms": 40_100,
            },
            {
                "key": "pool:gamma",
                "function_name": "gamma",
                "generation": 2,
                "effective_at_ms": 40_200,
            },
        ],
        version=3,
        generation_floor=2,
        update_mode="full",
    )
    v3 = manager.apply_package(v3_dir.parent, clock_ms=40_000)
    assert v3["kind"] == "full"
    assert v3["pointer"]["version"] == 3
    assert v3["digest_decision"]["incoming_matches_manifest"] is True
    assert v3["digest_decision"]["manifest_digest"] == v3["key_set_digest"]

    v4_dir = write_v2_package(
        tmp_path / "v4",
        [
            {
                "key": "pool:gamma",
                "function_name": "gamma",
                "generation": 3,
                "effective_at_ms": 50_000,
            }
        ],
        version=4,
        generation_floor=2,
        update_mode="incremental",
        base_version=3,
    )
    v4 = manager.apply_package(v4_dir.parent, clock_ms=50_000)
    assert v4["kind"] == "incremental"
    closure = v4["invalidation_closure"]
    assert closure["changed_keys"] == ["pool:gamma"]
    assert "gamma" in closure["affected_functions"]
    assert "gateway" in closure["affected_functions"]

    with pytest.raises(PolicyVersionRollbackError):
        manager.apply_package(data_root / "policies_v2", clock_ms=51_000)

    rows = store.conn.execute(
        "SELECT snapshot_id, version FROM policy_snapshots ORDER BY version"
    ).fetchall()
    assert [row["version"] for row in rows] == [2, 3, 4]
    pointer = store.get_active_policy_snapshot()
    assert pointer["version"] == 4
    store.close()
