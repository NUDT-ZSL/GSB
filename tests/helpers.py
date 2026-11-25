from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def make_workdir(prefix: str = "topodiag-test-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def cli_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return env


def run_cli(
    *args: str,
    check: bool = True,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "topodiag", *args],
        cwd=str(ROOT),
        env=cli_env(),
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"CLI failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def run_cli_json(*args: str, check: bool = True) -> tuple[dict[str, Any], int]:
    result = run_cli(*args, check=check)
    return json.loads(result.stdout), result.returncode


def status(workdir: Path) -> dict[str, Any]:
    return run_cli_json("status", "--workdir", str(workdir))[0]


def verify(workdir: Path) -> dict[str, Any]:
    return run_cli_json("verify", "--workdir", str(workdir))[0]
