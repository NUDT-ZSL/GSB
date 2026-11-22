from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from lease_arbiter.samplegen import sample_documents


ROOT = Path(__file__).resolve().parents[1]


def write_docs(directory: Path, documents: dict[str, Any]) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for filename, document in documents.items():
        path = directory / filename
        path.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        paths[filename] = path
    return paths


def sample_paths(directory: Path) -> dict[str, Path]:
    return write_docs(directory, sample_documents())


def cli(
    *args: str,
    db: Path,
    pointer: Path,
    reports: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "lease_arbiter",
        "--db",
        str(db),
        "--pointer",
        str(pointer),
        "--reports",
        str(reports),
        *args,
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, **(env or {})},
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"command failed: {' '.join(command)}\n"
            f"rc={completed.returncode}\nstdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def cli_json(*args: str, **kwargs: Any) -> dict[str, Any]:
    completed = cli(*args, **kwargs)
    return json.loads(completed.stdout)


def load_args(paths: dict[str, Path]) -> list[str]:
    return [
        "load",
        "--cards",
        str(paths["accelerator_cards.json"]),
        "--jobs",
        str(paths["jobs.json"]),
        "--config",
        str(paths["lease_config.json"]),
        "--anomalies",
        str(paths["anomaly_injections.json"]),
        "--force",
    ]
