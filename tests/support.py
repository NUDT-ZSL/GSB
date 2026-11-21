from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = ROOT / "tests" / ".tmp"
SAMPLES = ROOT / "samples"
SCENARIO = SAMPLES / "scenario.json"
NOTSTARTED = SAMPLES / "notstarted.json"


def fresh_dir(name: str) -> Path:
    path = TMP_ROOT / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_cli(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "lockledger",
        "--workspace",
        str(ROOT),
        *args,
    ]
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def cli_json(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"invalid JSON output: {exc}\nstdout={completed.stdout}\n"
            f"stderr={completed.stderr}"
        ) from exc


def wait_signature(status: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (
            tx["tx_key"],
            tx["status"],
            tx["reason"],
            [
                (
                    wait["registration_seq"],
                    wait["raw_wait_ms"],
                    wait["fixed_units"],
                    wait["exact_units"],
                    wait["discarded_remainder"],
                    wait["rounding_direction"],
                    wait["outcome"],
                )
                for wait in tx["wait_records"]
            ],
        )
        for tx in status["transactions"]
    ]

