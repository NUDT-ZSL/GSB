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
SAMPLES = ROOT / "samples"
REGISTRY = SAMPLES / "registry.json"
CLOCKS = SAMPLES / "clocks.json"
SHARDS = sorted((SAMPLES / "shards").glob("*.json"))
MONOLITH = SAMPLES / "streams" / "monolithic.json"
INVALID = SAMPLES / "invalid"


@pytest.fixture
def sample_paths() -> dict[str, Any]:
    return {
        "root": ROOT,
        "registry": REGISTRY,
        "clocks": CLOCKS,
        "shards": SHARDS,
        "monolith": MONOLITH,
        "invalid": INVALID,
    }


def run_cli(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "probe_ledger", *args],
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def load_all(workspace: Path, *, reverse: bool = False) -> subprocess.CompletedProcess[str]:
    files = list(reversed(SHARDS)) if reverse else list(SHARDS)
    args = [
        "load",
        "--workspace",
        str(workspace),
        "--registry",
        str(REGISTRY),
        "--clocks",
        str(CLOCKS),
    ]
    for path in files:
        args.extend(["--shard", str(path)])
    return run_cli(*args)


def reconcile(workspace: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return run_cli(
        "reconcile",
        "--workspace",
        str(workspace),
        *extra,
    )


def ledger(workspace: Path) -> dict[str, Any]:
    return json.loads((workspace / "ledger.json").read_text(encoding="utf-8"))


def ledger_bytes(workspace: Path) -> bytes:
    return (workspace / "ledger.json").read_bytes()


def status(workspace: Path, *extra: str) -> dict[str, Any]:
    result = run_cli("status", "--workspace", str(workspace), *extra)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)
