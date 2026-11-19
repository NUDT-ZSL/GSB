from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "canonical"


def copy_sample(destination: Path) -> Path:
    shutil.copytree(SAMPLE, destination)
    return destination


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def run_cli(
    *args: str,
    cwd: Path | None = None,
    expected_code: int = 0,
) -> tuple[subprocess.CompletedProcess[bytes], dict[str, Any]]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    completed = subprocess.run(
        [sys.executable, "-m", "vmpool", *args],
        cwd=str(cwd or ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != expected_code:
        raise AssertionError(
            f"CLI returned {completed.returncode}, expected {expected_code}\n"
            f"stdout={completed.stdout.decode('utf-8', 'replace')}\n"
            f"stderr={completed.stderr.decode('utf-8', 'replace')}"
        )
    if not completed.stdout.strip():
        return completed, {}
    return completed, json.loads(completed.stdout)


def spawn_cli(*args: str) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.Popen(
        [sys.executable, "-m", "vmpool", *args],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
