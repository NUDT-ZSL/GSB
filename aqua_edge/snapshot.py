"""Versioned snapshot I/O."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import AquaError
from .scheduler import SNAPSHOT_VERSION, Scheduler
from .trace import load_json, save_json


def save_snapshot(scheduler: Scheduler, path: str | Path) -> dict[str, Any]:
    document = scheduler.snapshot_document()
    save_json(path, document)
    return document


def load_snapshot(path: str | Path) -> tuple[Scheduler, dict[str, Any]]:
    try:
        raw = load_json(path)
    except AquaError as exc:
        if exc.code == "E_JSON":
            raise AquaError(
                "E_SNAPVERSION",
                f"快照不可解析，期望版本 {SNAPSHOT_VERSION}",
                {
                    "path": str(path),
                    "reason": exc.message,
                    "expected": SNAPSHOT_VERSION,
                },
            ) from exc
        raise
    if not isinstance(raw, dict) or raw.get("snapshot_version") != SNAPSHOT_VERSION:
        found = raw.get("snapshot_version") if isinstance(raw, dict) else None
        raise AquaError(
            "E_SNAPVERSION",
            f"快照版本不兼容，期望 {SNAPSHOT_VERSION}",
            {"found": found, "expected": SNAPSHOT_VERSION},
        )
    scheduler = Scheduler.from_snapshot(raw)
    return scheduler, raw

