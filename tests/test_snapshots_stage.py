from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from mixedpipe.errors import (
    E_SNAPSHOT_CORRUPT,
    E_STAGE_BUSY,
    E_STALE_VERSION,
    BusinessError,
)
from mixedpipe.models import load_config, load_manifest
from mixedpipe.pipeline import Pipeline
from mixedpipe.snapshots import SnapshotStore
from mixedpipe.stage import StageGate


def test_snapshot_versions_replace_wholesale_and_reject_rollback(
    tmp_path: Path,
) -> None:
    store = SnapshotStore(tmp_path)
    first = store.commit("plan", {"value": 1}, 1)
    second = store.commit("plan", {"value": 2}, 2)
    assert first.version == 1
    assert second.version == 2
    assert store.load("plan").payload == {"value": 2}

    restarted = SnapshotStore(tmp_path)
    assert restarted.load("plan").payload == {"value": 2}
    with pytest.raises(BusinessError) as excinfo:
        restarted.commit("plan", {"value": 0}, 1)
    assert excinfo.value.code == E_STALE_VERSION
    assert excinfo.value.details["current_version"] == 2


def test_snapshot_checksum_corruption_reports_category_and_version(
    tmp_path: Path,
) -> None:
    store = SnapshotStore(tmp_path)
    store.commit("ledger", {"value": 10}, 4)
    path = store.path_for("ledger")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"]["value"] = 11
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(BusinessError) as excinfo:
        SnapshotStore(tmp_path).load("ledger")
    assert excinfo.value.code == E_SNAPSHOT_CORRUPT
    assert excinfo.value.details["snapshot_category"] == "ledger"
    assert excinfo.value.details["snapshot_version"] == 4


def test_truncated_snapshot_reports_extractable_version(tmp_path: Path) -> None:
    path = tmp_path / "results.json"
    path.write_text('{"version":7,"category":"results"', encoding="utf-8")
    with pytest.raises(BusinessError) as excinfo:
        SnapshotStore(tmp_path).load("results")
    assert excinfo.value.code == E_SNAPSHOT_CORRUPT
    assert excinfo.value.details["snapshot_category"] == "results"
    assert excinfo.value.details["snapshot_version"] == 7


def test_stage_busy_is_immediate(demo_root: Path) -> None:
    gate = StageGate(demo_root / "data" / "runtime")
    gate.acquire()
    started = time.monotonic()
    try:
        with pytest.raises(BusinessError) as excinfo:
            Pipeline(demo_root).run(
                load_manifest(demo_root / "data" / "jobs.jsonl"),
                load_config(demo_root / "configs" / "demo.json"),
            )
    finally:
        gate.release()
    elapsed = time.monotonic() - started
    assert excinfo.value.code == E_STAGE_BUSY
    assert elapsed < 1.0
