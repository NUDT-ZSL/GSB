"""Atomic, integrity-checked local file replacement."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import Codes, MFAError


def atomic_write_bytes(path: str | Path, payload: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        if os.name != "nt":
            directory_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, MFAError):
            raise
        raise MFAError(
            Codes.ATOMIC_WRITE,
            "failed to atomically replace file",
            {"path": str(target), "error": str(exc)},
        ) from exc


def atomic_write_json(path: str | Path, value: Any) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    # Verify completeness before exposing the new name.
    json.loads(payload.decode("utf-8"))
    atomic_write_bytes(path, payload + b"\n")


def atomic_write_json_lines(path: str | Path, values: list[Any]) -> None:
    chunks: list[bytes] = []
    for value in values:
        line = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        json.loads(line)
        chunks.append(line.encode("utf-8") + b"\n")
    atomic_write_bytes(path, b"".join(chunks))


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)

