from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from eventbus.engine import EventBus
from eventbus.errors import BusinessError
from conftest import SAMPLES, drain_bridges, make_dataset


def _event_rows(bus: EventBus, channel: str) -> list[dict]:
    connection = sqlite3.connect(bus.db_path)
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM events WHERE channel=? ORDER BY offset", (channel,)
            )
        ]
    finally:
        connection.close()


def test_deterministic_partition_and_contiguous_batches(bus: EventBus) -> None:
    first = bus._partition("orders", 4, "order-1001")
    assert first == bus._partition("orders", 4, "order-1001")
    document = {
        "events": [
            {
                "sequence": 8 + index,
                "partition_key": f"batch-{index}",
                "logical_clock": 200 + index,
                "produced_at_ms": 1_700_000_010_000 + index,
                "fields": {
                    "order_id": f"O-{index}",
                    "region": "east",
                    "amount": 1,
                    "note": "batch",
                    "currency": "CNY",
                    "version_tag": "append",
                },
            }
            for index in range(5)
        ]
    }
    result = bus.publish_events("orders", document, max_batch=2, clock_ms=6000)
    assert result["appended"] == 5
    offsets = [item["offset"] for item in result["results"]]
    assert offsets == [7, 8, 9, 10, 11]
    connection = sqlite3.connect(bus.db_path)
    try:
        batch_counts = [
            row[0]
            for row in connection.execute(
                "SELECT event_count FROM batches WHERE channel='orders' "
                "AND batch_id LIKE 'orders:%' ORDER BY start_offset"
            )
        ]
    finally:
        connection.close()
    assert batch_counts == [2, 2, 1]


def test_repeated_event_sequence_is_idempotent_or_conflicts(bus: EventBus) -> None:
    event = {
        "sequence": 9,
        "partition_key": "idem",
        "logical_clock": 300,
        "produced_at_ms": 1_700_000_020_000,
        "fields": {
            "order_id": "O-IDEM",
            "region": "east",
            "amount": 5,
            "note": "",
            "currency": "CNY",
            "version_tag": "append",
        },
    }
    first = bus.publish_events("orders", {"events": [event]}, clock_ms=7000)
    second = bus.publish_events("orders", {"events": [event]}, clock_ms=7000)
    assert first["appended"] == 1
    assert second["appended"] == 0
    assert second["results"][0]["offset"] == first["results"][0]["offset"]
    changed = json.loads(json.dumps(event))
    changed["fields"]["amount"] = 6
    with pytest.raises(BusinessError) as captured:
        bus.publish_events("orders", {"events": [changed]}, clock_ms=7000)
    assert captured.value.code == "DATA_CONFLICT"


def test_bridge_filter_transform_and_replay_verify(bus: EventBus) -> None:
    drain_bridges(bus)
    status = bus.status()
    bridge = status["bridges"][0]
    assert bridge["next_offset"] == 7
    assert bridge["lag"] == 0
    audit_events = _event_rows(bus, "audit")
    assert len(audit_events) == 5
    delivered = audit_events[1:]
    assert [json.loads(row["fields_json"])["order_id"] for row in delivered] == [
        "O-1001",
        "O-1003",
        "O-1004",
        "O-1005",
    ]
    amounts = [json.loads(row["fields_json"])["amount"] for row in delivered]
    assert amounts == [11.55, 25.58, 16.5, 33.0]
    assert all(json.loads(row["fields_json"])["source"] == "bridge" for row in delivered)
    verification = bus.verify()
    assert verification["ok"], verification["errors"]
    assert verification["checks"]["bridges"]["orders-to-audit"]["checks"] == 7
    repeated = bus.report_bridge_position("orders-to-audit", 7)
    assert repeated["idempotent"] is True
    with pytest.raises(BusinessError) as captured:
        bus.report_bridge_position("orders-to-audit", 3)
    assert captured.value.code == "BRIDGE_POSITION_REWIND"


def test_generation_and_upstream_watermark_fail_distinctly(bus: EventBus) -> None:
    patch = {
        "bridges": [
            {
                "op": "update",
                "bridge": {
                    "id": "orders-to-audit",
                    "source_channel": "orders",
                    "target_channel": "audit",
                    "source_generation": 1,
                    "target_generation": 2,
                    "min_upstream_watermark": 0,
                    "filter": {},
                    "transforms": [],
                    "max_batch": 2,
                },
            }
        ]
    }
    bus.edit(patch, clock_ms=2000)
    with pytest.raises(BusinessError) as captured:
        bus.run_bridge("orders-to-audit")
    assert captured.value.code == "GENERATION_MISMATCH"
    patch["bridges"][0]["bridge"]["source_generation"] = 2
    patch["bridges"][0]["bridge"]["min_upstream_watermark"] = 99
    bus.edit(patch, clock_ms=3000)
    with pytest.raises(BusinessError) as captured:
        bus.run_bridge("orders-to-audit")
    assert captured.value.code == "BRIDGE_UPSTREAM_WATERMARK"


def test_consume_offset_is_bounded_idempotent_and_no_rewind(bus: EventBus) -> None:
    first = bus.consume(
        "orders-reader", to_offset=2, request_id="report-2", clock_ms=2000
    )
    assert first["offset"] == 2
    repeated = bus.consume(
        "orders-reader", to_offset=2, request_id="report-2", clock_ms=2000
    )
    assert repeated["idempotent"] is True
    advanced = bus.consume(
        "orders-reader", to_offset=4, request_id="report-4", clock_ms=3000
    )
    assert advanced["offset"] == 4
    stale = bus.consume(
        "orders-reader", to_offset=2, request_id="report-2-again", clock_ms=3000
    )
    assert stale["idempotent"] is True
    assert stale["offset"] == 4
    with pytest.raises(BusinessError) as captured:
        bus.consume("orders-reader", to_offset=3, request_id="stale-new", clock_ms=3000)
    assert captured.value.code == "SUBSCRIPTION_REWIND"
    with pytest.raises(BusinessError) as captured:
        bus.consume("orders-reader", to_offset=8, clock_ms=3000)
    assert captured.value.code == "SUBSCRIPTION_OFFSET_OUT_OF_RANGE"


def test_cleanup_has_physical_and_tombstone_paths_and_clock_guard(bus: EventBus) -> None:
    drain_bridges(bus)
    bus.consume("orders-reader", count=7, clock_ms=2000)
    report = bus.cleanup(clock_ms=4000, retention_bytes=1000)
    assert report["physical_removed"] == 6
    assert report["tombstones_marked"] == 1
    assert report["blocked_slowest"]["channel"] == "audit"
    assert report["bytes_after"] > report["retention_bytes"]
    assert bus.verify()["ok"]
    with pytest.raises(BusinessError) as captured:
        bus.cleanup(clock_ms=1)
    assert captured.value.code == "CLOCK_REWIND"
    latest = bus.status()["last_cleanup"]
    assert latest["physical_removed"] == 6


def test_edit_adds_updates_deletes_event_and_versions_config(bus: EventBus) -> None:
    patch = {
        "patch_id": "event-crud",
        "events": [
            {
                "op": "add",
                "channel": "orders",
                "sequence": 8,
                "partition_key": "crud",
                "logical_clock": 180,
                "produced_at_ms": 1_700_000_007_000,
                "fields": {
                    "order_id": "O-CRUD",
                    "region": "east",
                    "amount": 9,
                    "note": "created",
                    "currency": "CNY",
                    "version_tag": "edit",
                },
            }
        ],
    }
    added = bus.edit(patch, clock_ms=2000)
    assert added["config_version"] == 2
    update = {
        "patch_id": "event-update",
        "events": [
            {
                "op": "update",
                "channel": "orders",
                "sequence": 8,
                "logical_clock": 181,
                "fields": {
                    "order_id": "O-CRUD",
                    "region": "north",
                    "amount": 10,
                    "note": "updated",
                    "currency": "CNY",
                    "version_tag": "edit",
                },
            }
        ],
    }
    updated = bus.edit(update, clock_ms=3000)
    assert updated["config_version"] == 3
    delete = {
        "patch_id": "event-delete",
        "events": [{"op": "delete", "channel": "orders", "sequence": 8}],
    }
    deleted = bus.edit(delete, clock_ms=4000)
    assert deleted["config_version"] == 4
    row = _event_rows(bus, "orders")[-1]
    assert row["tombstone"] == 1
    assert bus.verify()["ok"]


def test_publish_version_switches_pointer_without_rewriting(bus: EventBus) -> None:
    bus.edit(
        {
            "subscriptions": [
                {
                    "op": "add",
                    "subscription": {
                        "name": "tail",
                        "channel": "orders",
                        "start_offset": 1,
                    },
                }
            ]
        },
        clock_ms=2000,
    )
    assert bus.status()["config_version"] == 2
    result = bus.publish_version(1, clock_ms=3000)
    assert result["config_version"] == 1
    assert bus.status()["config_version"] == 1


def test_verify_detects_corrupted_payload(bus: EventBus) -> None:
    connection = sqlite3.connect(bus.db_path)
    try:
        connection.execute(
            "UPDATE events SET fields_json='{\"tampered\":true}' "
            "WHERE channel='orders' AND sequence=1"
        )
        connection.commit()
    finally:
        connection.close()
    result = bus.verify()
    assert not result["ok"]
    assert any(item["code"] == "BRIDGE_DIGEST_MISMATCH" for item in result["errors"])
