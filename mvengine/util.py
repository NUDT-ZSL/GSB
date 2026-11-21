"""Canonical serialization, hashing, time handling, and cross-platform locks."""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import fail


def canonical_json(value: Any) -> str:
    """Return the one canonical JSON representation used for every digest."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_json(value: Any) -> str:
    return digest_bytes(canonical_json(value).encode("utf-8"))


def digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def read_json(path: Path, *, code: str = "E_JSON_INVALID") -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, parse_constant=reject_constant)
    except FileNotFoundError:
        raise fail("E_INPUT_NOT_FOUND", "input file does not exist", path=str(path))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError) as exc:
        raise fail(
            code,
            "input is not a valid UTF-8 JSON document",
            path=str(path),
            detail=str(exc),
        )


def require_int(
    value: Any,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    code: str = "E_SCHEMA_INVALID",
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise fail(code, f"{field} must be an integer", field=field, value=value)
    if minimum is not None and value < minimum:
        raise fail(code, f"{field} must be >= {minimum}", field=field, value=value)
    if maximum is not None and value > maximum:
        raise fail(code, f"{field} must be <= {maximum}", field=field, value=value)
    return value


def require_str(
    value: Any,
    field: str,
    *,
    allow_empty: bool = False,
    code: str = "E_SCHEMA_INVALID",
) -> str:
    if not isinstance(value, str):
        raise fail(code, f"{field} must be a string", field=field, value=value)
    if not allow_empty and not value:
        raise fail(code, f"{field} must not be empty", field=field)
    return value


def require_list(value: Any, field: str, *, code: str = "E_SCHEMA_INVALID") -> list[Any]:
    if not isinstance(value, list):
        raise fail(code, f"{field} must be a list", field=field, value=value)
    return value


def require_dict(value: Any, field: str, *, code: str = "E_SCHEMA_INVALID") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise fail(code, f"{field} must be an object", field=field, value=value)
    return value


def safe_child(root: Path, relative: str) -> Path:
    """Resolve a package-relative path without allowing traversal."""

    require_str(relative, "path", code="E_SCHEMA_INVALID")
    candidate = (root / relative).resolve()
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        raise fail("E_SCHEMA_INVALID", "path escapes its package root", path=relative)
    return candidate


def relpath_posix(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


class FileLock:
    """Advisory process lock. SQLite remains the source of transactional truth."""

    def __init__(self, path: Path, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = timeout
        self._handle: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise fail(
                        "E_LOCK_TIMEOUT",
                        "timed out waiting for the writer lock",
                        path=str(self.path),
                    )
                time.sleep(0.02)

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


@contextmanager
def file_lock(path: Path, timeout: float = 30.0) -> Iterator[None]:
    lock = FileLock(path, timeout=timeout)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def seconds_floor_ms(value_ms: int) -> int:
    return (value_ms // 1000) * 1000

