from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from gateway.engine import GatewayEngine
from gateway.validation import validate_sample

from .conftest import load_sample_docs, read_sample


ROOT = Path(__file__).resolve().parents[1]


def _produce(path: Path, events_path: Path, db: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gateway.cli",
            "--db",
            str(db),
            "produce",
            "--events",
            str(events_path),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_concurrent_processes_append_without_offset_gaps(
    tmp_path: Path,
) -> None:
    db = tmp_path / "multi.db"
    engine = GatewayEngine(db)
    topics, _, subscriptions, config = load_sample_docs()
    engine.load_documents(
        topics,
        {"events": []},
        subscriptions,
        config,
    )
    events = read_sample("events.json")["events"]
    sample = validate_sample(*load_sample_docs())
    routes = sample.event_routes()
    group_a = {"orders/0", "orders/2", "audit/0"}
    first = {
        "events": [
            event for event in events if routes[event["event_seq"]] in group_a
        ]
    }
    second = {
        "events": [
            event for event in events if routes[event["event_seq"]] not in group_a
        ]
    }
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text(json.dumps(first), encoding="utf-8")
    second_path.write_text(json.dumps(second), encoding="utf-8")

    process_a = _produce(first_path, first_path, db)
    process_b = _produce(second_path, second_path, db)
    stdout_a, stderr_a = process_a.communicate(timeout=30)
    stdout_b, stderr_b = process_b.communicate(timeout=30)
    assert process_a.returncode == 0, stdout_a + stderr_a
    assert process_b.returncode == 0, stdout_b + stderr_b
    result_a = json.loads(stdout_a)
    result_b = json.loads(stdout_b)
    assert result_a["appended"] + result_b["appended"] == 24
    assert result_a["idempotent"] + result_b["idempotent"] == 0
    assert engine.verify()["ok"]
    status = engine.status()
    assert sum(
        partition["offset_watermark"] for partition in status["partitions"]
    ) == 24


def test_concurrent_delivery_processes_respect_partition_gate(
    tmp_path: Path,
) -> None:
    db = tmp_path / "multi-deliver.db"
    engine = GatewayEngine(db)
    engine.load_documents(*load_sample_docs())

    commands = [
        [
            sys.executable,
            "-m",
            "gateway.cli",
            "--db",
            str(db),
            "deliver",
            "--subscriber",
            "sub-orders-a",
            "--round-id",
            "parallel-a",
        ],
        [
            sys.executable,
            "-m",
            "gateway.cli",
            "--db",
            str(db),
            "deliver",
            "--subscriber",
            "sub-audit",
            "--round-id",
            "parallel-audit",
        ],
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for command in commands
    ]
    outputs = [process.communicate(timeout=30) for process in processes]
    for process, (stdout, stderr) in zip(processes, outputs):
        assert process.returncode == 0, stdout + stderr
        assert json.loads(stdout)["ok"] is True
    verification = engine.verify()
    assert verification["ok"], verification["failures"]
    status = engine.status()
    assert status["global"]["inflight_count"] == 0
