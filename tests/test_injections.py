from __future__ import annotations

from pathlib import Path

from conftest import load_sample, run_json


def test_full_chain_injections_are_distinct_and_retryable(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    append_error = load_sample(
        db, injection_manifest="full_chain.json", expected_exit=2
    )
    assert append_error["error"]["code"] == "E_APPEND_LOG_FULL"
    assert append_error["error"]["details"]["batch_seq"] == 9
    status = run_json(db, "status")
    assert status["logical_clock_ms"] == 1450
    assert status["transaction_counts"] == {"prepared": 3}
    assert run_json(db, "verify")["ok"] is True

    prepare_error = load_sample(
        db, injection_manifest="full_chain.json", expected_exit=2
    )
    assert prepare_error["error"]["code"] == "E_PREPARE_TX_CONFLICT"
    assert prepare_error["error"]["details"]["tx_id"] == "tx-2005"
    status = run_json(db, "status")
    assert status["transaction_counts"] == {"prepared": 4}
    incomplete_verify = run_json(db, "verify", expected_exit=1)
    assert {
        (error["kind"], error.get("batch_seq"))
        for error in incomplete_verify["errors"]
    } == {
        ("batch_transaction_status_mismatch", 10),
        ("invalid_prepared_visibility", None),
    }

    completed = load_sample(db, injection_manifest="full_chain.json")
    assert completed["resumed_batches"][0]["batch_seq"] == 10
    assert completed["prepared_transactions"] == [
        "tx-2001", "tx-2002", "tx-2003", "tx-2004", "tx-2005"
    ]
    assert run_json(db, "verify")["ok"] is True

    commit_error = run_json(
        db, "commit", "--tx-id", "tx-2001", "--logical-ms", "1700",
        expected_exit=2,
    )
    assert commit_error["error"]["code"] == "E_COMMIT_DIGEST_MISMATCH"
    commit_ok = run_json(
        db, "commit", "--tx-id", "tx-2001", "--logical-ms", "1750"
    )
    assert commit_ok["state"] == "committed"

    consumed = run_json(
        db, "consume", "--consumer-id", "analyst", "--topic", "orders",
        "--partition", "2", "--max", "10", "--logical-ms", "1800"
    )
    assert [message["offset"] for message in consumed["messages"]] == [0, 1]
    ack_error = run_json(
        db, "ack", "--consumer-id", "analyst", "--topic", "orders",
        "--partition", "2", "--offset", "0", "--logical-ms", "1900",
        expected_exit=2,
    )
    assert ack_error["error"]["code"] == "E_ACK_OFFSET_REGRESSION"
    ack_ok = run_json(
        db, "ack", "--consumer-id", "analyst", "--topic", "orders",
        "--partition", "2", "--offset", "0", "--logical-ms", "2000"
    )
    assert ack_ok["state"] == "acked"
    assert run_json(db, "verify")["ok"] is True

