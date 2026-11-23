from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from gateway.api import app

from .conftest import load_sample_docs


ROOT = Path(__file__).resolve().parents[1]


def test_cli_status_is_stable_json(tmp_path: Path) -> None:
    db = tmp_path / "cli.db"
    common = [
        sys.executable,
        "-m",
        "gateway.cli",
        "--db",
        str(db),
    ]
    load = subprocess.run(
        common
        + [
            "load",
            "--topics",
            str(ROOT / "samples" / "topics.json"),
            "--events",
            str(ROOT / "samples" / "events.json"),
            "--subscriptions",
            str(ROOT / "samples" / "subscriptions.json"),
            "--config",
            str(ROOT / "samples" / "config.json"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert load.returncode == 0, load.stdout + load.stderr
    first = subprocess.run(
        common + ["status"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    second = subprocess.run(
        common + ["status"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert first.returncode == 0
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout)
    assert [partition["partition_id"] for partition in payload["partitions"]] == sorted(
        partition["partition_id"] for partition in payload["partitions"]
    )


def test_fastapi_control_plane(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "api.db"
    monkeypatch.setenv("GATEWAY_DB", str(db))
    topics, events, subscriptions, config = load_sample_docs()
    with TestClient(app) as client:
        loaded = client.post(
            "/load",
            json={
                "topics": topics,
                "events": events,
                "subscriptions": subscriptions,
                "config": config,
            },
        )
        assert loaded.status_code == 200
        assert loaded.json()["ingested_events"] == 24
        delivered = client.post(
            "/deliver",
            json={
                "subscriber_id": "sub-orders-a",
                "round_id": "api-round",
            },
        )
        assert delivered.status_code == 200
        assert delivered.json()["summary"]["delivery_attempts"] > 0
        status = client.get("/status")
        assert status.status_code == 200
        assert status.json()["ok"] is True
        verify = client.get("/verify")
        assert verify.status_code == 200
        assert verify.json()["ok"] is True
