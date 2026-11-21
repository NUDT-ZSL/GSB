from __future__ import annotations

import copy
import json

import pytest

from prefworkbench.canonical import read_json
from prefworkbench.errors import BusinessError, E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK
from prefworkbench.pipeline import PreferencePipeline
from prefworkbench.report import write_snapshot_report
from prefworkbench.snapshots import SnapshotStore, build_snapshot_categories


def _state(config: dict, samples: list[dict], tmp_path):
    return PreferencePipeline(config, tmp_path / "cache").run(samples, use_cache=False)


def test_snapshot_versioning_whole_replacement_and_idempotence(config, samples, tmp_path) -> None:
    state = _state(config, samples, tmp_path)
    store = SnapshotStore(tmp_path / "snapshots")
    categories = build_snapshot_categories(state)
    first = store.commit(categories)
    assert first["version"] == 1
    assert first["replaced"]
    same = store.commit(copy.deepcopy(categories))
    assert same["version"] == 1
    assert not same["replaced"]
    assert (tmp_path / "snapshots" / "snapshot-000001.json").exists()

    changed_categories = copy.deepcopy(categories)
    changed_categories["manifest"]["valid_pair_ids"] = changed_categories["manifest"]["valid_pair_ids"][:-1]
    second = store.commit(changed_categories)
    assert second["version"] == 2
    assert second["replaced"]
    assert second["category_checksums"]["manifest"] != first["category_checksums"]["manifest"]
    snapshot = store.load()
    assert snapshot["version"] == 2
    assert snapshot["categories"]["manifest"]["payload"]["valid_pair_ids"] == changed_categories[
        "manifest"
    ]["valid_pair_ids"]
    for category in ("manifest", "statistics", "error_spectrum"):
        envelope = snapshot["categories"][category]
        assert envelope["version"] == 2
        assert envelope["checksum"]
    assert (tmp_path / "snapshots" / "snapshot-000002.json").exists()


def test_execution_metadata_only_does_not_create_a_new_snapshot(config, samples, tmp_path) -> None:
    state = _state(config, samples, tmp_path)
    store = SnapshotStore(tmp_path / "snapshots")
    categories = build_snapshot_categories(state)
    first = store.commit(categories)
    metadata_only = copy.deepcopy(categories)
    metadata_only["statistics"]["execution_mode"] = "incremental"
    metadata_only["statistics"]["invalidation"]["mode"] = "incremental"
    metadata_only["statistics"]["invalidation"]["reused_levels"] = [
        "quality",
        "dedup",
        "consistency",
        "sampling",
        "manifest",
    ]
    second = store.commit(metadata_only)
    assert second["version"] == first["version"]
    assert not second["replaced"]
    assert second["idempotence_basis"] == "logic_digest"


def test_snapshot_corruption_reports_category_and_version(config, samples, tmp_path) -> None:
    state = _state(config, samples, tmp_path)
    store = SnapshotStore(tmp_path / "snapshots")
    commit = store.commit(build_snapshot_categories(state))
    path = tmp_path / "snapshots" / f"snapshot-{commit['version']:06d}.json"
    snapshot = read_json(path)
    snapshot["categories"]["statistics"]["payload"]["valid_sample_count"] = -1
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BusinessError) as caught:
        store.load()
    assert caught.value.code == E_SNAPSHOT_CORRUPT
    assert caught.value.details["category"] == "statistics"
    assert caught.value.details["version"] == commit["version"]


def test_snapshot_rollback_is_rejected(config, samples, tmp_path) -> None:
    state = _state(config, samples, tmp_path)
    store = SnapshotStore(tmp_path / "snapshots")
    store.commit(build_snapshot_categories(state))
    with pytest.raises(BusinessError) as caught:
        store.attempt_rollback(0)
    assert caught.value.code == E_SNAPSHOT_ROLLBACK
    assert caught.value.details["current_version"] == 1


def test_report_is_self_contained_and_contains_diagnostics(config, samples, tmp_path) -> None:
    state = _state(config, samples, tmp_path)
    store = SnapshotStore(tmp_path / "snapshots")
    store.commit(build_snapshot_categories(state))
    snapshot = store.load()
    report_path = tmp_path / "workbench.html"
    write_snapshot_report(snapshot, report_path)
    html = report_path.read_text(encoding="utf-8")
    assert "<script src=" not in html
    assert "http://" not in html
    assert "https://" not in html
    assert "diagnostic-data" in html
    for marker in (
        "误差传播链",
        "失效传播覆盖范围",
        "保留集 / 剔除集",
        "配额达成与欠配归因",
        "最近业务错误码",
        "清单与逐级追踪",
    ):
        assert marker in html
    start = html.index('<script id="diagnostic-data" type="application/json">') + len(
        '<script id="diagnostic-data" type="application/json">'
    )
    end = html.index("</script>", start)
    embedded = json.loads(html[start:end])
    assert embedded["version"] == snapshot["version"]
    assert embedded["statistics"]["logic_digest"] == state["execution"]["logic_digest"]

