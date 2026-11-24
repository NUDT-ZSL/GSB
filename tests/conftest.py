from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import pytest

from bsl_workbench.generator import generate_tree


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def sample_root(tmp_path: Path) -> Path:
    root = tmp_path / "samples"
    generate_tree(root)
    return root


@pytest.fixture()
def manifest(sample_root: Path) -> dict:
    import json

    return json.loads((sample_root / "manifest.json").read_text(encoding="utf-8"))


def run_cli(args: Sequence[object], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [sys.executable, "-m", "bsl_workbench.cli", *[str(arg) for arg in args]],
        cwd=str(cwd or ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


@pytest.fixture()
def cli_runner():
    return run_cli
