from __future__ import annotations

import pytest

from table_features.canonical import read_json
from table_features.errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    BusinessError,
)
from table_features.pipeline import Pipeline
from table_features.snapshots import SnapshotStore


def test_snapshot_whole_replacement_and_rollback(tmp_path):
    store = SnapshotStore(tmp_path / "snap", clear=True)
    first = store.put("config", 1, {"value": "one", "nested": {"x": 1}})
    assert first.status == "created" and first.replaced_entirely
    second = store.put("config", 2, {"value": "two"})
    assert second.status == "replaced_new_version"
    assert store.get("config")["payload"] == {"value": "two"}
    with pytest.raises(BusinessError) as caught:
        store.put("config", 1, {"value": "rollback"})
    assert caught.value.record.code == E_SNAPSHOT_ROLLBACK
    assert caught.value.record.feature == "config"
    assert store.get("config")["version"] == 2


def test_same_version_changed_checksum_replaces_entire_payload(tmp_path):
    store = SnapshotStore(tmp_path / "snap", clear=True)
    store.put("statistics", 3, {"a": 1, "b": 2})
    replaced = store.put("statistics", 3, {"a": 9})
    assert replaced.status == "replaced_same_version_changed_checksum"
    assert replaced.replaced_entirely
    payload = store.get("statistics")["payload"]
    assert payload == {"a": 9}
    assert "b" not in payload


def test_snapshot_checksum_corruption_is_detected(tmp_path):
    store = SnapshotStore(tmp_path / "snap", clear=True)
    store.put("selection", 1, {"selected": ["a"], "scores": {"a": 1.0}})
    path = tmp_path / "snap" / "selection.json"
    envelope = read_json(path)
    envelope["payload"]["scores"]["a"] = 999.0
    path.write_text(__import__("json").dumps(envelope), encoding="utf-8")
    with pytest.raises(BusinessError) as caught:
        store.get("selection")
    assert caught.value.record.code == E_SNAPSHOT_CORRUPT
    assert caught.value.record.feature == "selection"


def test_pipeline_reports_config_snapshot_rollback(tmp_path, rows, cfg):
    store = SnapshotStore(tmp_path / "snap", clear=True)
    store.put("config", 2, {"future": True})
    pipeline = Pipeline(tmp_path / "cache", tmp_path / "snap")
    result = pipeline.run(rows, cfg)
    assert "E_SNAPSHOT_ROLLBACK" in result.error_codes
    assert result.errors[0].feature == "config"
    assert result.selection is None


def test_pipeline_snapshot_versions_are_independent(tmp_path, rows, cfg):
    result = Pipeline(tmp_path / "cache", tmp_path / "snap", clear=True).run(rows, cfg)
    statuses = {write.kind: write for write in result.snapshot_writes}
    assert statuses["config"].version == cfg.config_version
    assert statuses["statistics"].version == cfg.statistics_version
    assert statuses["selection"].version == cfg.selection_version
    assert all(write.checksum for write in statuses.values())

