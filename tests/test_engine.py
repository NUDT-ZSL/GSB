from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import load_sample, run_json, sqlite_rows


def test_load_is_idempotent_and_fresh_runs_are_identical(tmp_path: Path) -> None:
    db1 = tmp_path / "one.db"
    db2 = tmp_path / "two.db"
    first = load_sample(db1)
    second = load_sample(db2)
    assert first["loaded_batches"] == second["loaded_batches"]
    assert first["sample_digest"] == second["sample_digest"]

    logs1 = sqlite_rows(
        db1,
        'SELECT topic, partition, "offset", batch_seq, message_index, payload_digest '
        'FROM partition_log ORDER BY topic, partition, "offset"',
    )
    logs2 = sqlite_rows(
        db2,
        'SELECT topic, partition, "offset", batch_seq, message_index, payload_digest '
        'FROM partition_log ORDER BY topic, partition, "offset"',
    )
    assert logs1 == logs2
    status1 = run_json(db1, "status")
    status2 = run_json(db2, "status")
    verify1 = run_json(db1, "verify")
    verify2 = run_json(db2, "verify")
    assert status1 == status2
    assert verify1 == verify2
    assert verify1["ok"] is True

    repeated = load_sample(db1)
    assert repeated["loaded_batches"] == []
    assert repeated["skipped_batches"] == list(range(1, 11))
    assert run_json(db1, "status") == status1
    assert run_json(db1, "verify") == verify1

    routings = sqlite_rows(
        db1,
        "SELECT topic, ordering_key, partition FROM routing_keys ORDER BY topic, ordering_key",
    )
    assert ("orders", "customer-alice", 0) in routings
    assert ("orders", "customer-alice", 0) in routings
    assert len([row for row in routings if row[1] == "customer-alice"]) == 1


def test_transaction_visibility_ack_and_timeout(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    load_sample(db)

    blocked = run_json(
        db, "consume", "--consumer-id", "c1", "--topic", "orders",
        "--partition", "2", "--max", "10", "--logical-ms", "1600"
    )
    assert blocked["messages"] == []

    committed = run_json(db, "commit", "--tx-id", "tx-2001", "--logical-ms", "1700")
    assert committed["state"] == "committed"
    visible = run_json(
        db, "consume", "--consumer-id", "c1", "--topic", "orders",
        "--partition", "2", "--max", "10", "--logical-ms", "1800"
    )
    assert [message["offset"] for message in visible["messages"]] == [0, 1]
    assert [message["message_id"] for message in visible["messages"]] == ["m-0003", "m-0009"]

    gap = run_json(
        db, "ack", "--consumer-id", "c1", "--topic", "orders",
        "--partition", "2", "--offset", "1", "--logical-ms", "1900",
        expected_exit=2,
    )
    assert gap["error"]["code"] == "E_ACK_OFFSET_GAP"

    ack0 = run_json(
        db, "ack", "--consumer-id", "c1", "--topic", "orders",
        "--partition", "2", "--offset", "0", "--logical-ms", "2000"
    )
    assert ack0["committed_offset"] == 1
    duplicate = run_json(
        db, "ack", "--consumer-id", "c1", "--topic", "orders",
        "--partition", "2", "--offset", "0", "--logical-ms", "2050"
    )
    assert duplicate["state"] == "stale_idempotent"
    assert duplicate["committed_offset"] == 1
    ack1 = run_json(
        db, "ack", "--consumer-id", "c1", "--topic", "orders",
        "--partition", "2", "--offset", "1", "--logical-ms", "2100"
    )
    assert ack1["committed_offset"] == 2

    not_delivered = run_json(
        db, "ack", "--consumer-id", "never-consumed", "--topic", "orders",
        "--partition", "1", "--offset", "0", "--logical-ms", "2150",
        expected_exit=2,
    )
    assert not_delivered["error"]["code"] == "E_ACK_NOT_DELIVERED"
    uncommitted = run_json(
        db, "ack", "--consumer-id", "events-consumer", "--topic", "events",
        "--partition", "0", "--offset", "0", "--logical-ms", "2200",
        expected_exit=2,
    )
    assert uncommitted["error"]["code"] == "E_ACK_UNCOMMITTED"

    resumed = run_json(db, "resume", "--logical-ms", "7000")
    timed_out = {item["tx_id"] for item in resumed["timed_out_transactions"]}
    assert {"tx-2002", "tx-2003", "tx-2004", "tx-2005"} <= timed_out
    late_commit = run_json(
        db, "commit", "--tx-id", "tx-2003", "--logical-ms", "7100",
        expected_exit=2,
    )
    assert late_commit["error"]["code"] == "E_TX_TIMEOUT"

    status = run_json(db, "status")
    orders_p0 = next(
        item for item in status["partitions"]
        if item["topic"] == "orders" and item["partition"] == 0
    )
    assert orders_p0["tombstones"] == 2
    assert orders_p0["prepared_messages"] == 0
    verify = run_json(db, "verify")
    assert verify["ok"] is True


def test_rollback_and_tombstone_scan(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    load_sample(db)
    rollback = run_json(db, "rollback", "--tx-id", "tx-2002", "--logical-ms", "1600")
    assert rollback["state"] == "rolled_back"
    assert rollback["tombstoned_messages"] == 1
    commit_after = run_json(
        db, "commit", "--tx-id", "tx-2002", "--logical-ms", "1700",
        expected_exit=2,
    )
    assert commit_after["error"]["code"] == "E_TX_STATE"

    pulled = run_json(
        db, "consume", "--consumer-id", "events-reader", "--topic", "events",
        "--partition", "0", "--max", "10", "--logical-ms", "1800"
    )
    assert [message["offset"] for message in pulled["messages"]] == [1]
    assert pulled["committed_offset"] == 1
    assert run_json(db, "verify")["ok"] is True


def test_commit_timeout_and_expected_digest(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    load_sample(db)
    mismatch = run_json(
        db,
        "commit",
        "--tx-id",
        "tx-2001",
        "--logical-ms",
        "1600",
        "--expected-digest",
        "0" * 64,
        expected_exit=2,
    )
    assert mismatch["error"]["code"] == "E_COMMIT_DIGEST_MISMATCH"
    assert run_json(db, "status")["transaction_counts"] == {"prepared": 5}

    timed_out = run_json(
        db, "commit", "--tx-id", "tx-2003", "--logical-ms", "6200",
        expected_exit=2,
    )
    assert timed_out["error"]["code"] == "E_TX_TIMEOUT"
    status = run_json(db, "status")
    assert status["transaction_counts"] == {"prepared": 4, "timed_out": 1}
    verify = run_json(db, "verify")
    assert verify["ok"] is True


def test_verify_detects_digest_tampering(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    load_sample(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE partition_state SET state_digest = 'tampered' "
            "WHERE topic = 'orders' AND partition = 0"
        )
        conn.commit()
    finally:
        conn.close()
    result = run_json(db, "verify", expected_exit=1)
    assert result["ok"] is False
    assert any(
        error["kind"] == "partition_state_digest_mismatch"
        for error in result["errors"]
    )

