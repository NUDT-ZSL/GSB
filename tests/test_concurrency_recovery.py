from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from eventbus.engine import EventBus
from conftest import ROOT, SAMPLES, drain_bridges, make_dataset, parse_cli, run_cli


def _fresh_db(path: Path) -> EventBus:
    bus = EventBus(path)
    bus.load(make_dataset(), clock_ms=1000)
    return bus


def _state_snapshot(db_path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        events = [
            {
                "channel": row["channel"],
                "offset": row["offset"],
                "sequence": row["sequence"],
                "partition": row["partition"],
                "logical_clock": row["logical_clock"],
                "produced_at_ms": row["produced_at_ms"],
                "fields": row["fields_json"],
                "digest": row["payload_sha256"],
                "tombstone": row["tombstone"],
                "compacted": row["compacted"],
                "source_version": row["source_version"],
            }
            for row in connection.execute(
                "SELECT * FROM events ORDER BY channel, offset"
            )
        ]
        bridges = [
            {
                "bridge_id": row["bridge_id"],
                "next_offset": row["next_offset"],
                "last_digest": row["last_digest"],
                "config_version": row["config_version"],
            }
            for row in connection.execute(
                "SELECT * FROM bridge_positions ORDER BY bridge_id"
            )
        ]
        subscriptions = [
            {
                "name": row["name"],
                "offset": row["offset"],
                "active": row["active"],
            }
            for row in connection.execute("SELECT * FROM subscriptions ORDER BY name")
        ]
        deliveries = [
            {
                "bridge": row["bridge_id"],
                "source_offset": row["source_offset"],
                "target_channel": row["target_channel"],
                "target_offset": row["target_offset"],
                "digest": row["transform_digest"],
            }
            for row in connection.execute(
                "SELECT * FROM bridge_deliveries "
                "ORDER BY bridge_id, source_offset"
            )
        ]
        checks = [
            {
                "bridge": row["bridge_id"],
                "offset": row["source_offset"],
                "matched": row["matched"],
                "source_digest": row["source_payload_sha256"],
                "transform_digest": row["transform_digest"],
            }
            for row in connection.execute(
                "SELECT * FROM bridge_checks ORDER BY bridge_id, source_offset"
            )
        ]
        channels = [
            {
                "name": row["name"],
                "low": row["low_watermark"],
                "high": row["high_watermark"],
                "generation": row["generation"],
            }
            for row in connection.execute("SELECT * FROM channels ORDER BY name")
        ]
        config_version = connection.execute(
            "SELECT version FROM config_pointer WHERE id=1"
        ).fetchone()[0]
        return {
            "events": events,
            "bridges": bridges,
            "subscriptions": subscriptions,
            "deliveries": deliveries,
            "checks": checks,
            "channels": channels,
            "config_version": config_version,
        }
    finally:
        connection.close()


def _baseline_snapshot(tmp_path: Path) -> dict[str, Any]:
    bus = _fresh_db(tmp_path / "baseline.sqlite")
    drain_bridges(bus)
    return _state_snapshot(bus.db_path)


@pytest.mark.parametrize(
    "point",
    ["append_before_commit", "append_after_commit"],
)
def test_append_crash_recovery_matches_uninterrupted(
    tmp_path: Path, point: str
) -> None:
    db = tmp_path / f"append-{point}.sqlite"
    _fresh_db(db)
    baseline = _fresh_db(tmp_path / f"baseline-{point}.sqlite")
    baseline.publish_events(
        "orders",
        json.loads((SAMPLES / "append-events.json").read_text(encoding="utf-8")),
        clock_ms=6000,
    )
    expected = _state_snapshot(baseline.db_path)
    env = dict(os.environ)
    env["EVENTBUS_FAULT_POINT"] = point
    crashed = run_cli(
        db,
        "publish",
        "--channel",
        "orders",
        "--events",
        str(SAMPLES / "append-events.json"),
        "--clock-ms",
        "6000",
        env=env,
    )
    assert crashed.returncode == 86
    connection = sqlite3.connect(db)
    try:
        pending = connection.execute(
            "SELECT COUNT(*) FROM operation_intents WHERE status='pending'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert pending == (1 if point == "append_before_commit" else 0)
    resumed = parse_cli(run_cli(db, "resume", check=True))
    assert resumed["ok"]
    assert _state_snapshot(db) == expected


@pytest.mark.parametrize(
    "point",
    ["bridge_before_commit", "bridge_after_commit"],
)
def test_bridge_crash_recovery_matches_uninterrupted(
    tmp_path: Path, point: str
) -> None:
    expected = _baseline_snapshot(tmp_path)
    db = tmp_path / f"bridge-{point}.sqlite"
    _fresh_db(db)
    env = dict(os.environ)
    env["EVENTBUS_FAULT_POINT"] = point
    crashed = run_cli(db, "bridge", "orders-to-audit", env=env)
    assert crashed.returncode == 86
    connection = sqlite3.connect(db)
    try:
        statuses = [
            row[0]
            for row in connection.execute(
                "SELECT status FROM bridge_runs ORDER BY id"
            )
        ]
    finally:
        connection.close()
    if point == "bridge_before_commit":
        assert statuses == ["running"]
    else:
        assert statuses == ["completed"]
    resumed = parse_cli(run_cli(db, "resume", check=True))
    assert resumed["ok"]
    assert _state_snapshot(db) == expected


@pytest.mark.parametrize(
    "point",
    ["consume_before_commit", "consume_after_commit", "cleanup_before_commit", "cleanup_after_commit"],
)
def test_offset_and_cleanup_crash_recovery(tmp_path: Path, point: str) -> None:
    db = tmp_path / f"{point}.sqlite"
    bus = _fresh_db(db)
    drain_bridges(bus)
    if point.startswith("cleanup"):
        bus.consume("orders-reader", count=7, clock_ms=2000)
        args = ("cleanup", "--clock-ms", "4000", "--retention-bytes", "1000")
    else:
        args = (
            "consume",
            "--subscription",
            "orders-reader",
            "--count",
            "7",
            "--clock-ms",
            "2000",
        )
    env = dict(os.environ)
    env["EVENTBUS_FAULT_POINT"] = point
    crashed = run_cli(db, *args, env=env)
    assert crashed.returncode == 86
    parse_cli(run_cli(db, "resume", check=True))
    assert bus.verify()["ok"]
    state = bus.status()
    assert state["subscriptions"][1]["offset"] == 7
    if point.startswith("cleanup"):
        assert state["last_cleanup"]["physical_removed"] == 6
    else:
        assert state["last_cleanup"] is None


def _write_events(path: Path, start: int, count: int) -> None:
    document = {
        "events": [
            {
                "sequence": start + index,
                "partition_key": f"concurrent-{start + index}",
                "logical_clock": 300 + start + index,
                "produced_at_ms": 1_700_000_100_000 + start + index,
                "fields": {
                    "order_id": f"O-{start + index}",
                    "region": "east",
                    "amount": index + 1,
                    "note": "concurrent",
                    "currency": "CNY",
                    "version_tag": "concurrent",
                },
            }
            for index in range(count)
        ]
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def test_concurrent_producers_have_unique_contiguous_offsets(tmp_path: Path) -> None:
    db = tmp_path / "producers.sqlite"
    _fresh_db(db)
    processes = []
    for start in range(8, 16):
        event_file = tmp_path / f"events-{start}.json"
        _write_events(event_file, start, 1)
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "eventbus",
                    "--db",
                    str(db),
                    "publish",
                    "--channel",
                    "orders",
                    "--events",
                    str(event_file),
                    "--clock-ms",
                    "6000",
                ],
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(os.environ),
            )
        )
    outputs = [(process.wait(), process.stdout.read()) for process in processes]
    assert all(code == 0 for code, _output in outputs), outputs
    connection = sqlite3.connect(db)
    try:
        offsets = [
            row[0]
            for row in connection.execute(
                "SELECT offset FROM events WHERE channel='orders' ORDER BY offset"
            )
        ]
    finally:
        connection.close()
    assert offsets == list(range(15))
    assert EventBus(db).verify()["ok"]


def test_concurrent_same_subscription_report_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "consume.sqlite"
    _fresh_db(db)
    processes = []
    for index in range(6):
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "eventbus",
                    "--db",
                    str(db),
                    "consume",
                    "--subscription",
                    "orders-reader",
                    "--to-offset",
                    "2",
                    "--request-id",
                    f"report-{index}",
                    "--clock-ms",
                    "2000",
                ],
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(os.environ),
            )
        )
    outputs = [(process.wait(), process.stdout.read()) for process in processes]
    assert all(code == 0 for code, _output in outputs), outputs
    connection = sqlite3.connect(db)
    try:
        offset = connection.execute(
            "SELECT offset FROM subscriptions WHERE name='orders-reader'"
        ).fetchone()[0]
        commits = connection.execute(
            "SELECT COUNT(*) FROM subscription_commits "
            "WHERE subscription_name='orders-reader' AND to_offset=2"
        ).fetchone()[0]
    finally:
        connection.close()
    assert offset == 2
    assert commits == 1


def test_config_change_during_bridge_is_rejected(tmp_path: Path) -> None:
    db = tmp_path / "config-change.sqlite"
    bus = _fresh_db(db)
    env = dict(os.environ)
    env["EVENTBUS_TEST_BRIDGE_HOLD_MS"] = "1200"
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "eventbus",
            "--db",
            str(db),
            "bridge",
            "orders-to-audit",
        ],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        connection = sqlite3.connect(db)
        try:
            running = connection.execute(
                "SELECT COUNT(*) FROM bridge_runs WHERE status='running'"
            ).fetchone()[0]
        finally:
            connection.close()
        if running:
            break
        time.sleep(0.02)
    patch = json.loads(
        (SAMPLES / "patches" / "toggle-audit-tag.json").read_text(encoding="utf-8")
    )
    bus.edit(patch, clock_ms=2000)
    code = child.wait(timeout=10)
    output = json.loads(child.stdout.read())
    assert code == 2
    assert output["error"]["code"] == "CONFIG_CHANGED"
    status = bus.status()
    assert status["bridges"][0]["next_offset"] == 0
    connection = sqlite3.connect(db)
    try:
        deliveries = connection.execute(
            "SELECT COUNT(*) FROM bridge_deliveries"
        ).fetchone()[0]
    finally:
        connection.close()
    assert deliveries == 0


def test_subscription_consume_concurrently_never_overruns(tmp_path: Path) -> None:
    db = tmp_path / "consume-count.sqlite"
    _fresh_db(db)
    processes = []
    for _ in range(4):
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "eventbus",
                    "--db",
                    str(db),
                    "consume",
                    "--subscription",
                    "orders-reader",
                    "--count",
                    "2",
                    "--clock-ms",
                    "2000",
                ],
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(os.environ),
            )
        )
    for process in processes:
        process.wait()
    connection = sqlite3.connect(db)
    try:
        offset = connection.execute(
            "SELECT offset FROM subscriptions WHERE name='orders-reader'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert offset == 7
