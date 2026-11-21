from __future__ import annotations

import json
import shutil
from pathlib import Path

from linear_path_engine.config import load_config
from linear_path_engine.engine import TrainingEngine
from linear_path_engine.snapshot import SnapshotStore

from .helpers import run_cli, write_config, write_dataset


def _snapshot_versions(config_path: Path) -> list[int]:
    config = load_config(config_path)
    files = sorted(config.snapshots.directory.glob("v*-*.json"))
    versions = []
    for path in files:
        versions.append(int(path.name[1:9]))
    return versions


def _snapshot_stages(config_path: Path) -> list[str]:
    config = load_config(config_path)
    stages = []
    for path in sorted(config.snapshots.directory.glob("v*-*.json")):
        stages.append(json.loads(path.read_text(encoding="utf-8"))["stage"])
    return stages


def test_complete_run_is_byte_reproducible_and_snapshots_are_monotonic(
    tmp_path: Path,
) -> None:
    data_path = write_dataset(tmp_path, n_samples=75, seed=91)
    config_path = write_config(
        tmp_path,
        data_path=data_path,
        run_id="e2e-repro",
        workers=1,
        strengths=(0.01, 0.05, 0.2),
    )
    config = load_config(config_path)
    first = TrainingEngine(config_path).run()
    result_path = config.output_directory / "result.json"
    first_bytes = result_path.read_bytes()
    assert _snapshot_versions(config_path) == list(range(1, 7))
    assert _snapshot_stages(config_path) == [
        "standardization",
        "path_solve",
        "path_solve",
        "path_solve",
        "path_validation",
        "path_summary",
    ]
    current = SnapshotStore(config.snapshots.directory, config.run_id).load_current()
    assert current is not None
    assert current.stage == "path_summary"
    assert current.version == 6
    assert first["summary"]["completed_strengths"] == 3
    assert all(entry["converged"] for entry in first["path"])

    shutil.move(str(config.snapshots.directory), str(tmp_path / "snap-backup-repro"))
    second = TrainingEngine(config_path).run()
    second_bytes = result_path.read_bytes()
    assert first == second
    assert first_bytes == second_bytes


def test_hard_exit_after_committed_strength_resumes_without_recompute(
    tmp_path: Path,
) -> None:
    data_path = write_dataset(tmp_path, n_samples=80, seed=92)
    config_path = write_config(
        tmp_path,
        data_path=data_path,
        run_id="e2e-crash-after",
        workers=1,
        strengths=(0.01, 0.05, 0.2),
    )
    config = load_config(config_path)
    crashed = run_cli(
        "train",
        "--config",
        str(config_path),
        "--crash-after-strengths",
        "2",
    )
    assert crashed.returncode == 97
    assert not (config.output_directory / "result.json").exists()
    store = SnapshotStore(config.snapshots.directory, config.run_id)
    before = store.load_current()
    assert before is not None
    assert before.version == 3
    assert before.stage == "path_solve"
    assert before.payload["phase_complete"] is False
    assert len(before.payload["path"]) == 2
    committed_prefix = before.payload["path"]

    resumed = run_cli("train", "--config", str(config_path))
    assert resumed.returncode == 0, resumed.stderr
    result_path = config.output_directory / "result.json"
    resumed_bytes = result_path.read_bytes()
    resumed_doc = json.loads(resumed_bytes)
    assert resumed_doc["path"][:2] == committed_prefix

    shutil.move(str(config.snapshots.directory), str(tmp_path / "snap-backup-after"))
    fresh = run_cli("train", "--config", str(config_path))
    assert fresh.returncode == 0, fresh.stderr
    assert result_path.read_bytes() == resumed_bytes


def test_hard_exit_during_uncommitted_strength_discards_partial_iterations(
    tmp_path: Path,
) -> None:
    data_path = write_dataset(tmp_path, n_samples=80, seed=93)
    config_path = write_config(
        tmp_path,
        data_path=data_path,
        run_id="e2e-crash-mid",
        workers=1,
        strengths=(0.01, 0.05, 0.2),
    )
    config = load_config(config_path)
    crashed = run_cli(
        "train",
        "--config",
        str(config_path),
        "--crash-during-strength",
        "2",
        "--crash-after-iterations",
        "3",
    )
    assert crashed.returncode == 97
    store = SnapshotStore(config.snapshots.directory, config.run_id)
    before = store.load_current()
    assert before is not None
    assert before.version == 1
    assert before.stage == "standardization"
    assert before.payload["path"] == []

    resumed = run_cli("train", "--config", str(config_path))
    assert resumed.returncode == 0, resumed.stderr
    result_path = config.output_directory / "result.json"
    resumed_bytes = result_path.read_bytes()
    shutil.move(str(config.snapshots.directory), str(tmp_path / "snap-backup-mid"))
    fresh = run_cli("train", "--config", str(config_path))
    assert fresh.returncode == 0, fresh.stderr
    assert result_path.read_bytes() == resumed_bytes


def test_degraded_plan_is_reused_after_resume(tmp_path: Path) -> None:
    data_path = write_dataset(tmp_path, n_samples=90, seed=94)
    config_path = write_config(
        tmp_path,
        data_path=data_path,
        run_id="e2e-degrade-resume",
        workers=4,
        block_size=16,
        min_block_size=4,
        memory_budget_bytes=13_000,
        strengths=(0.01, 0.05, 0.2),
    )
    config = load_config(config_path)
    crashed = run_cli(
        "train",
        "--config",
        str(config_path),
        "--crash-after-strengths",
        "1",
    )
    assert crashed.returncode == 97
    store = SnapshotStore(config.snapshots.directory, config.run_id)
    before = store.load_current()
    assert before is not None
    assert before.payload["chunk_plan"]["degradation_categories"] == [
        "workers_reduced",
        "small_block",
    ]
    committed_plan_checksum = before.payload["chunk_plan"]["decision_checksum"]
    committed_entry = before.payload["path"][0]

    resumed = run_cli("train", "--config", str(config_path))
    assert resumed.returncode == 0, resumed.stderr
    result = json.loads((config.output_directory / "result.json").read_text())
    assert result["chunk_plan"]["decision_checksum"] == committed_plan_checksum
    assert result["path"][0] == committed_entry
    assert result["summary"]["degradation_categories"] == [
        "workers_reduced",
        "small_block",
    ]


def test_cli_status_validate_and_rollback_codes(tmp_path: Path) -> None:
    data_path = write_dataset(tmp_path, n_samples=45, seed=95)
    config_path = write_config(
        tmp_path,
        data_path=data_path,
        run_id="e2e-cli",
        workers=1,
        strengths=(0.02, 0.1),
    )
    config = load_config(config_path)
    TrainingEngine(config_path).run()

    status = run_cli("snapshot-status", "--config", str(config_path))
    assert status.returncode == 0, status.stderr
    status_doc = json.loads(status.stdout)
    assert status_doc["committed"] is True
    assert status_doc["stage"] == "path_summary"

    validation = run_cli(
        "validate",
        "--config",
        str(config_path),
        "--result",
        str(config.output_directory / "result.json"),
    )
    assert validation.returncode == 0, validation.stderr
    assert json.loads(validation.stdout)["ok"] is True

    rollback = run_cli(
        "snapshot-rollback",
        "--config",
        str(config_path),
        "--to-version",
        "1",
    )
    assert rollback.returncode == 15
    assert json.loads(rollback.stderr)["code"] == "E_SNAPSHOT_ROLLBACK"

