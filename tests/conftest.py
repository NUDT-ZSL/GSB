from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from serverless_engine.engine import Engine


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


@pytest.fixture(scope="session", autouse=True)
def generate_samples() -> None:
    subprocess.run(
        [sys.executable, "scripts/generate_samples.py", "--out", str(SAMPLES)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def samples() -> Path:
    return SAMPLES


@pytest.fixture()
def loaded_engine(tmp_path: Path, samples: Path) -> Engine:
    engine = Engine(tmp_path / "state")
    engine.load(
        str(samples / "functions.json"),
        str(samples / "layers.json"),
        str(samples / "snapshots" / "v1.json"),
        str(samples / "snapshots" / "v2" / "index.json"),
    )
    return engine


def cli(state_dir: Path, *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "serverless_engine",
            *args,
            "--state-dir",
            str(state_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == expect, (
        f"exit={result.returncode} expected={expect}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    return result


def cli_json(state_dir: Path, *args: str, expect: int = 0) -> dict:
    result = cli(state_dir, *args, expect=expect)
    return json.loads(result.stdout)


def db_rows(db_path: Path, query: str, params: tuple = ()) -> list[sqlite3.Row]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()
