from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


@pytest.fixture(scope="session", autouse=True)
def deterministic_samples() -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_samples.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def sample_path() -> Path:
    return SAMPLES / "sample.json"


def run_cli(
    args: Iterable[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    effective_env = os.environ.copy()
    effective_env["PYTHONUTF8"] = "1"
    if env:
        effective_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "ic_ledger", *args],
        cwd=cwd,
        env=effective_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def run_cli_json(
    args: Iterable[str],
    *,
    env: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    result = run_cli([*args, "--compact"], env=env)
    payload = json.loads(result.stdout)
    return result.returncode, payload


def remove_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", ".lock"):
        Path(str(path) + suffix).unlink(missing_ok=True)

