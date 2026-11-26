from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def run_cli(
    *args: str,
    db: str | Path,
    env_extra: dict[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    if env_extra:
        env.update(env_extra)
    process = subprocess.run(
        [sys.executable, "-m", "engine_diag", "--db", str(db), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    if check and process.returncode != 0:
        raise AssertionError(
            f"command failed ({process.returncode}): {process.args}\n"
            f"stdout={process.stdout}\nstderr={process.stderr}"
        )
    return process


def json_output(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"stdout is not JSON: {process.stdout!r}\nstderr={process.stderr!r}"
        ) from exc


def load_default(db: str | Path, *, plan: str = "plans_v1.json") -> dict[str, Any]:
    process = run_cli(
        "load",
        "--tasks",
        str(SAMPLES / "tasks_v1.jsonl"),
        "--stats",
        str(SAMPLES / "platform_stats.json"),
        "--plan",
        str(SAMPLES / plan),
        db=db,
    )
    result = json_output(process)
    if process.returncode != 0:
        raise AssertionError(result)
    return result

