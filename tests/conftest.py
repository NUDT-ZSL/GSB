from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "default"


@pytest.fixture
def sample_dir() -> Path:
    return SAMPLE


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=True),
        encoding="utf-8",
    )
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def cli(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "columnar_engine.cli", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=60,
    )


def query(db_path: Path, sql: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def values_by_column(wire: dict[str, Any], column: str) -> list[Any]:
    return wire["columns"][column]["values"]

