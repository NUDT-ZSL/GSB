from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def _run_cli(
    arguments: list[str],
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tailsample", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONHASHSEED": "0", **(env or {})},
        check=False,
    )


@pytest.fixture
def run_cli() -> Callable[..., subprocess.CompletedProcess[str]]:
    return _run_cli


@pytest.fixture
def loaded_db(tmp_path: Path, run_cli: Callable[..., Any]) -> Path:
    db = tmp_path / "tailsample.db"
    completed = run_cli(
        [
            "load",
            "--db",
            str(db),
            "--topology",
            str(SAMPLES / "topology.json"),
            "--policies",
            str(SAMPLES / "policies.json"),
            "--events",
            str(SAMPLES / "events.json"),
            "--window-ms",
            "900",
            "--quota-per-second",
            "2",
            "--clock-tolerance-ms",
            "10",
            "--reset",
        ]
    )
    assert completed.returncode == 0, completed.stderr
    return db


@pytest.fixture
def decided_db(loaded_db: Path, run_cli: Callable[..., Any]) -> Path:
    completed = run_cli(
        ["sample", "--db", str(loaded_db), "--advance-to", "3500"]
    )
    assert completed.returncode == 0, completed.stderr
    return loaded_db


def output_json(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(completed.stdout)

