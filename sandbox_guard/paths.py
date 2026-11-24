"""Executable path syntax validation."""

from __future__ import annotations

import re


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_INVALID_CHARS = set('<>?"|*')
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_executable_path(value: object, *, path_style: str = "windows") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("executable path must be a non-empty string")
    if path_style == "posix":
        return _validate_posix(value)
    if path_style not in {"windows", "auto"}:
        raise ValueError(f"unsupported path_style: {path_style}")
    if path_style == "auto" and value.startswith("/"):
        return _validate_posix(value)
    return _validate_windows(value)


def _validate_posix(value: str) -> str:
    if not value.startswith("/"):
        raise ValueError("POSIX executable path must be absolute")
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("path contains a control character")
    if "\x00" in value:
        raise ValueError("path contains NUL")
    segments = value.split("/")
    if not segments[-1]:
        raise ValueError("executable path must name a file")
    return value


def _validate_windows(value: str) -> str:
    original = value
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("path contains a control character")
    if "\x00" in value:
        raise ValueError("path contains NUL")
    if any(ch in _INVALID_CHARS for ch in value):
        raise ValueError("path contains invalid Windows file-name characters")

    normalized = value.replace("/", "\\")
    unc = normalized.startswith("\\\\")
    drive = bool(_WINDOWS_DRIVE_RE.match(value))
    if not unc and not drive:
        raise ValueError("Windows executable path must be drive-absolute or UNC")

    if drive:
        rest = normalized[3:]
        segments = rest.split("\\")
    else:
        rest = normalized[2:]
        segments = rest.split("\\")
        if len(segments) < 3 or not segments[0] or not segments[1]:
            raise ValueError("UNC path must include server and share")
        segments = segments[2:]

    if not segments or not segments[-1]:
        raise ValueError("executable path must name a file")
    for segment in segments:
        if segment in {"", ".", ".."}:
            if segment == "":
                raise ValueError("path contains an empty segment")
            continue
        stem = segment.split(".", 1)[0].upper()
        if stem in _RESERVED_WINDOWS_NAMES:
            raise ValueError(f"path uses reserved Windows name {stem}")
    if not re.search(r"\.[A-Za-z0-9]{1,8}$", segments[-1]):
        raise ValueError("executable path must include a file extension")
    return original
