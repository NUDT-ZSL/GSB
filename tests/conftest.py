from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = ROOT / "samples" / "deterministic"
NODES = SAMPLE_DIR / "nodes.json"
PODS = SAMPLE_DIR / "pods.json"
CONFIG = SAMPLE_DIR / "schedule_config.json"
INJECTIONS = SAMPLE_DIR / "injections.json"


def cli(*args: str, cwd: Path | None = None):
    import subprocess

    return subprocess.run(
        [sys.executable, "-m", "kubesim", *args],
        cwd=str(cwd or ROOT),
        text=True,
        capture_output=True,
        check=False,
    )


def load_sample(db_path: Path) -> dict:
    result = cli(
        "load",
        "--db",
        str(db_path),
        "--nodes",
        str(NODES),
        "--pods",
        str(PODS),
        "--config",
        str(CONFIG),
        "--injections",
        str(INJECTIONS),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def sample_documents() -> tuple[dict, dict, dict]:
    return read_json(NODES), read_json(PODS), read_json(CONFIG)
