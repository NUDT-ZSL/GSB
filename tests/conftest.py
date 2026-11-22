from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest

from blockstore.engine import SnapshotEngine
from blockstore.models import PlanBundle, load_bundle

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "samples"


@pytest.fixture
def sample_bundle() -> PlanBundle:
    return load_bundle(
        SAMPLES / "volume.json",
        SAMPLES / "snapshots.json",
        SAMPLES / "rollbacks.json",
    )


@pytest.fixture
def loaded_engine(tmp_path: Path, sample_bundle: PlanBundle) -> SnapshotEngine:
    engine = SnapshotEngine(tmp_path / "store")
    engine.load_plan(sample_bundle)
    return engine


def run_cli(
    root: Path,
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "blockstore",
        "--root",
        str(root),
        "--compact",
        *args,
    ]
    merged_env = os.environ.copy()
    merged_env.pop("BLOCKSTORE_CRASH_POINT", None)
    if env:
        merged_env.update(env)
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=merged_env,
        capture_output=True,
        text=True,
        check=check,
    )


def read_cli_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout)


def query_db(
    engine: SnapshotEngine,
    sql: str,
    params: Iterable[Any] = (),
) -> list[sqlite3.Row]:
    connection = sqlite3.connect(engine.db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, tuple(params)).fetchall()
    finally:
        connection.close()


def write_json(path: Path, value: Any) -> Path:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path
