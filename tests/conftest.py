from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def demo_path(root: Path) -> Path:
    return root / "samples" / "reopt_demo.json"


@pytest.fixture(scope="session")
def budget_path(root: Path) -> Path:
    return root / "samples" / "budget_exhausted.json"


@pytest.fixture(scope="session")
def precision_path(root: Path) -> Path:
    return root / "samples" / "precision_sensitive.json"


def run_cli(args: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stream_reopt", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def cli_json(args: list[str], cwd: Path = ROOT) -> tuple[int, dict[str, Any], subprocess.CompletedProcess[str]]:
    completed = run_cli(args, cwd=cwd)
    payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
    return completed.returncode, payload, completed
