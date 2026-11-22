from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .util import atomic_write_json, read_json_file
from .workspace import Workspace


def write_json_report(path: Path, value: dict[str, Any]) -> None:
    atomic_write_json(path, value)


def read_json_report(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = read_json_file(path)
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def record_error(workspace: Workspace, error: BusinessError) -> None:
    payload = error.as_dict()
    write_json_report(workspace.last_error_path, payload)


def clear_error(workspace: Workspace) -> None:
    try:
        workspace.last_error_path.unlink()
    except FileNotFoundError:
        pass
