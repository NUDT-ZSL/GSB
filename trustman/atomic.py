"""Cross-platform stdlib-only workspace locking and atomic JSON writes."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import LockConflict
from .models import canonical_json


def _lock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def workspace_lock(workspace: str | Path) -> Iterator[None]:
    """Hold a non-blocking exclusive lock for one workspace command.

    A conflict is surfaced as E_LOCK before any state or report is touched.
    """

    workspace_path = Path(workspace)
    workspace_path.mkdir(parents=True, exist_ok=True)
    lock_path = workspace_path / ".trustman.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        try:
            _lock_fd(fd)
            locked = True
        except OSError as exc:
            raise LockConflict(
                f"workspace lock is already held: {lock_path.name}"
            ) from exc
        yield
    finally:
        if locked:
            try:
                _unlock_fd(fd)
            except OSError:
                pass
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _render_json(data: Any, pretty: bool) -> bytes:
    if pretty:
        text = json.dumps(
            data,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
    else:
        text = canonical_json(data)
    return (text + "\n").encode("utf-8")


def atomic_write_json(
    path: str | Path, data: Any, *, pretty: bool = True
) -> None:
    """Write, fsync, read back and verify JSON, then atomically replace."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _render_json(data, pretty)
    temp = target.with_name(
        f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        written = temp.read_bytes()
        decoded = json.loads(written.decode("utf-8"))
        if decoded != data:
            raise OSError("written JSON failed content verification")
        os.replace(temp, target)
        _fsync_directory(target.parent)
    except Exception:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def read_json_file(path: str | Path) -> Any:
    with Path(path).open("rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def orphan_temp_files(directory: str | Path, base_name: str) -> list[str]:
    directory_path = Path(directory)
    if not directory_path.is_dir():
        return []
    prefix = f".{base_name}."
    return sorted(
        path.name
        for path in directory_path.iterdir()
        if path.is_file()
        and path.name.startswith(prefix)
        and path.name.endswith(".tmp")
    )
