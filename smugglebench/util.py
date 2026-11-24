"""Canonical serialization, atomic files and Windows/POSIX file locks."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterator

from .codes import E_LOCK


def canonical_json(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        )
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def canonical_rule_name(name: str) -> str:
    """Canonicalize a rule key without erasing meaningful separators."""

    text = unicodedata.normalize("NFKC", name).strip().casefold()
    text = "_".join(text.split())
    return text


def stable_mutation_key(item: Any) -> tuple[str, str, str]:
    return (
        str(getattr(item, "mutation_id", "")),
        canonical_json(getattr(item, "stable_payload", {})),
        str(getattr(item, "connection_id", "")),
    )


class BusinessError(Exception):
    """An expected, serializable business failure."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class WorkdirLock:
    """Non-blocking advisory lock backed by fcntl or msvcrt."""

    def __init__(self, workdir: str | os.PathLike[str], name: str = ".workbench.lock"):
        self.path = Path(workdir) / name
        self._fd: int | None = None

    def acquire(self) -> "WorkdirLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            os.close(fd)
            raise BusinessError(
                E_LOCK,
                "work directory is already locked",
                {"lock_file": str(self.path)},
            ) from exc
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "WorkdirLock":
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


def atomic_write_bytes(
    path: str | os.PathLike[str],
    data: bytes,
    *,
    validator: Any | None = None,
) -> None:
    """Write a temp file, validate it, fsync and atomically replace the target."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.tmp-",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path = Path(temp_name)
        if validator is not None:
            validator(temp_path)
        os.replace(temp_path, target)
        if os.name != "nt":
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def atomic_write_json(
    path: str | os.PathLike[str],
    value: Any,
    *,
    pretty: bool = True,
) -> None:
    payload = (canonical_json(value, pretty=pretty) + "\n").encode("utf-8")

    def validate(temp_path: Path) -> None:
        with temp_path.open("r", encoding="utf-8") as handle:
            json.load(handle)

    atomic_write_bytes(path, payload, validator=validate)


def read_json(path: str | os.PathLike[str]) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


@contextlib.contextmanager
def elapsed_ns() -> Iterator[dict[str, int]]:
    mark = {"start_ns": time.monotonic_ns(), "end_ns": 0}
    try:
        yield mark
    finally:
        mark["end_ns"] = time.monotonic_ns()


def monotonic_deadline(seconds: float) -> int:
    return time.monotonic_ns() + int(seconds * 1_000_000_000)


def remaining_seconds(deadline_ns: int) -> float:
    return max(0.0, (deadline_ns - time.monotonic_ns()) / 1_000_000_000)
