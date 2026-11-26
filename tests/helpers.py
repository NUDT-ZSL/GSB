from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample"


def copy_sample(destination: Path) -> Path:
    destination = destination / "sample"
    shutil.copytree(SAMPLE, destination)
    return destination


def run_cli(
    args: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    check_json: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Any]:
    process_env = None
    if env is not None:
        import os

        process_env = os.environ.copy()
        process_env.update(env)
    completed = subprocess.run(
        [sys.executable, "-m", "quota_ledger", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        env=process_env,
    )
    payload: Any = None
    if check_json and completed.stdout.strip():
        payload = json.loads(completed.stdout)
    return completed, payload


def load_and_run(db: Path, data_dir: Path = SAMPLE) -> dict[str, Any]:
    loaded, _ = run_cli(
        ["load", "--data-dir", str(data_dir), "--db", str(db)],
        check_json=False,
    )
    if loaded.returncode != 0:
        raise AssertionError(loaded.stdout + loaded.stderr)
    ran, payload = run_cli(
        ["run", "--data-dir", str(data_dir), "--db", str(db)]
    )
    if ran.returncode != 0:
        raise AssertionError(ran.stdout + ran.stderr)
    return payload

