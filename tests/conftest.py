from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
NORMAL = SAMPLES / "normal"
BUDGET = SAMPLES / "budget"
ABNORMAL = SAMPLES / "abnormal"


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture(scope="session", autouse=True)
def ensure_samples() -> Path:
    if not (NORMAL / "policy.json").is_file():
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "generate_samples.py")],
            cwd=ROOT,
            check=True,
        )
    return SAMPLES


def run_cli(
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged["PYTHONPATH"] = str(ROOT)
    if env:
        merged.update(env)
    result = subprocess.run(
        [sys.executable, "-m", "walrec", *args],
        cwd=ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"CLI failed with {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def json_stdout(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout)
