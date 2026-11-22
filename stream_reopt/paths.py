"""Filesystem safety checks for destructive test/reset commands."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .errors import BusinessError, ErrorCode


def assert_safe_delete(path: str | Path, *, protected: Iterable[str | Path] = ()) -> Path:
    resolved = Path(path).resolve()
    cwd = Path.cwd().resolve()
    home = Path.home().resolve()
    root = Path(resolved.anchor).resolve()
    forbidden = {cwd, cwd.parent, home, root}
    if resolved in forbidden:
        raise BusinessError(
            ErrorCode.BAD_COMMAND,
            "refusing to delete a protected workspace or filesystem path",
            details={"path": str(resolved)},
        )
    if cwd.is_relative_to(resolved):
        raise BusinessError(
            ErrorCode.BAD_COMMAND,
            "refusing to delete an ancestor of the current working directory",
            details={"path": str(resolved), "cwd": str(cwd)},
        )
    for item in protected:
        other = Path(item).resolve()
        if other == resolved or other.is_relative_to(resolved):
            raise BusinessError(
                ErrorCode.BAD_COMMAND,
                "refusing to delete a path that contains a protected input or state file",
                details={"path": str(resolved), "protected": str(other)},
            )
    return resolved

