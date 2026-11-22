from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from h2diag.canonical import digest_without_fields

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


@pytest.fixture
def main_scenario() -> dict[str, Any]:
    return json.loads((SAMPLES / "scenario.json").read_text(encoding="utf-8"))


@pytest.fixture
def end_stream_scenario() -> dict[str, Any]:
    return json.loads((SAMPLES / "end_stream_scenario.json").read_text(encoding="utf-8"))


@pytest.fixture
def main_patch() -> dict[str, Any]:
    return json.loads(
        (SAMPLES / "patches" / "settings-weight-v2.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def end_stream_patch() -> dict[str, Any]:
    return json.loads(
        (SAMPLES / "patches" / "end-stream-v2.json").read_text(encoding="utf-8")
    )


def seal_patch(body: dict[str, Any]) -> dict[str, Any]:
    patch = copy.deepcopy(body)
    patch["content_digest"] = digest_without_fields(patch, ["content_digest"])
    return patch


def run_cli(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged["PYTHONPATH"] = str(ROOT)
    if env:
        merged.update(env)
    result = subprocess.run(
        [sys.executable, "-m", "h2diag", *args],
        cwd=ROOT,
        env=merged,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {result.args}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result
