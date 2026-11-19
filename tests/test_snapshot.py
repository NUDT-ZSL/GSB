from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from qrw.errors import BusinessError
from qrw.pipeline import compile_snapshot
from qrw.snapshot import SnapshotManager, load_current_snapshot, load_snapshot


def test_version_index_and_whole_snapshot_replace(samples: Path, config):
    snapshot, index = load_current_snapshot(samples / "versions.json")
    assert snapshot.version == index["current_version"] == 2
    manager = SnapshotManager()
    first = manager.install(load_snapshot(samples / "snapshots" / "snapshot_1.json"))
    second = manager.install(load_snapshot(samples / "snapshots" / "snapshot_2.json"))
    assert first["replaced"] is True
    assert second["replaced"] is True
    assert second["incremental"] is False
    assert second["strategy"] == "whole_snapshot_replace"
    assert second["previous_version"] == 1


def test_rollback_is_rejected(samples: Path):
    manager = SnapshotManager()
    manager.install(load_snapshot(samples / "snapshots" / "snapshot_1.json"))
    manager.install(load_snapshot(samples / "snapshots" / "snapshot_2.json"))
    with pytest.raises(BusinessError) as exc:
        manager.install(load_snapshot(samples / "snapshots" / "snapshot_1.json"))
    assert exc.value.code == "E_SNAPSHOT_ROLLBACK"
    assert exc.value.details["current_version"] == 2
    assert exc.value.details["attempted_version"] == 1


def test_checksum_mismatch_is_corrupt(samples: Path, tmp_path: Path):
    (tmp_path / "snapshots").mkdir()
    shutil.copytree(samples / "snapshot_sources", tmp_path / "snapshot_sources")
    manifest = json.loads(
        (samples / "snapshots" / "snapshot_2.json").read_text(encoding="utf-8")
    )
    target = tmp_path / "snapshot_sources" / "v2" / "v1.txt"
    target.write_text(target.read_text(encoding="utf-8") + "# tamper\n", encoding="utf-8")
    manifest_path = tmp_path / "snapshots" / "bad.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BusinessError) as exc:
        load_snapshot(manifest_path)
    assert exc.value.code == "E_SNAPSHOT_CORRUPT"
    assert exc.value.field == "checksum"


def test_retired_slot_type_is_rejected_atomically(samples: Path, config):
    manager = SnapshotManager()
    current = load_snapshot(samples / "snapshots" / "snapshot_2.json")
    retired = load_snapshot(samples / "snapshots" / "snapshot_retired.json")
    manager.install(current)
    compiled = compile_snapshot(current, config)
    with pytest.raises(BusinessError) as exc:
        manager.install(retired, active_ir=compiled["v2"])
    assert exc.value.code == "E_SLOT_TYPE_RETIRED"
    assert exc.value.details["rule_id"] == "R_area"
    assert exc.value.details["slot"] == "area"
    assert exc.value.details["slot_type"] == "AREA"
    assert manager.current is current

