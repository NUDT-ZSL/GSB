from __future__ import annotations

import json
from pathlib import Path

import pytest

from multilabel_lab import (
    SnapshotStore,
    build_default_payloads,
    create_projection,
    default_config,
)
from multilabel_lab.errors import E_SNAPSHOT_CORRUPT, E_SNAPSHOT_VERSION, LabError
from multilabel_lab.hashing import sha256_json
from multilabel_lab.snapshot import SnapshotCorrupt


def payloads(marker: str = "v1"):
    config = default_config()
    config["labels"] = [f"{label}-{marker}" for label in config["labels"]]
    config["decision"]["thresholds"] = {
        label: 0.5 for label in config["labels"]
    }
    config["seed"] = 42
    projection = create_projection(config)
    return build_default_payloads(
        config,
        sample_count=4,
        annotation_seed=9,
        projection=projection,
    )


def test_commit_replaces_whole_bundle_and_versions_increase(tmp_path: Path):
    store = SnapshotStore(tmp_path / "snapshots")
    first = store.commit(
        payloads("v1"),
        versions={"config": 1, "projection": 1, "annotations": 1},
    )
    first_bytes = store.path.read_bytes()
    second = store.commit(payloads("v2"))
    second_bytes = store.path.read_bytes()

    assert first_bytes != second_bytes
    assert not store.temp_path.exists()
    assert second["replaced"] is True
    assert set(second["changed_categories"]) == {"config", "projection", "annotations"}
    assert set(second["change_reasons"].values()) == {"version"}
    snapshot = second["snapshot"]
    assert all(
        snapshot["categories"][category]["version"] == 2
        for category in ("config", "projection", "annotations")
    )
    assert snapshot["categories"]["config"]["payload"]["labels"][0] == "cat-v2"

    with pytest.raises(LabError) as caught:
        store.commit(payloads("v3"), versions={"config": 2, "projection": 3, "annotations": 3})
    assert caught.value.code == E_SNAPSHOT_VERSION


def test_checksum_mismatch_rejects_entire_snapshot(tmp_path: Path):
    store = SnapshotStore(tmp_path / "snapshots")
    store.commit(payloads())
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["categories"]["config"]["payload"]["seed"] = 999
    store.path.write_text(
        json.dumps(data, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(SnapshotCorrupt) as caught:
        store.load()
    assert caught.value.code == E_SNAPSHOT_CORRUPT
    assert caught.value.details["category"] == "config"
    assert caught.value.details["version"] == 1


def test_checksum_change_with_same_version_is_detected_and_replaced(tmp_path: Path):
    store = SnapshotStore(tmp_path / "snapshots")
    first = store.commit(payloads("v1"))
    assert first["snapshot"]["categories"]["config"]["version"] == 1

    data = json.loads(store.path.read_text(encoding="utf-8"))
    config_payload = data["categories"]["config"]["payload"]
    config_payload["seed"] = 777
    data["categories"]["config"]["checksum"] = sha256_json(
        {"category": "config", "version": 1, "payload": config_payload}
    )
    store.path.write_text(
        json.dumps(data, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    loaded = store.load()
    assert loaded["replaced"] is True
    assert loaded["changed_categories"] == ["config"]
    assert loaded["change_reasons"] == {"config": "checksum"}
    assert loaded["snapshot"]["categories"]["config"]["payload"]["seed"] == 777
    assert loaded["snapshot"]["categories"]["projection"]["version"] == 1


def test_corrupt_json_identifies_bundle_category_and_version(tmp_path: Path):
    store = SnapshotStore(tmp_path / "snapshots")
    store.commit(payloads())
    store.path.write_text('{"schema":', encoding="utf-8")
    with pytest.raises(SnapshotCorrupt) as caught:
        store.load()
    assert caught.value.details["category"] == "__bundle__"
    assert caught.value.details["version"] is None
