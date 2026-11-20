from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "tilted"
PYTHON = sys.executable


def copy_sample(destination: Path) -> Path:
    shutil.copytree(SAMPLE, destination)
    return destination


def run_cli(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [PYTHON, "-m", "gsb", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=True,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

