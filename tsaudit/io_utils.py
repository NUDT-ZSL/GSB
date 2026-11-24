"""Atomic file writes and cross-process workspace locking."""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from typing import Any, Callable

from .canon import strict_json_loads
from .codes import Code
from .errors import AuditError


_TEMP_COUNTER = itertools.count(1)


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    path: str | Path,
    data: bytes,
    *,
    validator: Callable[[Path], None] | None = None,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / (
        f".{target.name}.tmp.{os.getpid()}.{next(_TEMP_COUNTER)}"
    )
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if validator is not None:
            validator(temp)
        os.replace(temp, target)
        fsync_directory(target.parent)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def atomic_write_json(path: str | Path, value: Any) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")

    def validate(candidate: Path) -> None:
        loaded = strict_json_loads(candidate.read_text(encoding="utf-8"))
        if loaded != value:
            raise AuditError(
                Code.E_CHECKPOINT,
                "atomic JSON content validation failed",
                {"path": str(path)},
            )

    atomic_write_bytes(path, payload, validator=validate)


def safe_read_json(path: str | Path) -> tuple[Any | None, str | None]:
    target = Path(path)
    if not target.is_file():
        return None, "missing"
    try:
        return strict_json_loads(target.read_text(encoding="utf-8")), None
    except Exception as exc:  # corrupted JSON must be recoverable
        return None, f"{type(exc).__name__}: {exc}"


class WorkspaceLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise AuditError(
                Code.E_LOCK,
                "workspace lock is already held by another writer",
                {"lock_path": str(self.path)},
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> "WorkspaceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()
