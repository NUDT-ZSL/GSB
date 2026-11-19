"""Small append-only recent business error display log."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes, read_jsonl
from .errors import BusinessError


def record_error(state_dir: Path, error: BusinessError) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "diagnostics.jsonl"
    payload = {
        "code": error.code,
        "message": error.message,
        "details": dict(error.details),
    }
    with path.open("ab") as handle:
        handle.write(canonical_json_bytes(payload))


def recent_errors(state_dir: Path, limit: int = 5) -> list[dict[str, Any]]:
    path = state_dir / "diagnostics.jsonl"
    if not path.is_file():
        return []
    try:
        rows = read_jsonl(path)
    except BusinessError:
        return []
    return rows[-limit:]

