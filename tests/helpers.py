from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def cli(
    state_dir: Path,
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "batchkernel.cli",
        "--state-dir",
        str(state_dir),
        *args,
    ]
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=process_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {command}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def load_sample(state_dir: Path, extra: list[str] | None = None) -> None:
    args = [
        "load",
        "--model",
        str(SAMPLES / "model.json"),
        "--replicas",
        str(SAMPLES / "replicas.json"),
        "--requests",
        str(SAMPLES / "requests.json"),
        "--precision",
        str(SAMPLES / "precision.json"),
        "--reset",
    ]
    if extra:
        args.extend(extra)
    cli(state_dir, *args, check=True)


def parse_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout)


def status(state_dir: Path) -> dict[str, Any]:
    result = cli(state_dir, "status", check=True)
    return parse_json(result)["status"]

