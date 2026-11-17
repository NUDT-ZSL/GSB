from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import generate_samples  # noqa: E402


def make_event(
    event_id: str,
    trace_id: str = "trace-a",
    actor: str = "alice",
    action: str = "read",
    object_name: str = "doc-1",
    ts: int = 1000,
    seq: int = 1,
    parent_event_id: str | None = None,
) -> dict[str, Any]:
    return generate_samples.event(
        event_id,
        trace_id,
        actor,
        action,
        object_name,
        ts,
        seq,
        parent_event_id,
    )


def write_jsonl(path: Path, values: list[dict[str, Any] | str]) -> None:
    generate_samples.write_jsonl(path, values)


def write_json(path: Path, value: Any) -> None:
    generate_samples.write_json(path, value)


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "audit_forge", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def parse_stdout(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def sample_dir(tmp_path: Path) -> Path:
    path = tmp_path / "samples"
    generate_samples.generate(path)
    return path

