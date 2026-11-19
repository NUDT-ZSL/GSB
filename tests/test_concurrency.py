from __future__ import annotations

import json
from pathlib import Path

from conftest import load_sample, run_cli_parallel, run_json, sqlite_rows


def test_parallel_produce_to_different_topics(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    load_sample(db)
    orders_batch = {
        "batch_seq": 11,
        "logical_ts_ms": 1550,
        "messages": [
            {
                "message_id": "parallel-orders",
                "payload": {"encoding": "utf8", "data": "orders"},
            }
        ],
        "ordering_key": "customer-dave",
        "topic": "orders",
        "transaction": None,
    }
    events_batch = {
        "batch_seq": 12,
        "logical_ts_ms": 1550,
        "messages": [
            {
                "message_id": "parallel-events",
                "payload": {"encoding": "utf8", "data": "events"},
            }
        ],
        "ordering_key": "sensor-c",
        "topic": "events",
        "transaction": None,
    }
    orders_file = tmp_path / "orders-batch.json"
    events_file = tmp_path / "events-batch.json"
    orders_file.write_text(json.dumps(orders_batch), encoding="utf-8")
    events_file.write_text(json.dumps(events_batch), encoding="utf-8")
    results = run_cli_parallel(
        db,
        [
            ("produce", "--batch-file", str(orders_file)),
            ("produce", "--batch-file", str(events_file)),
        ],
    )
    assert all(result.returncode == 0 for result in results), [
        result.stdout + result.stderr for result in results
    ]
    assert all(json.loads(result.stdout)["state"] == "inserted" for result in results)
    rows = sqlite_rows(
        db,
        "SELECT topic, partition, batch_seq FROM partition_log "
        "WHERE batch_seq IN (11, 12) ORDER BY batch_seq",
    )
    assert rows == [("orders", 0, 11), ("events", 0, 12)]
    assert run_json(db, "verify")["ok"] is True


def test_parallel_duplicate_produce_commit_and_ack_are_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    load_sample(db)
    batch = {
        "batch_seq": 11,
        "logical_ts_ms": 1550,
        "messages": [
            {
                "message_id": "m-parallel-1",
                "payload": {"encoding": "utf8", "data": "parallel"},
            }
        ],
        "ordering_key": "customer-dave",
        "topic": "orders",
        "transaction": None,
    }
    batch_file = tmp_path / "batch.json"
    batch_file.write_text(json.dumps(batch), encoding="utf-8")

    processes = run_cli_parallel(
        db,
        [("produce", "--batch-file", str(batch_file))] * 6,
    )
    assert all(process.returncode == 0 for process in processes), [
        process.stdout + process.stderr for process in processes
    ]
    states = [json.loads(process.stdout)["state"] for process in processes]
    assert states.count("inserted") == 1
    assert states.count("duplicate") == 5
    count = sqlite_rows(
        db, "SELECT COUNT(*) FROM partition_log WHERE batch_seq = 11"
    )[0][0]
    assert count == 1

    commits = run_cli_parallel(
        db,
        [("commit", "--tx-id", "tx-2001", "--logical-ms", "1700")] * 6,
    )
    assert all(process.returncode == 0 for process in commits), [
        process.stdout + process.stderr for process in commits
    ]
    commit_states = [json.loads(process.stdout)["state"] for process in commits]
    assert commit_states.count("committed") == 1
    assert commit_states.count("already_committed") == 5

    run_json(
        db, "consume", "--consumer-id", "parallel", "--topic", "orders",
        "--partition", "2", "--max", "10", "--logical-ms", "1800"
    )
    acks = run_cli_parallel(
        db,
        [
            (
                "ack", "--consumer-id", "parallel", "--topic", "orders",
                "--partition", "2", "--offset", "0", "--logical-ms", "1900",
            )
        ]
        * 6,
    )
    assert all(process.returncode == 0 for process in acks), [
        process.stdout + process.stderr for process in acks
    ]
    ack_states = [json.loads(process.stdout)["state"] for process in acks]
    assert ack_states.count("acked") == 1
    assert ack_states.count("stale_idempotent") == 5
    status = run_json(db, "status")
    parallel_offset = next(
        item for item in status["consumer_offsets"]
        if item["consumer_id"] == "parallel"
    )
    assert parallel_offset["committed_offset"] == 1
    assert run_json(db, "verify")["ok"] is True

