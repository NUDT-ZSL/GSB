from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ctr_pipeline.canonical import atomic_write_json
from ctr_pipeline.errors import (
    E_CONFIG_INVALID,
    E_SNAPSHOT_CORRUPT,
    E_STAGE_BUSY,
    PipelineError,
)
from ctr_pipeline.snapshots import (
    BudgetLedger,
    SnapshotCache,
    SnapshotStore,
    SubmissionGate,
    submit_schedule_plan,
)


def test_snapshot_version_replaces_whole_payload(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    store.publish(
        "schedule_plan",
        {"mode": "full", "nested": {"old_only": True}},
        version=1,
    )
    store.publish("schedule_plan", {"mode": "restricted"}, version=2)
    loaded = store.load("schedule_plan")
    assert loaded["version"] == 2
    assert loaded["payload"] == {"mode": "restricted"}
    assert "nested" not in loaded["payload"]


def test_snapshot_version_must_increase_for_changed_content(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    store.publish("budget_ledger", {"balance": 10}, version=1)
    with pytest.raises(PipelineError) as caught:
        store.publish("budget_ledger", {"balance": 9}, version=1)
    assert caught.value.code == E_CONFIG_INVALID
    assert caught.value.details["field"] == "version"


def test_snapshot_checksum_corruption_reports_object_and_version(
    tmp_path: Path,
) -> None:
    store = SnapshotStore(tmp_path)
    store.publish("training_summary", {"sample_count": 3}, version=9)
    path = tmp_path / "training_summary.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["checksum"] = "f" * 64
    atomic_write_json(path, envelope)
    with pytest.raises(PipelineError) as caught:
        store.load("training_summary")
    error = caught.value
    assert error.code == E_SNAPSHOT_CORRUPT
    assert error.details["object_name"] == "training_summary"
    assert error.details["version"] == 9


def test_snapshot_invalid_metadata_type_is_corruption(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    store.publish("budget_ledger", {"balance": 3}, version=2)
    path = tmp_path / "budget_ledger.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["version"] = "two"
    atomic_write_json(path, envelope)
    with pytest.raises(PipelineError) as caught:
        store.load("budget_ledger")
    assert caught.value.code == E_SNAPSHOT_CORRUPT
    assert caught.value.details["object_name"] == "budget_ledger"


def test_snapshot_cache_reconciles_whole_object(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    first = store.publish(
        "training_summary",
        {"old_key": 1, "sample_count": 2},
        version=1,
    )
    second = store.publish(
        "training_summary",
        {"sample_count": 3},
        version=2,
    )
    cache = SnapshotCache()
    assert cache.reconcile(first) is True
    assert cache.reconcile(second) is True
    assert cache.objects["training_summary"]["payload"] == {"sample_count": 3}
    assert cache.replacements[-1]["previous_version"] == 1
    assert cache.replacements[-1]["version"] == 2
    assert cache.replacements[-1]["replaced_whole_object"] is True


def test_concurrent_submission_fails_immediately(tmp_path: Path) -> None:
    gate = SubmissionGate(tmp_path / "gate.lock")
    gate.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                lambda: submit_schedule_plan(
                    SubmissionGate(tmp_path / "gate.lock"),
                    SnapshotStore(tmp_path / "snapshots"),
                    {"mode": "second"},
                    version=1,
                )
            )
            with pytest.raises(PipelineError) as caught:
                future.result(timeout=2)
        assert caught.value.code == E_STAGE_BUSY
        assert caught.value.stage == "schedule_submit"
    finally:
        gate.release()


def test_ledger_debit_and_release_are_atomic() -> None:
    ledger = BudgetLedger(500)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: ledger.debit(f"tx-{index}", 1), range(100)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: ledger.release(f"tx-{index}"), range(20)))
    snapshot = ledger.snapshot_payload()
    assert snapshot["balance"] == 420
    assert len(snapshot["entries"]) == 80
    assert snapshot["sequence"] == 120
