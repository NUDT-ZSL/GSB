from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterator

from .errors import BusinessError, fail


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_json(path: Path, *, label: str) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        fail("E_INPUT_MISSING", f"{label} does not exist", path=str(path))
    except json.JSONDecodeError as exc:
        fail(
            "E_VALIDATION",
            f"{label} is not valid JSON",
            path=str(path),
            line=exc.lineno,
            column=exc.colno,
            error=str(exc),
        )
    except OSError as exc:
        fail("E_VALIDATION", f"cannot read {label}", path=str(path), error=str(exc))


def require_dict(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail("E_VALIDATION", f"{label} must be an object", actual_type=type(value).__name__)
    return value


def require_list(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        fail("E_VALIDATION", f"{label} must be an array", actual_type=type(value).__name__)
    return value


def require_str(value: Any, *, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and value == ""):
        fail("E_VALIDATION", f"{label} must be a non-empty string")
    return value


def require_int(value: Any, *, label: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail("E_VALIDATION", f"{label} must be an integer")
    if minimum is not None and value < minimum:
        fail("E_VALIDATION", f"{label} must be >= {minimum}", actual=value)
    return value


def require_identifier(value: Any, *, label: str) -> str:
    result = require_str(value, label=label)
    if not IDENTIFIER_RE.fullmatch(result):
        fail("E_VALIDATION", f"{label} is not a safe SQL identifier", value=result)
    return result


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def ensure_relative_child(base: Path, relative: str, *, label: str) -> Path:
    candidate = (base / relative).resolve()
    base_resolved = base.resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        fail("E_VALIDATION", f"{label} escapes data directory", path=relative)
    return candidate


def normalize_json_value(value: Any, *, label: str) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            fail("E_VALIDATION", f"{label} must not contain non-integral floats")
        return int(value)
    if isinstance(value, list):
        return [normalize_json_value(item, label=label) for item in value]
    if isinstance(value, dict):
        return {
            str(key): normalize_json_value(item, label=label)
            for key, item in sorted(value.items())
        }
    fail("E_VALIDATION", f"{label} contains unsupported value", actual_type=type(value).__name__)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


class FileLock:
    """Small cross-process advisory lock used around schema/rule transitions."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()


def stable_sorted_dict(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: stable_sorted_dict(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [stable_sorted_dict(item) for item in value]
    return value


def dump_json(value: Any, *, pretty: bool = True) -> str:
    if pretty:
        return json.dumps(
            stable_sorted_dict(value),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    return canonical_json(value)


@contextlib.contextmanager
def read_only_sqlite(path: Path) -> Iterator[Any]:
    import sqlite3

    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise BusinessError(
            code="E_SHARD_DB_UNOPENABLE",
            message="SQLite file cannot be opened",
            details={"path": str(path), "error": str(exc)},
        ) from exc
    try:
        connection.execute("PRAGMA query_only = ON")
        yield connection
    finally:
        connection.close()
