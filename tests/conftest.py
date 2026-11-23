from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "slowquery.py"
SAMPLES = ROOT / "samples"


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("SLOWQUERY_FAILPOINT", None)
    env.pop("SLOWQUERY_TASK_DELAY_MS", None)
    if extra:
        env.update(extra)
    return env


def invoke(
    state: Path,
    args: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, str(CLI), "--state", str(state), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=clean_env(env),
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"command failed: {args}\nstdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def invoke_json(
    state: Path,
    args: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> dict:
    completed = invoke(state, args, check=check, env=env)
    return json.loads(completed.stdout)


@pytest.fixture()
def initialized(tmp_path: Path) -> Path:
    state = tmp_path / "state.db"
    invoke_json(state, ["load", "--reset"])
    return state


@pytest.fixture()
def sampled(initialized: Path) -> Path:
    invoke_json(initialized, ["sample"])
    return initialized


@pytest.fixture()
def planned(sampled: Path) -> Path:
    invoke_json(
        sampled,
        ["plan", "--max-workers", "2", "--memory-budget", "1024", "--memory-unit", "8"],
    )
    return sampled
