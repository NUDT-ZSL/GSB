from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Iterator

from term_pipeline.config import load_config
from term_pipeline.pipeline import run_pipeline
from term_pipeline.runtime import load_committed_runtime
from tools.generate_samples import generate


def new_workspace() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temp = tempfile.TemporaryDirectory(prefix="term-test-")
    root = Path(temp.name)
    generate(root)
    return temp, root


def run_workspace(root: Path) -> dict:
    return run_pipeline(root / "config.json")


def runtime(root: Path) -> dict:
    return load_committed_runtime(root / "config.json")


def read_config(root: Path) -> dict:
    return json.loads((root / "config.json").read_text(encoding="utf-8"))


def write_config(root: Path, value: dict) -> None:
    path = root / "config.json"
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

