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


def sample(name: str) -> Any:
    with open(SAMPLES / name, "r", encoding="utf-8") as handle:
        return json.load(handle)


def cli(*args: str, env: dict[str, str] | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    result = subprocess.run(
        [sys.executable, "-m", "batchdiag", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=merged,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"CLI failed ({result.returncode}): {result.stdout}\n{result.stderr}"
        )
    return result


def cli_json(*args: str, env: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    result = cli(*args, env=env)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"CLI did not emit JSON: stdout={result.stdout!r} stderr={result.stderr!r}"
        ) from exc
    return result.returncode, payload


def load_sample(
    db: Path,
    *,
    requests: str = "requests.json",
    force: bool = False,
) -> tuple[int, dict[str, Any]]:
    args = [
        "load",
        "--model",
        str(SAMPLES / "model.json"),
        "--replicas",
        str(SAMPLES / "replicas.json"),
        "--requests",
        str(SAMPLES / requests),
        "--config",
        str(SAMPLES / "precision.json"),
        "--db",
        str(db),
    ]
    if force:
        args.append("--force")
    return cli_json(*args)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "state.sqlite"
