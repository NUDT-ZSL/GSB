"""Atomic JSON report output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import atomic_write_json


def write_report(path: Path, report: dict[str, Any]) -> Path:
    atomic_write_json(Path(path), report)
    return Path(path)
