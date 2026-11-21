from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def run_cli(
    workspace: Path,
    *arguments: str,
    env: Mapping[str, str] | None = None,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "wadiag",
            "--workspace",
            str(workspace),
            *arguments,
        ],
        cwd=ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def cli_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout)


def load_sample(workspace: Path) -> dict[str, Any]:
    result = run_cli(
        workspace,
        "load",
        "--trajectory",
        str(SAMPLES / "write_trajectory.json"),
        "--storage",
        str(SAMPLES / "storage_config.json"),
        "--diagnostic",
        str(SAMPLES / "diagnostic_config.json"),
        "--clock-table",
        str(SAMPLES / "clock_steps.json"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return cli_json(result)


@pytest.fixture
def loaded_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "state"
    load_sample(workspace)
    return workspace
