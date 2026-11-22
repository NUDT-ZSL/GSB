from __future__ import annotations

import copy
import json

import pytest

from ner_governance.config import load_config
from ner_governance.errors import (
    E_INCONSISTENT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    GovernanceError,
)
from ner_governance.selfcheck import run_self_check
from ner_governance.snapshots import SnapshotStore
from ner_governance.typesets import load_type_set


def _inputs(sample_paths):
    from ner_governance.annotations import parse_annotation

    views = {
        name: load_type_set(sample_paths[name]) for name in ("v1", "v2", "v3")
    }
    config = load_config(sample_paths["config"])
    records = [
        parse_annotation(json.loads(line), line=index)
        for index, line in enumerate(
            sample_paths["annotations"]
            .read_text(encoding="utf-8")
            .splitlines(),
            start=1,
        )
        if line.strip()
    ]
    return views, config, records


def _error(callable_):
    with pytest.raises(GovernanceError) as raised:
        callable_()
    return raised.value


def test_self_check_success(sample_paths):
    views, config, records = _inputs(sample_paths)
    report = run_self_check(records, views, config)
    assert report["ok"]
    assert all(report["checks"].values())
    assert report["token_position_count"] == 134


def test_self_check_detects_doc_set_mismatch(sample_paths):
    views, config, records = _inputs(sample_paths)
    trimmed = [
        item
        for item in records
        if not (
            item.doc_id == "doc-008" and item.annotator_id == "ann_a"
        )
    ]
    error = _error(lambda: run_self_check(trimmed, views, config))
    assert error.code == E_INCONSISTENT
    assert error.details["doc_id"] == "doc-008"
    assert error.details["reason"] == "annotator_doc_id_set_mismatch"


def test_self_check_detects_format_view_drift(sample_paths):
    views, config, records = _inputs(sample_paths)
    drifted = copy.deepcopy(views["v2"])
    drifted.type_map()["ORG"].enabled = False
    views["v2"] = drifted
    error = _error(lambda: run_self_check(records, views, config))
    assert error.code == E_INCONSISTENT
    assert error.details["doc_id"] == "doc-001"
    assert error.details["reason"] == "type_set_view_mismatch"


def test_snapshot_versioning_rollback_and_corruption(tmp_path):
    store = SnapshotStore(tmp_path)
    first = store.write("type_set", {"types": ["A"]})
    assert first.version == 1
    assert first.replaces_version is None
    loaded = store.load("type_set")
    assert loaded.checksum == first.checksum

    second = store.write(
        "type_set",
        {"types": ["A", "B"]},
        base_version=first.version,
        expected_checksum=first.checksum,
    )
    assert second.version == 2
    assert second.replaces_version == 1
    assert (tmp_path / "type_set.snapshot.json").exists()
    assert not (tmp_path / ".type_set.snapshot.json.tmp").exists()

    rollback_write = _error(
        lambda: store.write(
            "type_set",
            {"types": ["OLD"]},
            base_version=1,
        )
    )
    assert rollback_write.code == E_SNAPSHOT_ROLLBACK

    rollback_restore = _error(lambda: store.restore(first))
    assert rollback_restore.code == E_SNAPSHOT_ROLLBACK
    assert rollback_restore.details["current_version"] == 2
    assert rollback_restore.details["requested_version"] == 1

    path = tmp_path / "type_set.snapshot.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["checksum"] = "0" * 64
    path.write_text(json.dumps(envelope), encoding="utf-8")
    corrupt = _error(lambda: store.load("type_set"))
    assert corrupt.code == E_SNAPSHOT_CORRUPT
    assert corrupt.details["reason"] == "checksum_mismatch"


def test_snapshot_stores_merge_result_kind(tmp_path):
    store = SnapshotStore(tmp_path)
    snapshot = store.write("merge_result", {"documents": 2})
    assert snapshot.kind == "merge_result"
    assert store.load("merge_result").payload == {"documents": 2}
