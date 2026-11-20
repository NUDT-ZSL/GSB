from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from summarize_pipeline.errors import ErrorCode, PipelineError
from summarize_pipeline.snapshots import SnapshotStore, build_snapshot
from summarize_pipeline.utils import write_json_atomic


def test_snapshot_commit_rollback_and_same_version_protection(
    tmp_path: Path, report, config
) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    version_two = build_snapshot(report, config, 2)
    stored = store.commit(version_two)
    assert stored["snapshot_version"] == 2
    assert store.current["checksum"] == version_two["checksum"]
    assert (store.history_directory / "v00000002.json").exists()

    version_one = build_snapshot(report, config, 1)
    with pytest.raises(PipelineError) as rollback:
        store.commit(version_one)
    assert rollback.value.code == ErrorCode.SNAPSHOT_ROLLBACK

    changed_config = replace(config, version=3)
    changed_same_version = build_snapshot(report, changed_config, 2)
    with pytest.raises(PipelineError) as same_version:
        store.commit(changed_same_version)
    assert same_version.value.code == ErrorCode.SNAPSHOT_CORRUPT


def test_snapshot_load_whole_replacement_and_tamper_detection(
    tmp_path: Path, report, config
) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    store.commit(build_snapshot(report, config, 1))
    version_three = build_snapshot(report, config, 3)
    external = tmp_path / "external-v3.json"
    write_json_atomic(external, version_three, pretty=False)
    loaded = store.load(external)
    assert loaded["snapshot_version"] == 3
    assert store.current["checksum"] == version_three["checksum"]

    version_two = build_snapshot(report, config, 2)
    external_old = tmp_path / "external-v2.json"
    write_json_atomic(external_old, version_two, pretty=False)
    with pytest.raises(PipelineError) as rollback:
        store.load(external_old)
    assert rollback.value.code == ErrorCode.SNAPSHOT_ROLLBACK

    tampered = copy.deepcopy(version_three)
    tampered["sections"]["scoring_config"]["payload"]["weights"]["position"] = 0.9
    tampered_path = tmp_path / "tampered.json"
    write_json_atomic(tampered_path, tampered, pretty=False)
    with pytest.raises(PipelineError) as corrupt:
        store.load(tampered_path)
    assert corrupt.value.code == ErrorCode.SNAPSHOT_CORRUPT


def test_snapshot_sections_have_checksums_and_versions(report, config) -> None:
    snapshot = build_snapshot(report, config, 7)
    assert snapshot["checksum"]
    for section in snapshot["sections"].values():
        assert section["version"] == 7
        assert section["checksum"]
        assert section["payload"]

