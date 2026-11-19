from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from qconv.consistency import verify_run
from qconv.errors import DomainError, ErrorCode
from qconv.models import ExecutionConfig, SchedulerConfig, ToleranceConfig
from qconv.samplegen import default_sample_jobs
from qconv.service import QConvService
from qconv.snapshots import SnapshotStore


def _components(tag: str) -> dict:
    return {
        "plan": {"tag": tag, "items": [1, 2, 3]},
        "budget_ledger": {"tag": tag, "usage": 3},
        "result_summary": {"tag": tag, "status": "ok"},
    }


def test_snapshot_whole_replacement_versions_and_checksums(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    first = store.commit(_components("first"))
    assert first["generation"] == 1
    assert first["previous_generation"] == 0
    assert {
        wrapper["version"] for wrapper in first["components"].values()
    } == {1}
    second = store.commit(
        {
            "plan": {"tag": "second"},
            "budget_ledger": {"tag": "second", "usage": 9},
            "result_summary": {"tag": "second", "status": "ok"},
        }
    )
    assert second["generation"] == 2
    assert second["previous_generation"] == 1
    loaded = store.load()
    assert loaded == second
    assert loaded["components"]["plan"]["payload"] == {"tag": "second"}
    raw_text = store.snapshot_path.read_text(encoding="utf-8")
    assert "items" not in raw_text


def test_snapshot_corruption_returns_business_code(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    store.commit(_components("ok"))
    raw = json.loads(store.snapshot_path.read_text(encoding="utf-8"))
    raw["components"]["plan"]["payload"]["tag"] = "tampered"
    store.snapshot_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(DomainError) as exc_info:
        store.load()
    assert exc_info.value.code == ErrorCode.E_SNAPSHOT_CORRUPT


def test_snapshot_stage_busy_is_immediate(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    with store.stage_lock():
        with pytest.raises(DomainError) as exc_info:
            store.commit(_components("blocked"))
        assert exc_info.value.code == ErrorCode.E_STAGE_BUSY


def test_concurrent_snapshot_submission_returns_busy_without_waiting(
    tmp_path: Path,
) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    captured: list[DomainError] = []
    with store.stage_lock():
        thread = threading.Thread(
            target=lambda: _capture_commit_error(store, captured)
        )
        started = time.monotonic()
        thread.start()
        thread.join(timeout=1.0)
        elapsed = time.monotonic() - started
    assert not thread.is_alive()
    assert elapsed < 0.5
    assert captured and captured[0].code == ErrorCode.E_STAGE_BUSY


def _capture_commit_error(store: SnapshotStore, captured: list[DomainError]) -> None:
    try:
        store.commit(_components("late"))
    except DomainError as exc:
        captured.append(exc)


def test_cross_module_consistency_detects_tamper(tmp_path: Path) -> None:
    service = QConvService(tmp_path / "snapshots", tmp_path / "cache")
    artifact = service.run_records(
        [job.to_dict() for job in default_sample_jobs()[:1]],
        __import__("qconv.demo", fromlist=["normal_budget"]).normal_budget(),
        SchedulerConfig(max_workers_per_task=1),
        ToleranceConfig(),
        ExecutionConfig(max_processes=1),
        commit_snapshot=False,
    )
    assert artifact["self_check"]["ok"]
    ledger = json.loads(json.dumps(artifact["ledger"]))
    ledger["usage"]["mac_committed"] += 1
    check = verify_run(
        {job.job_id: job for job in default_sample_jobs()[:1]},
        artifact["plan"],
        ledger,
        artifact["execution"],
        artifact["error_spectrum"],
    )
    assert not check["ok"]
    assert any(issue["check"] == "ledger_sum" for issue in check["issues"])


def test_service_snapshot_generation_and_cli_self_check(tmp_path: Path) -> None:
    from qconv.cli import main
    import os

    service = QConvService(tmp_path / "snapshots", tmp_path / "cache")
    first = service.run_records(
        [job.to_dict() for job in default_sample_jobs()[:1]],
        __import__("qconv.demo", fromlist=["normal_budget"]).normal_budget(),
        SchedulerConfig(max_workers_per_task=1),
        ToleranceConfig(),
        ExecutionConfig(max_processes=1),
    )
    second = service.run_records(
        [job.to_dict() for job in default_sample_jobs()[:1]],
        __import__("qconv.demo", fromlist=["normal_budget"]).normal_budget(),
        SchedulerConfig(max_workers_per_task=1),
        ToleranceConfig(),
        ExecutionConfig(max_processes=1),
    )
    assert first["snapshot"]["generation"] == 1
    assert second["snapshot"]["generation"] == 2
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        config = {
            "snapshots_dir": str(tmp_path / "snapshots"),
            "cache_dir": str(tmp_path / "cache"),
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        assert main(["--config", str(config_path), "self-check"]) == 0
    finally:
        os.chdir(previous)

