"""Small deterministic I/O and serialization helpers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .errors import DiagnosticsError


def stable_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def stable_json_text(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return stable_json_bytes(value).decode("utf-8") + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise DiagnosticsError(
            "E_IO", message=f"cannot read {path}", location=str(path), details={"reason": str(exc)}
        ) from exc
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DiagnosticsError(
            "E_IO", message=f"cannot read {path}", location=str(path), details={"reason": str(exc)}
        ) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DiagnosticsError(
            "E_JSON",
            location=f"{path}:{exc.lineno}:{exc.colno}",
            details={"reason": exc.msg},
        ) from exc


def read_jsonl(path: Path) -> list[tuple[int, Any]]:
    rows: list[tuple[int, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DiagnosticsError(
            "E_IO", message=f"cannot read {path}", location=str(path), details={"reason": str(exc)}
        ) from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rows.append((line_number, json.loads(line)))
        except json.JSONDecodeError as exc:
            raise DiagnosticsError(
                "E_JSON",
                location=f"{path}:{line_number}:{exc.colno}",
                details={"reason": exc.msg},
            ) from exc
    return rows


def resolve_path(base: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base / path).resolve()


def ensure_parent(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DiagnosticsError(
            "E_IO",
            message=f"cannot create directory {path.parent}",
            location=str(path.parent),
            details={"reason": str(exc)},
        ) from exc


def atomic_replace_text(path: Path, text: str) -> None:
    ensure_parent(path)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise DiagnosticsError(
            "E_IO", message=f"cannot write {path}", location=str(path), details={"reason": str(exc)}
        ) from exc
