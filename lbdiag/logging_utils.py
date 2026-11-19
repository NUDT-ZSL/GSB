"""Append-only JSONL diagnostics log protected by the same portable file lock."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .filelock import InstanceFileLock
from .model import canonical_json


def append_event(log_dir: str | Path, event: dict[str, Any]) -> None:
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "diagnostics.jsonl"
    lock_path = directory / ".diagnostics.lock"
    line = canonical_json(event) + "\n"
    with InstanceFileLock(lock_path, timeout_ms=30_000):
        with log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()

