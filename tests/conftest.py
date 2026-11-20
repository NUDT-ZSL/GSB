from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from graphstore.model import ClockConfig
from graphstore.package import load_package
from graphstore.storage import GraphStore


@pytest.fixture
def workspace_tmp() -> Path:
    path = ROOT / "data" / "pytest" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def cli_path(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


@pytest.fixture
def clock() -> ClockConfig:
    return ClockConfig(
        anchor_ms=1_710_000_000_000,
        step_ms=250,
        max_rollback_ms=1_000,
        v1_scale_numerator=1_000,
        v1_scale_denominator=1,
        v1_rounding="floor",
    )


@pytest.fixture
def base_package(clock: ClockConfig):
    return load_package(ROOT / "samples/packages/base/manifest.json", clock)


@pytest.fixture
def loaded_store(tmp_path: Path, clock: ClockConfig, base_package):
    store = GraphStore(tmp_path / "graphstore.sqlite3", clock)
    store.replace(base_package)
    return store


def cli_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "graphstore.cli", *args]


def run_cli(
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    merged_env["PYTHONPATH"] = str(ROOT)
    if env:
        merged_env.update(env)
    return subprocess.run(
        cli_command(*args),
        cwd=ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
        check=check,
    )


def popen_cli(*args: str, env: dict[str, str] | None = None):
    merged_env = os.environ.copy()
    merged_env["PYTHONPATH"] = str(ROOT)
    if env:
        merged_env.update(env)
    return subprocess.Popen(
        cli_command(*args),
        cwd=ROOT,
        env=merged_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

