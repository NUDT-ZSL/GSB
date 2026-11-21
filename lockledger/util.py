"""Small deterministic and cross-platform helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import fail

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_identifier(name: str, path: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.fullmatch(name):
        fail(
            "E_BAD_IDENTIFIER",
            "identifiers must match ^[A-Za-z_][A-Za-z0-9_]*$",
            path,
        )
    return name


def quote_identifier(name: str) -> str:
    validate_identifier(name, "identifier")
    return '"' + name.replace('"', '""') + '"'


def ensure_within(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        fail(
            "E_PATH_OUTSIDE_WORKSPACE",
            f"path must stay inside workspace {root}: {resolved}",
        )
    return resolved


def check_workspace(root: Path, minimum_free_bytes: int = 16_777_216) -> None:
    probe_path: str | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        handle, probe_path = tempfile.mkstemp(prefix=".write-probe-", dir=root)
        with os.fdopen(handle, "w", encoding="ascii") as probe:
            probe.write("ok")
        os.unlink(probe_path)
        probe_path = None
    except OSError as exc:
        fail("E_WORKSPACE_NOT_WRITABLE", str(exc))
    finally:
        if probe_path is not None:
            try:
                os.unlink(probe_path)
            except OSError:
                pass
    usage = shutil.disk_usage(root)
    if usage.free < minimum_free_bytes:
        fail(
            "E_DISK_SPACE",
            f"need at least {minimum_free_bytes} free bytes, found {usage.free}",
        )


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class FileLock:
    """Advisory whole-process lock compatible with Windows and POSIX."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: Any = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        self._handle.seek(0)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
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


@contextmanager
def pushd(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)

