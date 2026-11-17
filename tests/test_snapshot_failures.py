from __future__ import annotations

import json
from pathlib import Path

from gbdt_workbench.errors import E_CONFIG_MISMATCH, E_TRAIN_TREE
from gbdt_workbench.pipeline import WorkbenchEngine
from gbdt_workbench.snapshots import SNAPSHOT_VERSION

from .helpers import read_json, rewrite_config, write_config


def test_stage_snapshots_are_atomic_and_versioned(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        experiment={"n_trees": 5},
        runtime={"n_jobs": 2, "snapshot_interval": 2},
    )
    output = tmp_path / "out"
    result = WorkbenchEngine(config_path, output).run()
    assert result.exit_code == 0

    snapshot_root = output / "snapshots"
    for stage in ("binning", "trees", "ensemble", "contributions", "diagnostics"):
        assert (snapshot_root / stage).is_dir()
        assert list((snapshot_root / stage).glob("*.json"))
    assert not list(snapshot_root.rglob("*.tmp"))
    manifest = read_json(snapshot_root / "manifest.json")
    assert manifest["snapshot_version"] == SNAPSHOT_VERSION
    audit = result.run_report["operational_audit"]
    assert audit["snapshot"]["commit_count"] >= 1
    assert audit["snapshot"]["commits_this_run"] >= 1


def test_snapshot_version_mismatch_returns_config_mismatch(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    output = tmp_path / "out"
    first = WorkbenchEngine(config_path, output).run()
    assert first.exit_code == 0
    manifest_path = output / "snapshots" / "manifest.json"
    manifest = read_json(manifest_path)
    manifest["snapshot_version"] = "incompatible-version"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    second = WorkbenchEngine(config_path, output).run()
    assert second.exit_code == 3
    assert second.status == "failed"
    assert second.diagnostic_report["failures"][0]["code"] == E_CONFIG_MISMATCH
    assert (
        second.diagnostic_report["failures"][0]["details"]["reason"]
        == "snapshot_version_mismatch"
    )


def test_strict_restore_rejects_changed_config_fingerprint(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    output = tmp_path / "out"
    first = WorkbenchEngine(config_path, output).run()
    assert first.exit_code == 0
    rewrite_config(config_path, experiment={"learning_rate": 0.2})

    second = WorkbenchEngine(config_path, output, strict_restore=True).run()
    assert second.exit_code == 3
    assert second.diagnostic_report["failures"][0]["code"] == E_CONFIG_MISMATCH
    assert (
        second.diagnostic_report["failures"][0]["details"]["reason"]
        == "config_fingerprint_mismatch"
    )


def test_single_tree_failure_is_isolated_and_reported(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        experiment={"n_trees": 5},
        runtime={"n_jobs": 3, "snapshot_interval": 1},
        diagnostics={"fault_tree_indices": [2]},
    )
    result = WorkbenchEngine(config_path, tmp_path / "out").run()

    assert result.exit_code == 1
    assert result.status == "partial_failure"
    failures = result.diagnostic_report["failures"]
    assert failures == [
        {
            "code": E_TRAIN_TREE,
            "stage": "train_tree",
            "message": "deterministic fault injection requested by diagnostics.fault_tree_indices",
            "tree_index": 2,
            "details": {"injected": True},
        }
    ]
    assert [tree["tree_index"] for tree in result.diagnostic_report["trees"]] == [
        0,
        1,
        3,
        4,
    ]
    assert result.diagnostic_report["ensemble"]["tree_indices"] == [0, 1, 3, 4]

