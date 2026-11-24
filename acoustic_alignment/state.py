"""Persistent interruption markers."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .ledger import atomic_write_bytes


def write_interruption_marker(path: str | Path, payload: dict[str, Any]) -> Path:
    path = Path(path)
    import json

    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write_bytes(path, data)
    return path


def remove_interruption_marker(path: str | Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
