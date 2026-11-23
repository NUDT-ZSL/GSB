from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import AbstractContextManager
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

from .errors import AppError, ErrorCode


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_text(data: str) -> str:
    return sha256_bytes(data.encode("utf-8"))


def sha256_obj(value: Any) -> str:
    return sha256_text(canonical_json(value))


def absolute_ms_from_relative_seconds(
    raw_relative_seconds: int | float | str, anchor_ms: int
) -> tuple[int, dict[str, Any]]:
    try:
        raw = Decimal(str(raw_relative_seconds))
    except Exception as exc:
        raise AppError(
            ErrorCode.CHECKPOINT_INVALID,
            "v1 relative checkpoint time is not numeric",
            {"value": raw_relative_seconds},
        ) from exc
    if not raw.is_finite() or raw < 0:
        raise AppError(
            ErrorCode.CHECKPOINT_INVALID,
            "v1 relative checkpoint time must be a finite non-negative value",
            {"value": raw_relative_seconds},
        )
    offset = (raw * Decimal(1000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    absolute_ms = int(anchor_ms) + int(offset)
    conversion = {
        "source": "v1_relative_seconds",
        "raw_relative_seconds": raw_relative_seconds,
        "clock_anchor_ms": int(anchor_ms),
        "scale_ms_per_unit": 1000,
        "rounding": "half_up",
        "rounding_direction": "nearest_ties_away_from_zero",
        "offset_ms": int(offset),
        "absolute_saved_at_ms": absolute_ms,
    }
    return absolute_ms, conversion


def safe_identifier(name: str) -> str:
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    cleaned = cleaned[:32] or "table"
    if cleaned[0].isdigit():
        cleaned = "t_" + cleaned
    return f"{cleaned}_{digest}"


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, stable_json(value) + "\n")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class FileLock(AbstractContextManager["FileLock"]):
    """Cross-platform advisory exclusive lock.

    SQLite protects database transactions; this adjacent file lock serializes
    multi-step operations and SQLite's own BEGIN IMMEDIATE retry window.
    """

    def __init__(self, path: Path, timeout_seconds: float = 30.0) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._handle = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+b")
        if self._handle.seek(0, os.SEEK_END) == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        self._handle.seek(0)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise AppError(
                        ErrorCode.LOCK_TIMEOUT,
                        "timed out waiting for the workspace lock",
                        {"lock_path": str(self.path)},
                    )
                time.sleep(0.02)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def first_difference(
    left: dict[tuple[str, str], dict[str, Any]],
    right: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    for key in sorted(set(left) | set(right)):
        lval = left.get(key)
        rval = right.get(key)
        if lval != rval:
            return {
                "table": key[0],
                "key": key[1],
                "incremental": lval,
                "full_replay": rval,
            }
    return None


def sorted_rows(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (row["table"], row["key"]))
