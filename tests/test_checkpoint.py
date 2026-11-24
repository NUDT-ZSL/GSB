from __future__ import annotations

import json
from pathlib import Path

import pytest

from smugglebench.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointState,
    CheckpointStore,
    rules_basis_fingerprint,
)
from smugglebench.codes import E_LOCK
from smugglebench.engine import RunConfig, Workbench
from smugglebench.normalize import load_workspace
from smugglebench.util import BusinessError, WorkdirLock


def test_workdir_lock_conflict(tmp_path: Path) -> None:
    first = WorkdirLock(tmp_path).acquire()
    try:
        with pytest.raises(BusinessError) as raised:
            WorkdirLock(tmp_path).acquire()
        assert raised.value.code == E_LOCK
    finally:
        first.release()


def test_checkpoint_atomic_roundtrip_and_residue(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    state = CheckpointState(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        rules_basis="basis",
        completed_connections=["c1"],
        connection_results={"c1": {"ok": True}},
        mutation_results={},
        aggregate={"mutation_count": 0},
        complete=True,
    )
    generation = store.save(state)
    residue_file = tmp_path / "snapshots" / ".half.tmp-123"
    residue_file.write_text("{", encoding="utf-8")
    loaded = store.load(expected_rules_basis="basis")
    assert not loaded.rebuilt
    assert loaded.generation == generation
    assert loaded.state is not None
    assert loaded.state.completed_connections == ["c1"]
    assert any(".half.tmp-123" in path for path in loaded.residue)
    json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))


def test_checkpoint_schema_change_forces_rebuild(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    state = CheckpointState(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        rules_basis="old",
        complete=True,
    )
    store.save(state)
    loaded = store.load(expected_rules_basis="new")
    assert loaded.rebuilt
    assert loaded.reason == "RULE_GENERATION_BASIS_CHANGED"


def test_resume_restores_completed_connection(
    sample_workspace: Path,
    baseline_report: dict,
    tmp_path: Path,
) -> None:
    data = load_workspace(sample_workspace)
    basis = rules_basis_fingerprint(
        list(data.rules),
        observed_clock_ms=data.observed_clock_ms,
    )
    first_mutation = next(
        item for item in baseline_report["mutations"]
        if item["connection_id"] == "conn-normal"
    )
    stored = CheckpointState(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        rules_basis=basis,
        completed_connections=["conn-normal"],
        connection_results={
            "conn-normal": baseline_report["connections"]["conn-normal"]
        },
        mutation_results={first_mutation["mutation_id"]: first_mutation},
        aggregate={},
        complete=True,
    )
    CheckpointStore(tmp_path).save(stored)
    report = Workbench(
        sample_workspace,
        tmp_path,
        RunConfig(concurrency=2, resume=True),
    ).run()
    assert report["checkpoint"]["used"] is True
    assert report["checkpoint"]["reason"] == "RESTORED"
    assert report["checkpoint"]["rebuilt"] is False
    assert len(report["mutations"]) == 10
    assert report["validation"]["completed_connections"] == 9
    assert (tmp_path / "diagnostic_report.json").is_file()
    json.loads((tmp_path / "diagnostic_report.json").read_text(encoding="utf-8"))
