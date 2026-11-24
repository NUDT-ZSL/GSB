import threading
from pathlib import Path

import pytest

from pdre.codes import BusinessCode as Codes
from pdre.config import EngineConfig
from pdre.engine import analyze
from pdre.judgment import JudgeOutcome
from pdre.runtime import BoundedUnitProcessor, UnitJob
from pdre.storage import (
    CheckpointManager,
    FileLock,
    Ledger,
    LockConflict,
    StorageError,
)

from factories import event, policy


def test_event_budget_returns_ebudget():
    events = [event(seq=index, process="root1", parent=None) for index in range(4)]
    report = analyze(events, [], [], EngineConfig(concurrency=1, max_events_per_unit=2))
    unit = report["units"][0]
    assert unit["status"] == "budget_exceeded"
    assert unit["errors"][0]["code"] == Codes.BUDGET


def test_wall_clock_timeout_returns_etimeout():
    digest = "sha256:" + "f" * 64
    events = [
        event(seq=index, process="root1", parent=None, observed_ms=index, digest=digest)
        for index in range(400)
    ]
    policies = [
        policy(key=f"pdre:p{index:04d}", digest="sha256:" + f"{index % 16:01x}" * 64)
        for index in range(2000)
    ]
    report = analyze(
        events,
        [],
        policies,
        EngineConfig(concurrency=1, unit_timeout_ms=2, max_queue_size=16),
    )
    unit = report["units"][0]
    assert unit["status"] == "timeout"
    assert unit["errors"][0]["code"] == Codes.TIMEOUT
    assert unit["processed_event_count"] < len(events)
    assert unit["errors"][0]["elapsed_ms_measured"] >= 0


def test_bounded_queue_rejects_overflow():
    entered = threading.Event()
    release = threading.Event()

    def blocking_handler(assignment, events, policies, config):
        entered.set()
        release.wait(timeout=5)
        return JudgeOutcome({"unit_id": assignment.unit_id}, 0.0, False)

    config = EngineConfig(concurrency=1, max_queue_size=1)
    processor = BoundedUnitProcessor(config, handler=blocking_handler).start()
    job = UnitJob(assignment=type("A", (), {"unit_id": "u1"})(), events=[], policies=[])
    queued_job = UnitJob(assignment=type("A", (), {"unit_id": "u2"})(), events=[], policies=[])
    rejected_job = UnitJob(assignment=type("A", (), {"unit_id": "u3"})(), events=[], policies=[])

    assert processor.submit(job) is True
    assert entered.wait(timeout=2)
    assert processor.submit(queued_job) is True
    assert processor.submit(rejected_job) is False
    assert processor.queue_rejections[0]["code"] == Codes.QUEUE_FULL

    release.set()
    processor.wait()


def test_file_lock_conflict_returns_business_code(tmp_path: Path):
    lock_path = tmp_path / "checkpoint.lock"
    with FileLock(lock_path, timeout_ms=0):
        with pytest.raises(LockConflict) as exc_info:
            with FileLock(lock_path, timeout_ms=0):
                pass
    assert exc_info.value.code == Codes.LOCK


def test_checkpoint_recovery_detects_partial_ledger(tmp_path: Path):
    checkpoint_dir = tmp_path / "checkpoints"
    snapshot_dir = tmp_path / "snapshots"
    ledger_path = tmp_path / "ledger.jsonl"
    manager = CheckpointManager(
        checkpoint_dir,
        snapshot_dir=snapshot_dir,
        ledger_path=ledger_path,
    )
    ledger = Ledger(ledger_path)
    append_result = ledger.append_unit_judgments(
        [{"unit_id": "unit-1", "decision": {"action_code": "BLOCK"}}],
        batch_id="batch-1",
    )
    manager.save_checkpoint(
        watermark="batch-1",
        batch_seq=1,
        state={"cursor": 1, "unit_cache": {"unit-1": {"unit_id": "unit-1"}}},
        ledger=append_result,
    )

    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write('{"partial": ')
    (snapshot_dir / "residue.json.tmp").write_text("{broken", encoding="utf-8")

    recovered_manager = CheckpointManager(
        checkpoint_dir,
        snapshot_dir=snapshot_dir,
        ledger_path=ledger_path,
    )
    recovery = recovered_manager.recover()
    assert recovery is not None
    assert recovery.checkpoint["watermark"] == "batch-1"
    assert recovery.state["cursor"] == 1
    assert recovery.residue["half_write_detected"] is True
    assert recovery.residue["extra_bytes"] > 0
    assert ledger_path.stat().st_size == append_result.byte_offset


def test_version_mismatch_requires_full_rebuild(tmp_path: Path):
    paths = dict(
        checkpoint_dir=tmp_path / "checkpoints",
        snapshot_dir=tmp_path / "snapshots",
        ledger_path=tmp_path / "ledger.jsonl",
    )
    old_manager = CheckpointManager(semantics_version=999, **paths)
    ledger = Ledger(paths["ledger_path"])
    result = ledger.append_unit_judgments([{"unit_id": "u"}], batch_id="b")
    old_manager.save_checkpoint(watermark="b", batch_seq=1, state={}, ledger=result)

    current_manager = CheckpointManager(**paths)
    with pytest.raises(StorageError) as exc_info:
        current_manager.recover()
    assert exc_info.value.code == Codes.VERSION_MISMATCH
