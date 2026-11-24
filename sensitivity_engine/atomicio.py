"""Parse-before-rename atomic JSON writes and stale temp detection."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    validator: Callable[[Any], None] | None = None,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json_text(payload))
            handle.flush()
            os.fsync(handle.fileno())

        with temporary.open("r", encoding="utf-8") as handle:
            parsed = json.load(handle)
        if validator is not None:
            validator(parsed)

        os.replace(temporary, target)
        return target
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def stale_temp_files(path: str | Path) -> list[Path]:
    target = Path(path)
    if not target.parent.exists():
        return []
    return sorted(target.parent.glob(f".{target.name}.*.tmp"))


def remove_stale_temp_files(path: str | Path) -> list[str]:
    removed: list[str] = []
    for temporary in stale_temp_files(path):
        try:
            temporary.unlink()
            removed.append(str(temporary))
        except OSError:
            pass
    return removed
