from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping

from .errors import LayerForgeError, PathEscapeError


KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def require_int(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LayerForgeError(
            f"{field} must be an integer",
            details={"field": field, "value": value, "actual_type": type(value).__name__},
        )
    if minimum is not None and value < minimum:
        raise LayerForgeError(
            f"{field} must be >= {minimum}",
            details={"field": field, "value": value, "minimum": minimum},
        )
    return value


def require_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise LayerForgeError(
            f"{field} must be a non-empty string",
            details={"field": field, "value": value},
        )
    return value


def validate_key_values(
    values: Any,
    field: str,
    *,
    env: bool = False,
) -> Dict[str, Any]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise LayerForgeError(
            f"{field} must be an object",
            details={"field": field, "actual_type": type(values).__name__},
        )
    pattern = ENV_KEY_RE if env else KEY_RE
    result: Dict[str, Any] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not pattern.fullmatch(key):
            raise LayerForgeError(
                f"invalid {field} key",
                details={"field": field, "key": key, "pattern": pattern.pattern},
            )
        if env:
            if value is None:
                value = ""
            if not isinstance(value, (str, int, float, bool)):
                raise LayerForgeError(
                    f"{field}.{key} must be a scalar",
                    details={"field": field, "key": key, "value_type": type(value).__name__},
                )
            result[key] = str(value) if not isinstance(value, bool) else ("true" if value else "false")
        else:
            if not isinstance(value, (str, int, float, bool)) or value is None:
                raise LayerForgeError(
                    f"{field}.{key} must be a string, number, or boolean",
                    details={"field": field, "key": key, "value_type": type(value).__name__},
                )
            result[key] = value
    return {key: result[key] for key in sorted(result)}


def safe_relative_path(raw_path: Any, *, workspace_root: Path) -> str:
    """Validate an action path lexically and against the resolved root."""

    path_text = require_nonempty_str(raw_path, "action.path")
    if "\x00" in path_text:
        raise PathEscapeError(
            "action path contains NUL",
            details={"path": path_text, "root": str(workspace_root)},
        )
    pure = PurePosixPath(path_text)
    if pure.is_absolute() or path_text.startswith(("/", "\\")):
        raise PathEscapeError(
            "action path must be relative",
            details={"path": path_text, "root": str(workspace_root)},
        )
    parts = [part for part in pure.parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise PathEscapeError(
            "action path contains '..'",
            details={"path": path_text, "root": str(workspace_root)},
        )
    relative = Path(*parts) if parts else Path(".")
    root = workspace_root.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PathEscapeError(
            "action path escapes workspace root",
            details={"path": path_text, "resolved": str(target), "root": str(root)},
        ) from exc
    return relative.as_posix()


def validate_seq_continuity(steps: list[Dict[str, Any]]) -> None:
    seqs = [require_int(step.get("seq"), "step.seq", minimum=1) for step in steps]
    if len(seqs) != len(set(seqs)):
        raise LayerForgeError(
            "step seq values must be unique",
            details={"seqs": seqs},
        )
    ordered = sorted(seqs)
    if ordered != list(range(1, len(ordered) + 1)):
        raise LayerForgeError(
            "step seq values must be continuous from 1",
            details={"seqs": seqs, "expected": list(range(1, len(ordered) + 1))},
        )
