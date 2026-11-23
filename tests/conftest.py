from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from eventbus.engine import EventBus
from eventbus.validation import load_dataset

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def make_dataset(**overrides: Any):
    return load_dataset(
        overrides.get("v1", SAMPLES / "v1" / "events.json"),
        overrides.get("shards", SAMPLES / "v2" / "shards"),
        overrides.get("metadata", SAMPLES / "v2" / "metadata.sqlite"),
        retention_bytes=overrides.get("retention_bytes"),
        clock_tolerance_ms=overrides.get("clock_tolerance_ms"),
    )


@pytest.fixture
def bus(tmp_path: Path) -> EventBus:
    instance = EventBus(tmp_path / "eventbus.sqlite")
    instance.load(make_dataset(), clock_ms=1000)
    return instance


def drain_bridges(instance: EventBus) -> None:
    for _ in range(100):
        status = instance.status()
        if all(item["lag"] == 0 for item in status["bridges"]):
            return
        instance.run_bridge(status["bridges"][0]["id"], clock_ms=2000)
    raise AssertionError("bridges did not drain")


def run_cli(
    db_path: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "eventbus", "--db", str(db_path), *arguments]
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed: {command}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def parse_cli(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout)
