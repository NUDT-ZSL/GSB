from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from memguard.codes import E_LOCK, E_LEDGER_REPLAY_MISMATCH
from memguard.engine import RunOptions, run
from memguard.ledger import Ledger
from memguard.models import BatchDeltaEntry, LedgerBatch
from memguard.util import FileLock, GuardError

from conftest import ROOT, decision_snapshot, read_jsonl, sha256_file, write_jsonl


def _batch(batch_id: str, seq: int, kind: str = "read") -> LedgerBatch:
    return LedgerBatch(
        batch_id=batch_id,
        first_seq=seq,
        last_seq=seq,
        entries=(
            BatchDeltaEntry(
                region_id="region-root",
                access_kind=kind,
                access_seq=seq,
                oob=False,
                access_end=seq + 1,
            ),
        ),
    )


def test_batch_validation_failure_leaves_no_partial_write(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    snapshot_path = tmp_path / "snapshot.json"
    with Ledger(db_path, snapshot_path) as ledger:
        ledger.prepare("layout-a")
        assert ledger.apply_batch(_batch("batch-1", 1)) is True
        before_rows = ledger.rows()
        before_batches = ledger.db.execute(
            "SELECT COUNT(*) AS n FROM batches"
        ).fetchone()["n"]
        bad = LedgerBatch(
            batch_id="batch-2",
            first_seq=2,
            last_seq=2,
            entries=(
                BatchDeltaEntry("region-root", "read", 2, False, 3),
                BatchDeltaEntry("region-root", "execute", 2, False, 3),
            ),
        )
        with pytest.raises(GuardError):
            ledger.apply_batch(bad)
        assert ledger.rows() == before_rows
        assert (
            ledger.db.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]
            == before_batches
        )


def test_layout_change_rebuilds_ledger(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    snapshot_path = tmp_path / "snapshot.json"
    with Ledger(db_path, snapshot_path) as ledger:
        ledger.prepare("layout-a")
        ledger.apply_batch(_batch("batch-1", 1))
        old_version = json.loads(snapshot_path.read_text(encoding="utf-8"))[
            "snapshot_version"
        ]
        recovery = ledger.prepare("layout-b")
        assert recovery.rebuilt is True
        assert ledger.rows() == []
        new_version = json.loads(snapshot_path.read_text(encoding="utf-8"))[
            "snapshot_version"
        ]
        assert new_version > old_version


def test_region_generation_change_rebuilds_without_double_count(
    sample: Path, tmp_path: Path
) -> None:
    database = tmp_path / "generation.sqlite"
    snapshot = tmp_path / "generation.snapshot.json"
    first = run(
        RunOptions(
            region_paths=[sample / "regions.json"],
            trace_paths=[sample / "traces.jsonl"],
            database_path=database,
            snapshot_path=snapshot,
            report_path=tmp_path / "generation-first.json",
            max_workers=2,
        )
    )
    document = json.loads((sample / "regions.json").read_text(encoding="utf-8"))
    for region in document["regions"]:
        if region["region_id"] == "region-root":
            region["generation"] += 1
    (sample / "regions.json").write_text(json.dumps(document), encoding="utf-8")
    second = run(
        RunOptions(
            region_paths=[sample / "regions.json"],
            trace_paths=[sample / "traces.jsonl"],
            database_path=database,
            snapshot_path=snapshot,
            report_path=tmp_path / "generation-second.json",
            max_workers=2,
        )
    )
    assert second.report["recovery"]["rebuilt"] is True
    assert first.report["ledger"]["rows"] == second.report["ledger"]["rows"]


def test_recovery_detects_ledger_tampering(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    snapshot_path = tmp_path / "snapshot.json"
    with Ledger(db_path, snapshot_path) as ledger:
        ledger.prepare("layout-a")
        ledger.apply_batch(_batch("batch-1", 1))
    connection = sqlite3.connect(str(db_path))
    connection.execute(
        "UPDATE region_ledger SET access_count = access_count + 7 "
        "WHERE region_id = 'region-root' AND access_kind = 'read'"
    )
    connection.commit()
    connection.close()
    with Ledger(db_path, snapshot_path) as ledger:
        with pytest.raises(GuardError) as error:
            ledger.prepare("layout-a")
        assert error.value.business_code == E_LEDGER_REPLAY_MISMATCH


def test_large_unsigned_upper_bound_is_exact(tmp_path: Path) -> None:
    db_path = tmp_path / "large.sqlite"
    snapshot_path = tmp_path / "large.snapshot.json"
    maximum = (1 << 64) - 1
    with Ledger(db_path, snapshot_path) as ledger:
        ledger.prepare("layout-large")
        ledger.apply_batch(
            LedgerBatch(
                "large-batch",
                1,
                1,
                (BatchDeltaEntry("region-root", "read", 1, True, maximum),),
            )
        )
        rows = ledger.rows()
        assert rows[0]["oob_upper_bound"] == maximum


def test_large_unsigned_access_sequence_is_exact(tmp_path: Path) -> None:
    db_path = tmp_path / "large-seq.sqlite"
    snapshot_path = tmp_path / "large-seq.snapshot.json"
    maximum = (1 << 64) - 1
    with Ledger(db_path, snapshot_path) as ledger:
        ledger.prepare("layout-large-seq")
        ledger.apply_batch(
            LedgerBatch(
                "large-seq-batch",
                maximum,
                maximum,
                (BatchDeltaEntry("region-root", "read", maximum, False, 1),),
            )
        )
        rows = ledger.rows()
        assert rows[0]["processed_access_seq"] == maximum
        assert ledger.checkpoint_metadata()["watermark_seq"] == maximum
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["watermark_seq"] == maximum


def test_invalid_final_snapshot_is_repaired_from_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "invalid-snapshot.sqlite"
    snapshot_path = tmp_path / "invalid-snapshot.json"
    with Ledger(db_path, snapshot_path) as ledger:
        ledger.prepare("layout-invalid")
        ledger.apply_batch(_batch("batch-1", 1))
        expected_rows = ledger.rows()
    snapshot_path.write_text("{not-json", encoding="utf-8")
    with Ledger(db_path, snapshot_path) as ledger:
        recovery = ledger.prepare("layout-invalid")
        assert ledger.rows() == expected_rows
        assert recovery.snapshot_status == "repaired"
        assert any(
            item.business_code == "E_SNAPSHOT_INVALID"
            for item in recovery.warnings
        )
    json.loads(snapshot_path.read_text(encoding="utf-8"))


def _cli_command(sample: Path, tmp_path: Path, batch_size: int = 3) -> list[str]:
    return [
        sys.executable,
        "-m",
        "memguard",
        "run",
        "--regions",
        str(sample / "regions.json"),
        "--trace",
        str(sample / "traces.jsonl"),
        "--truth-markers",
        str(sample / "sync_markers.json"),
        "--database",
        str(tmp_path / "recover.sqlite"),
        "--snapshot",
        str(tmp_path / "recover.snapshot.json"),
        "--report",
        str(tmp_path / "recover-report.json"),
        "--workers",
        "2",
        "--batch-size",
        str(batch_size),
    ]


def test_forced_termination_and_recovery(sample: Path, tmp_path: Path) -> None:
    command = _cli_command(sample, tmp_path)
    environment = dict(os.environ)
    environment["MEMGUARD_TEST_KILL_AFTER_BATCHES"] = "1"
    killed = subprocess.run(
        command,
        cwd=str(ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert killed.returncode == 97
    assert (tmp_path / "recover.sqlite").exists()
    assert not (tmp_path / "recover-report.json").exists()

    half_written = tmp_path / ".recover.snapshot.json.999.dead.tmp"
    half_written.write_text('{"snapshot_version": broken', encoding="utf-8")
    resumed = subprocess.run(
        command,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert resumed.returncode in (0, 1)
    assert resumed.stdout
    report = json.loads((tmp_path / "recover-report.json").read_text(encoding="utf-8"))
    assert report["recovery"]["skipped_batches"] > 0
    assert report["recovery"]["stale_temp_files"]
    assert any(
        item["business_code"] in {"E_SNAPSHOT_STALE", "E_SNAPSHOT_INVALID"}
        for item in report["recovery"]["warnings"]
    )
    assert report["truth"]["ok"] is True
    assert not half_written.exists()

    reference = run(
        RunOptions(
            region_paths=[sample / "regions.json"],
            trace_paths=[sample / "traces.jsonl"],
            database_path=tmp_path / "reference.sqlite",
            snapshot_path=tmp_path / "reference.snapshot.json",
            report_path=tmp_path / "reference-report.json",
            truth_marker_paths=[sample / "sync_markers.json"],
            max_workers=1,
            batch_size=3,
        )
    )
    assert decision_snapshot(report) == decision_snapshot(reference.report)


def test_input_files_are_not_modified(sample: Path, tmp_path: Path) -> None:
    paths = [
        sample / "regions.json",
        sample / "traces.jsonl",
        sample / "sync_markers.json",
    ]
    before = {str(path): sha256_file(path) for path in paths}
    run(
        RunOptions(
            region_paths=[sample / "regions.json"],
            trace_paths=[sample / "traces.jsonl"],
            database_path=tmp_path / "ledger.sqlite",
            report_path=tmp_path / "report.json",
            truth_marker_paths=[sample / "sync_markers.json"],
            max_workers=3,
        )
    )
    after = {str(path): sha256_file(path) for path in paths}
    assert before == after


def test_lock_conflict_returns_e_lock_and_preserves_ledger(sample: Path, tmp_path: Path) -> None:
    database = tmp_path / "locked.sqlite"
    lock_path = database.with_suffix(database.suffix + ".lock")
    with FileLock(lock_path):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "memguard",
                "run",
                "--regions",
                str(sample / "regions.json"),
                "--trace",
                str(sample / "traces.jsonl"),
                "--database",
                str(database),
                "--report",
                str(tmp_path / "locked-report.json"),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["business_code"] == E_LOCK
    assert not database.exists()
    assert not (tmp_path / "locked-report.json").exists()


def test_split_trace_arrival_order_is_equivalent(sample: Path, tmp_path: Path) -> None:
    records = read_jsonl(sample / "traces.jsonl")
    first = tmp_path / "part-a.jsonl"
    second = tmp_path / "part-b.jsonl"
    write_jsonl(first, records[::2])
    write_jsonl(second, records[1::2])
    common = dict(
        region_paths=[sample / "regions.json"],
        truth_marker_paths=[sample / "sync_markers.json"],
        max_workers=2,
        batch_size=3,
    )
    one = run(
        RunOptions(
            trace_paths=[first, second],
            database_path=tmp_path / "a.sqlite",
            report_path=tmp_path / "a.json",
            snapshot_path=tmp_path / "a.snapshot.json",
            **common,
        )
    )
    two = run(
        RunOptions(
            trace_paths=[second, first],
            database_path=tmp_path / "b.sqlite",
            report_path=tmp_path / "b.json",
            snapshot_path=tmp_path / "b.snapshot.json",
            **common,
        )
    )
    assert decision_snapshot(one.report) == decision_snapshot(two.report)
