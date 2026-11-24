"""Small deterministic helpers, atomic file operations and file locking."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .codes import Code
from .errors import EngineError


def canonical_rule_key(value: str) -> str:
    return value.strip()


def normalize_process_guid(value: str) -> str:
    return value.strip().lower()


def normalize_digest(value: str) -> str:
    return value.strip().lower()


def stable_json(value: Any, *, pretty: bool = False) -> str:
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


def digest_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_nonnegative_int(value: Any) -> bool:
    return is_int(value) and value >= 0


def merge_key_for(rule_key: str, payload_digest: str) -> str:
    return hashlib.sha256(
        (rule_key + "\x00" + payload_digest).encode("utf-8")
    ).hexdigest()


def cluster_id_for(
    host_id: str,
    merge_key: str,
    representative_alert_id: str,
    member_alert_ids: tuple[str, ...] | None = None,
) -> str:
    raw = (
        host_id
        + "\x00"
        + merge_key
        + "\x00"
        + representative_alert_id
    ).encode("utf-8")
    if member_alert_ids is not None:
        raw += b"\x00" + b"\x00".join(
            item.encode("utf-8") for item in sorted(member_alert_ids)
        )
    return "c-" + hashlib.sha256(raw).hexdigest()[:24]


def _flock_nonblocking(handle) -> bool:
    try:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return False
        return True
    except ImportError:  # pragma: no cover - Windows
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True


def _flock_unlock(handle) -> None:
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except ImportError:  # pragma: no cover - Windows
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


class FileLock:
    """Non-blocking cross-process lock with a stable E_LOCK error."""

    _local_guard = threading.Lock()

    def __init__(self, path: Path, *, timeout_ms: int = 0):
        self.path = Path(path)
        self.timeout_ms = timeout_ms
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if not _flock_nonblocking(handle):
            handle.close()
            raise EngineError(
                Code.E_LOCK,
                "workspace file lock is already held",
                details={"lock_path": str(self.path)},
            )
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            _flock_unlock(self._handle)
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    lock_path: Path | None = None,
    acquire_lock: bool = True,
    temp_dir: Path | None = None,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if temp_dir is not None:
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
    lock = FileLock(lock_path or (path.parent / ".engine.lock"))
    lock_context = lock if acquire_lock else _NullContext()
    with lock_context:
        descriptor, temp_name = tempfile.mkstemp(
            prefix="." + path.name + ".",
            suffix=".tmp",
            dir=str(temp_dir if temp_dir is not None else path.parent),
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def atomic_write_json(
    path: Path,
    value: Any,
    *,
    lock_path: Path | None = None,
    validator: Callable[[Any], None] | None = None,
    pretty: bool = True,
    acquire_lock: bool = True,
    temp_dir: Path | None = None,
) -> None:
    text = stable_json(value, pretty=pretty)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - internal guard
        raise EngineError(
            Code.E_ATOMIC_WRITE,
            "serialized JSON is not parseable",
            details={"path": str(path), "error": str(exc)},
        ) from exc
    if validator is not None:
        validator(parsed)
    atomic_write_bytes(
        Path(path),
        text.encode("utf-8"),
        lock_path=lock_path,
        acquire_lock=acquire_lock,
        temp_dir=temp_dir,
    )


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def read_json(path: Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def remove_tree_contents(path: Path) -> None:
    path = Path(path)
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


@contextmanager
def temporary_directory(parent: Path, prefix: str) -> Iterator[Path]:
    parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
