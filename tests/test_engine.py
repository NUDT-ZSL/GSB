from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from gateway.engine import GatewayEngine
from gateway.errors import (
    E_BACKLOG_LIMIT,
    E_CLOCK_REGRESSION,
    E_DELIVERY_NOT_FOUND,
    E_SUBSCRIBER_MISMATCH,
    E_SUBSCRIBER_NOT_FOUND,
    GatewayError,
)
from gateway.ledger import logical_state_fingerprint
from gateway.validation import validate_sample

from .conftest import load_sample_docs, one_partition_docs


def _load_full(tmp_path: Path) -> GatewayEngine:
    engine = GatewayEngine(tmp_path / "gateway.db")
    engine.load_documents(*load_sample_docs())
    return engine


def test_offsets_are_contiguous_and_duplicate_load_is_idempotent(
    tmp_path: Path,
) -> None:
    engine = GatewayEngine(tmp_path / "gateway.db")
    first = engine.load_documents(*load_sample_docs())
    fingerprint = engine.status()["ledger_fingerprint"]
    second = engine.load_documents(*load_sample_docs())

    assert first["ingested_events"] == 24
    assert second["ingested_events"] == 0
    assert second["idempotent_events"] == 24
    assert engine.status()["ledger_fingerprint"] == fingerprint
    for partition in engine.status()["partitions"]:
        assert partition["offset_watermark"] >= 2
    assert engine.verify()["ok"]


def test_produce_is_transactional_and_batch_idempotent(tmp_path: Path) -> None:
    engine = _load_full(tmp_path)
    before = engine.status()
    produced = engine.produce(
        {"events": json.loads(json.dumps(load_sample_docs()[1]))["events"]}
    )
    after = engine.status()
    assert produced["appended"] == 0
    assert produced["idempotent"] == 24
    assert after["ledger_fingerprint"] == before["ledger_fingerprint"]
    assert engine.verify()["ok"]


def test_delivery_retry_dead_letter_and_round_idempotency(
    tmp_path: Path,
) -> None:
    engine = _load_full(tmp_path)
    result = engine.deliver("sub-orders-a", round_id="round-a-1")
    assert result["summary"]["delivery_attempts"] > 0
    assert result["summary"]["failed_attempts"] > 0
    assert result["summary"]["dead_letters"] == 1
    assert any(
        partition["mode_decision"]["actual_mode"] == "segment"
        for partition in result["partitions"]
        if partition["started"]
    )

    status_after = engine.status()
    delivered_fingerprint = status_after["ledger_fingerprint"]
    dead_entries = []
    for partition in status_after["partitions"]:
        for subscriber in partition["subscribers"].values():
            dead_entries.extend(
                offset
                for offset in subscriber["dead_letter_offsets"]
            )
    assert dead_entries

    replay = engine.deliver("sub-orders-a", round_id="round-a-1")
    assert replay["idempotent_replay"] is True
    assert engine.status()["ledger_fingerprint"] == delivered_fingerprint
    assert engine.verify()["ok"]


def test_confirmation_idempotency_late_and_cross_subscriber(
    tmp_path: Path,
) -> None:
    engine = _load_full(tmp_path)
    engine.deliver("sub-orders-a", round_id="confirm-round")

    connection = sqlite3.connect(engine.db_path)
    connection.row_factory = sqlite3.Row
    acked = connection.execute(
        """
        SELECT delivery_id FROM deliveries
        WHERE subscriber_id = 'sub-orders-a' AND status = 'acked'
        ORDER BY delivery_id LIMIT 1
        """
    ).fetchone()
    dead = connection.execute(
        """
        SELECT delivery_id FROM deliveries
        WHERE subscriber_id = 'sub-orders-a' AND status = 'dead'
        ORDER BY delivery_id LIMIT 1
        """
    ).fetchone()
    connection.close()
    assert acked is not None
    assert dead is not None

    duplicate = engine.confirm(
        acked["delivery_id"],
        subscriber_id="sub-orders-a",
    )
    assert duplicate["duplicate_confirmation"] is True
    assert duplicate["applied"] is False

    late = engine.confirm(
        dead["delivery_id"],
        subscriber_id="sub-orders-a",
    )
    assert late["late_confirmation"] is True
    assert late["applied"] is False

    before = engine.status()["ledger_fingerprint"]
    with pytest.raises(GatewayError) as error:
        engine.confirm(
            acked["delivery_id"],
            subscriber_id="sub-orders-b",
        )
    assert error.value.code == E_SUBSCRIBER_MISMATCH
    assert engine.status()["ledger_fingerprint"] == before

    with pytest.raises(GatewayError) as error:
        engine.confirm(
            "missing-delivery",
            subscriber_id="sub-orders-a",
        )
    assert error.value.code == E_DELIVERY_NOT_FOUND


def test_business_error_codes_are_isolated(tmp_path: Path) -> None:
    engine = _load_full(tmp_path)
    with pytest.raises(GatewayError) as error:
        engine.deliver("unknown-subscriber")
    assert error.value.code == E_SUBSCRIBER_NOT_FOUND

    topics, events, subscriptions, config = one_partition_docs(
        event_count=1,
        backlog_limit=1,
    )
    small = GatewayEngine(tmp_path / "small.db")
    small.load_documents(topics, events, subscriptions, config)
    extra = {
        "events": [
            {
                "event_seq": 99,
                "event_id": "extra",
                "topic": "only",
                "partition_key": "same-key",
                "payload_digest": "sha256:extra",
                "injected_clock_ms": 100,
                "expected_delivery_generation": 1,
            }
        ]
    }
    before = small.status()["ledger_fingerprint"]
    with pytest.raises(GatewayError) as error:
        small.produce(extra)
    assert error.value.code == E_BACKLOG_LIMIT
    assert small.status()["ledger_fingerprint"] == before

    regression_topics, regression_events, regression_subs, regression_config = (
        one_partition_docs(event_count=2, backlog_limit=2)
    )
    regression_engine = GatewayEngine(tmp_path / "regression.db")
    regression_engine.load_documents(
        regression_topics,
        regression_events,
        regression_subs,
        regression_config,
    )
    regression_before = regression_engine.status()["ledger_fingerprint"]
    regressed = {
        "events": [
            {
                "event_seq": 100,
                "event_id": "regressed",
                "topic": "only",
                "partition_key": "same-key",
                "payload_digest": "sha256:regressed",
                "injected_clock_ms": 0,
                "expected_delivery_generation": 1,
            }
        ]
    }
    with pytest.raises(GatewayError) as error:
        regression_engine.produce(regressed)
    assert error.value.code == E_CLOCK_REGRESSION
    assert (
        regression_engine.status()["ledger_fingerprint"]
        == regression_before
    )


def test_budget_abandonment_has_explicit_outcomes(tmp_path: Path) -> None:
    docs = list(load_sample_docs())
    docs[3] = json.loads(
        (Path(__file__).resolve().parents[1] / "samples" / "config_tight.json")
        .read_text(encoding="utf-8")
    )
    engine = GatewayEngine(tmp_path / "tight.db")
    engine.load_documents(*docs)
    result = engine.deliver("sub-orders-a", round_id="tight-round")

    assert result["summary"]["started"] == 0
    assert result["summary"]["abandoned_budget"] == 4
    assert result["summary"]["delivery_attempts"] == 0
    assert all(
        partition["outcome"] == "ABANDONED_BUDGET"
        and "remaining_budget_ms" in partition["reason"]
        for partition in result["partitions"]
    )
    assert engine.verify()["ok"]


def test_missing_delivery_event_reports_business_error(tmp_path: Path) -> None:
    topics, events, subscriptions, config = one_partition_docs()
    engine = GatewayEngine(tmp_path / "missing-event.db")
    engine.load_documents(topics, events, subscriptions, config)
    connection = engine.storage.connect()
    try:
        with engine.storage.transaction(connection):
            state = engine.storage.require_state(connection)
            partition = state["partitions"]["only/0"]
            del partition["subscribers"]["sub-only"]["deliveries"]["0"]
            engine.storage.commit_state(connection, state)
    finally:
        connection.close()

    result = engine.deliver("sub-only", round_id="missing-event-round")
    failed = [
        partition
        for partition in result["partitions"]
        if partition["outcome"] == "STARTED_FAILED"
    ]
    assert failed
    assert failed[0]["error"]["code"] == "E_EVENT_NOT_FOUND"


def test_adaptive_lock_decision_contains_rejection_and_consistency(
    tmp_path: Path,
) -> None:
    sample = validate_sample(*load_sample_docs())
    engine = GatewayEngine(tmp_path / "mode.db")
    engine.load_documents(*load_sample_docs())
    result = engine.deliver("sub-orders-a", round_id="mode-round")
    decisions = [
        partition["mode_decision"]
        for partition in result["partitions"]
        if partition["mode_decision"] is not None
    ]
    assert decisions
    decision = decisions[0]
    assert decision["actual_mode"] in {"partition", "segment"}
    assert decision["rejected_mode"] != decision["actual_mode"]
    assert decision["trigger"]
    assert decision["reason"]
    assert decision["consistency_before"]["contiguous_offsets"] is True
    assert decision["consistency_after"]["max_inflight_per_partition"] == 1
