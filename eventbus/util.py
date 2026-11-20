from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import fail


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("E_INPUT_JSON", "duplicate JSON object key", key=key)
        result[key] = value
    return result


def parse_json_text(text: str, source: str = "<input>") -> Any:
    def reject_constant(value: str) -> None:
        fail(
            "E_INPUT_JSON",
            "non-finite JSON numbers are not allowed",
            source=source,
            value=value,
        )

    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except ValueError as exc:
        fail("E_INPUT_JSON", "invalid JSON input", source=source, reason=str(exc))


def load_json(path: str | Path) -> Any:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        fail("E_INPUT_JSON", "cannot read JSON file", path=str(p), reason=str(exc))
    return parse_json_text(text, str(p))


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        fail("E_BAD_JSON", "value is not JSON-serializable", reason=str(exc))


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def normalize_partition_key(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        fail("E_BAD_PARTITION_KEY", "partition key must be a string or integer")
    if isinstance(value, int):
        return f"int:{value}"
    if isinstance(value, str):
        if not value:
            fail("E_BAD_PARTITION_KEY", "partition key must not be empty")
        return f"str:{value}"
    fail(
        "E_BAD_PARTITION_KEY",
        "partition key must be a string or integer",
        value_type=type(value).__name__,
    )


def partition_for(normalized_key: str, partitions: int) -> int:
    digest = hashlib.blake2b(
        normalized_key.encode("utf-8"),
        digest_size=8,
        person=b"eventbus",
    ).digest()
    return int.from_bytes(digest, "big") % partitions


def require_plain_int(value: Any, code: str, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(code, f"{field} must be an integer", field=field)
    return value


def require_nonempty_str(value: Any, code: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        fail(code, f"{field} must be a non-empty string", field=field)
    return value


def stable_sort_dicts(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: str(item.get(key, "")))


class FileLock:
    """Small cross-process advisory lock held for one command."""

    def __init__(self, path: str | Path, timeout: float = 15.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._handle: Any = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    fail(
                        "E_BUSY",
                        "another writer holds the event-bus lock",
                        lock=str(self.path),
                    )
                time.sleep(0.02)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def crash_point(name: str) -> None:
    """Simulate a hard process termination at a named boundary."""

    if os.environ.get("EVENTBUS_CRASH_AT") == name:
        os._exit(90)


@contextmanager
def atomic_output(path: str | Path) -> Iterator[Any]:
    """Write a text file via an atomic replace."""

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}")
    handle = tmp.open("w", encoding="utf-8", newline="\n")
    try:
        yield handle
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp, p)
    finally:
        if not handle.closed:
            handle.close()
        if tmp.exists():
            tmp.unlink()

