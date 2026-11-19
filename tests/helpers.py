from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
NOW = "1760000200000"


def run_cli(state_dir: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "cacheman",
            "--state-dir",
            str(state_dir),
            *args,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def json_stdout(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(completed.stdout)


def initial_source_args() -> list[str]:
    return [
        "--v1",
        "samples/v1/cache.json",
        "--v2",
        "samples/v2/meta.sqlite3",
        "--now-ms",
        NOW,
    ]


def pipeline_args() -> list[str]:
    return [
        "--pipeline",
        "samples/pipelines/app-ci.json",
        "--pipeline",
        "samples/pipelines/docs-ci.json",
        "--now-ms",
        NOW,
    ]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
