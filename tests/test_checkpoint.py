from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mfa_guard.engine import EngineConfig, load_engine_from_samples
from mfa_guard.errors import Codes, MFAError
from mfa_guard.locking import FileLock
from mfa_guard.reporting import verify_sealed_report
from mfa_guard.util import canonical_json_bytes, sha256_canonical

from .conftest import normalized_results


def test_half_write_recovery_restores_last_complete_checkpoint(make_engine):
    baseline = make_engine("recovery-baseline", concurrency=1)
    baseline_batch = baseline.process_batch()
    baseline_digest = baseline.ledger.ledger_digest()

    engine = make_engine("recovery", concurrency=2)
    attempts = sorted(
        engine.initial_attempts,
        key=lambda item: (
            item.submitted_at_ms,
            item.attempt_no,
            item.user_id,
            item.factor_id,
        ),
    )
    first = engine.process_batch(attempts[:5], checkpoint=True)
    engine.process_batch(attempts[5:], checkpoint=False)
    assert (
        engine.checkpoints.latest_complete()["ledger_digest"]
        != engine.ledger.ledger_digest()
    )

    recovery = engine.recover()
    assert recovery["status"] == "RECOVERED_HALF_WRITE"
    assert recovery["half_write_detected"] is True
    assert recovery["checkpoint_seq"] == first.batch_seq
    assert engine.ledger.ledger_digest() != baseline_digest

    second = engine.process_batch(attempts[5:], checkpoint=True)
    assert (
        normalized_results(first) + normalized_results(second)
        == normalized_results(baseline_batch)
    )
    assert engine.ledger.ledger_digest() == baseline_digest
    assert engine.checkpoints.latest_complete()["checkpoint_seq"] == second.batch_seq


def test_lock_conflict_returns_e_lock_and_preserves_ledger(make_engine):
    engine = make_engine("lock")
    engine.process_batch()
    digest = engine.ledger.ledger_digest()
    checkpoints_before = engine.ledger.counts()["checkpoints"]
    with FileLock(engine.checkpoints.lock_path):
        with pytest.raises(MFAError) as exc_info:
            engine.checkpoints.write_checkpoint(
                batch_seq=999,
                watermark=0,
                policy_version=engine.policy.policy_version,
            )
    assert exc_info.value.code == Codes.LOCK
    assert engine.ledger.ledger_digest() == digest
    assert engine.ledger.counts()["checkpoints"] == checkpoints_before


def test_latest_complete_skips_corrupt_newer_checkpoint(make_engine):
    engine = make_engine("checkpoint-fallback")
    attempts = sorted(
        engine.initial_attempts,
        key=lambda item: (
            item.submitted_at_ms,
            item.attempt_no,
            item.user_id,
            item.factor_id,
        ),
    )
    first = engine.process_batch(attempts[:5], checkpoint=True)
    second = engine.process_batch(attempts[5:], checkpoint=True)
    assert second.checkpoint["checkpoint_seq"] > first.checkpoint["checkpoint_seq"]
    Path(second.checkpoint["checkpoint_path"]).write_text("{broken", encoding="utf-8")
    latest = engine.checkpoints.latest_complete()
    assert latest is not None
    assert latest["checkpoint_seq"] == first.checkpoint["checkpoint_seq"]


def test_report_is_atomic_and_integrity_sealed(make_engine, sample_root: Path):
    engine = make_engine("report")
    batch = engine.process_batch()
    report_path = sample_root / "reports" / "test_decision_report.json"
    report = engine.write_report(batch, report_path)
    assert verify_sealed_report(report)
    disk = json.loads(report_path.read_text(encoding="utf-8"))
    assert verify_sealed_report(disk)
    disk["counts"]["approved"] = 999
    assert not verify_sealed_report(disk)


def test_ticket_table_primary_key_matches_contract(make_engine):
    engine = make_engine("schema")
    rows = {
        row["name"]: row
        for row in engine.ledger.connection.execute("PRAGMA table_info(tickets)")
    }
    primary_key = [
        row["name"]
        for row in sorted(
            (value for value in rows.values() if value["pk"]),
            key=lambda item: item["pk"],
        )
    ]
    assert primary_key == ["user_id", "factor_id", "ticket_generation"]


def test_checkpoint_snapshot_and_report_leave_no_temp_files(make_engine):
    engine = make_engine("atomic")
    batch = engine.process_batch()
    assert batch.checkpoint["status"] == "WRITTEN"
    leftovers = list(engine.work_dir.glob("*.tmp")) + list(
        engine.work_dir.glob(".*.tmp")
    )
    assert leftovers == []
    snapshot = json.loads(
        Path(batch.checkpoint["snapshot_path"]).read_text(encoding="utf-8")
    )
    body = dict(snapshot)
    checksum = body.pop("checksum")
    assert checksum == sha256_canonical(body)


def test_forced_process_kill_recovers_and_writes_no_exit_report(
    make_engine, sample_root: Path
):
    baseline = make_engine("kill-baseline", concurrency=1)
    baseline.process_batch()
    baseline_digest = baseline.ledger.ledger_digest()

    work = sample_root / "work" / "killed"
    work.mkdir(parents=True)
    marker = work / "committed.marker"
    report_path = sample_root / "reports" / "killed_exit_report.json"
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("kill_worker.py")),
            "--root",
            str(sample_root),
            "--work",
            str(work),
            "--marker",
            str(marker),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 15
    while not marker.exists():
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise AssertionError(f"kill worker exited early: {stderr}")
        if time.time() > deadline:
            process.kill()
            process.wait(timeout=5)
            raise AssertionError("kill worker did not reach the commit marker")
        time.sleep(0.02)
    process.kill()
    process.wait(timeout=5)
    assert not report_path.exists()

    recovered = load_engine_from_samples(
        sample_root / "samples",
        ledger_path=work / "mfa_ledger.sqlite",
        work_dir=work,
        config=EngineConfig(
            canonical_scale=6,
            fingerprint_threshold_micro=5_000,
            concurrency=1,
            timeout_ms=None,
            queue_limit=64,
            auto_recover=True,
        ),
    )
    try:
        assert recovered.recovery_result["status"] == "RECOVERED_HALF_WRITE"
        attempts = sorted(
            recovered.initial_attempts,
            key=lambda item: (
                item.submitted_at_ms,
                item.attempt_no,
                item.user_id,
                item.factor_id,
            ),
        )
        recovered.process_batch(attempts[5:], checkpoint=True)
        assert recovered.ledger.ledger_digest() == baseline_digest
    finally:
        recovered.close()

