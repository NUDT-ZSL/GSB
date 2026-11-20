from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from .errors import fail


class FileLock:
    """Small cross-platform advisory lock backed by one lock-file byte."""

    def __init__(self, path: str | Path, timeout_s: float = 10.0) -> None:
        self.path = Path(path)
        self.timeout_s = timeout_s
        self._file = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a+b")
        try:
            if os.name == "nt":
                self._lock_windows()
            else:
                self._lock_posix()
        except BaseException:
            self._file.close()
            self._file = None
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None

    def _lock_windows(self) -> None:
        import msvcrt

        assert self._file is not None
        self._file.seek(0)
        if self._file.read(1) == b"":
            self._file.write(b"\0")
            self._file.flush()
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    fail("E_LOCK_TIMEOUT", "file lock acquisition timed out")
                time.sleep(0.01)

    def _lock_posix(self) -> None:
        import fcntl

        assert self._file is not None
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                fcntl.flock(
                    self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    fail("E_LOCK_TIMEOUT", "file lock acquisition timed out")
                time.sleep(0.01)


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def atomic_write_bytes(path: str | Path, payload: bytes) -> None:
    """Write bytes to a sibling temp file, fsync it, then atomically replace."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
        if os.name != "nt":
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str | Path, value: Any) -> None:
    text = json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    )
    atomic_write_bytes(path, (text + "\n").encode("utf-8"))


def read_json(path: str | Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        fail("E_IO", "input file does not exist", path=str(path))
    except json.JSONDecodeError as exc:
        fail(
            "E_JSON",
            "input file is not valid JSON",
            path=str(path),
            line=exc.lineno,
            column=exc.colno,
        )
    except OSError as exc:
        fail("E_IO", "cannot read input file", path=str(path), error=str(exc))

