from __future__ import annotations

from pathlib import Path

from conftest import INJECTIONS, SAMPLE, load_sample, run_cli, run_json


def test_crash_after_append_and_prepare_are_replayed(tmp_path: Path) -> None:
    append_db = tmp_path / "append.db"
    crash = run_cli(
        append_db,
        "load",
        "--sample",
        str(SAMPLE),
        "--injections",
        str(INJECTIONS / "crash_append.json"),
    )
    assert crash.returncode == 137
    assert crash.stdout == ""
    status = run_json(append_db, "status")
    assert status["logical_clock_ms"] == 1200
    batch4 = next(
        item for item in status["partitions"]
        if item["topic"] == "orders" and item["partition"] == 0
    )
    assert batch4["prepared_messages"] == 1
    resumed = run_json(append_db, "resume")
    assert resumed["completed_batches"][0]["batch_seq"] == 4
    assert resumed["completed_batches"][0]["status"] == "prepared"
    completed = load_sample(append_db, injection_manifest="crash_append.json")
    assert completed["resumed_batches"] == []
    assert run_json(append_db, "verify")["ok"] is True

    prepare_db = tmp_path / "prepare.db"
    result = run_cli(
        prepare_db,
        "load",
        "--sample",
        str(SAMPLE),
        "--injections",
        str(INJECTIONS / "crash_prepare.json"),
    )
    assert result.returncode == 137
    assert result.stdout == ""
    status = run_json(prepare_db, "status")
    assert status["transaction_counts"] == {"prepared": 3}
    resumed = run_json(prepare_db, "resume")
    assert resumed["completed_batches"] == []
    load_sample(prepare_db, injection_manifest="crash_prepare.json")
    assert run_json(prepare_db, "verify")["ok"] is True


def test_crash_before_commit_is_completed_by_resume(tmp_path: Path) -> None:
    db = tmp_path / "commit.db"
    load_sample(db, injection_manifest="crash_commit.json")
    result = run_cli(db, "commit", "--tx-id", "tx-2001", "--logical-ms", "1700")
    assert result.returncode == 137
    assert result.stdout == ""
    status = run_json(db, "status")
    assert status["transaction_counts"] == {"prepared": 5}
    assert status["pending_operations"] == 1
    resumed = run_json(db, "resume")
    assert resumed["completed_operations"][0]["op_id"] == "commit:tx-2001"
    status = run_json(db, "status")
    assert status["transaction_counts"] == {"committed": 1, "prepared": 4}
    assert status["pending_operations"] == 0
    assert run_json(db, "verify")["ok"] is True


def test_crash_before_ack_is_completed_by_resume(tmp_path: Path) -> None:
    db = tmp_path / "ack.db"
    load_sample(db, injection_manifest="crash_ack.json")
    run_json(db, "commit", "--tx-id", "tx-2001", "--logical-ms", "1700")
    consumed = run_json(
        db, "consume", "--consumer-id", "crash-worker", "--topic", "orders",
        "--partition", "2", "--max", "10", "--logical-ms", "1800"
    )
    assert [message["offset"] for message in consumed["messages"]] == [0, 1]
    result = run_cli(
        db, "ack", "--consumer-id", "crash-worker", "--topic", "orders",
        "--partition", "2", "--offset", "0", "--logical-ms", "1900"
    )
    assert result.returncode == 137
    assert result.stdout == ""
    status = run_json(db, "status")
    assert status["consumer_offsets"][0]["committed_offset"] == 0
    resumed = run_json(db, "resume")
    assert resumed["completed_operations"][0]["op_id"] == "ack:crash-worker:orders:2:0"
    status = run_json(db, "status")
    assert status["consumer_offsets"][0]["committed_offset"] == 1
    assert run_json(db, "verify")["ok"] is True

