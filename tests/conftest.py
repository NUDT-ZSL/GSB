from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shardroute.sample import load_sample
from shardroute.state import StateStore


SAMPLE_PATH = ROOT / "samples" / "sample.json"
INJECTIONS_PATH = ROOT / "samples" / "injections.json"


@pytest.fixture(scope="session")
def sample():
    return load_sample(SAMPLE_PATH)


@pytest.fixture
def make_state(tmp_path: Path):
    counter = 0

    def factory(clock_ms: int = 1000, state_name: str = "state.db") -> StateStore:
        nonlocal counter
        counter += 1
        path = tmp_path / f"{counter}-{state_name}"
        StateStore.initialize(path, load_sample(SAMPLE_PATH), clock_ms)
        return StateStore(path)

    return factory


@pytest.fixture
def state(make_state) -> StateStore:
    return make_state()


def run_cli(*args: str, check: bool = True, state_path: str | Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "shardroute", *args]
    if state_path is not None:
        command.extend(["--state", str(state_path)])
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def cli_json(*args: str, state_path: str | Path | None = None, check: bool = True) -> dict[str, Any]:
    completed = run_cli(*args, check=check, state_path=state_path)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"CLI did not emit JSON: {' '.join(args)}\n{completed.stdout}\n{completed.stderr}"
        ) from exc


def read_sample_raw() -> dict[str, Any]:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))

