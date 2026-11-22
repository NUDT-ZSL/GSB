from __future__ import annotations

import copy
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PATH = ROOT / "samples" / "adaptive_join_sample.json"


@pytest.fixture
def sample_dict() -> dict[str, Any]:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def clone_sample() -> Callable[[], dict[str, Any]]:
    def clone() -> dict[str, Any]:
        return copy.deepcopy(json.loads(SAMPLE_PATH.read_text(encoding="utf-8")))

    return clone


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def run_cli(*args: str, cwd: Path = ROOT, timeout: float = 90.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "stream_reopt", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )

