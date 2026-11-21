"""Small deterministic and cross-platform utilities."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterator

from .errors import bad


_THREAD_LOCK = threading.RLock()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"


def sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def read_json(path: str | os.PathLike[str]) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise bad("E_IO", f"file not found: {path}", str(path))
    except json.JSONDecodeError as exc:
        raise bad(
            "E_BAD_JSON",
            f"invalid JSON: {exc.msg}",
            str(path),
            line=exc.lineno,
            column=exc.colno,
        )


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)


def atomic_write_json(path: str | os.PathLike[str], value: Any) -> None:
    atomic_write_text(path, stable_json(value))


@contextlib.contextmanager
def state_file_lock(state_dir: str | os.PathLike[str]) -> Iterator[None]:
    """Cross-platform exclusive file lock for engine write critical sections."""

    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "engine.lock"
    handle = open(lock_path, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        with _THREAD_LOCK:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def as_nonnegative_int(value: Any, path: str, code: str = "E_BAD_CALL") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise bad(code, "expected a non-negative integer", path)
    return value


def as_positive_int(value: Any, path: str, code: str = "E_BAD_FUNC") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise bad(code, "expected a positive integer", path)
    return value


def require_object(value: Any, path: str, code: str = "E_BAD_INPUT") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise bad(code, "expected a JSON object", path)
    return value


def require_list(value: Any, path: str, code: str = "E_BAD_INPUT") -> list[Any]:
    if not isinstance(value, list):
        raise bad(code, "expected a JSON array", path)
    return value


def require_string(value: Any, path: str, code: str = "E_BAD_INPUT") -> str:
    if not isinstance(value, str) or not value.strip():
        raise bad(code, "expected a non-empty string", path)
    return value
