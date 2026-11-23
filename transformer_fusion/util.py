"""Deterministic hashing, atomic I/O and small numeric helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .errors import (
    Diagnostic,
    E_WORKSPACE_BUSY,
    E_WORKSPACE_NOT_WRITABLE,
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def array_bytes(array: np.ndarray) -> bytes:
    contiguous = np.ascontiguousarray(array)
    return contiguous.tobytes(order="C")


def content_hash_array(array: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(array)
    header = canonical_json(
        {
            "dtype": str(contiguous.dtype),
            "shape": list(contiguous.shape),
            "order": "C",
            "version": "tf-input-v1",
        }
    )
    return sha256_bytes(header.encode("utf-8") + b"\0" + contiguous.tobytes(order="C"))


def digest_array(array: np.ndarray, domain: str = "array") -> str:
    contiguous = np.ascontiguousarray(array)
    header = canonical_json(
        {
            "domain": domain,
            "dtype": str(contiguous.dtype),
            "shape": list(contiguous.shape),
            "order": "C",
        }
    )
    return sha256_bytes(header.encode("utf-8") + b"\0" + contiguous.tobytes(order="C"))


def npy_bytes(array: np.ndarray) -> bytes:
    import io

    buffer = io.BytesIO()
    np.save(buffer, np.ascontiguousarray(array), allow_pickle=False)
    return buffer.getvalue()


def npy_load_bytes(data: bytes) -> np.ndarray:
    import io

    return np.load(io.BytesIO(data), allow_pickle=False)


def ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    target = Path(path)
    ensure_dir(target.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str | Path, value: Any) -> None:
    atomic_write_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def read_json(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _lock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def preflight_directory(
    path: str | Path,
    *,
    case_id: str = "*",
    stage: str = "workspace",
) -> Diagnostic | None:
    """Check that a directory is writable and can be exclusively locked."""

    try:
        target = ensure_dir(path)
    except OSError as exc:
        return Diagnostic(
            code=E_WORKSPACE_NOT_WRITABLE,
            case_id=case_id,
            stage=stage,
            message=f"directory cannot be created: {target}",
            details={"error": str(exc), "path": str(path)},
        )

    probe = target / f".tf-write-{os.getpid()}.tmp"
    lock_path = target / ".tf-workspace.lock"
    try:
        with open(probe, "wb") as handle:
            handle.write(b"ok")
            handle.flush()
            os.fsync(handle.fileno())
        probe.unlink()
    except OSError as exc:
        try:
            probe.unlink()
        except OSError:
            pass
        return Diagnostic(
            code=E_WORKSPACE_NOT_WRITABLE,
            case_id=case_id,
            stage=stage,
            message=f"directory is not writable: {target}",
            details={"error": str(exc), "path": str(path)},
        )

    try:
        with open(lock_path, "a+b") as handle:
            _lock_file(handle)
            _unlock_file(handle)
    except OSError as exc:
        return Diagnostic(
            code=E_WORKSPACE_BUSY,
            case_id=case_id,
            stage=stage,
            message=f"directory is locked by another process: {target}",
            details={"error": str(exc), "path": str(path)},
        )
    return None


class LockedPath:
    """A short-lived cross-process exclusive lock around a directory."""

    def __init__(self, path: str | Path) -> None:
        self.path = ensure_dir(path)
        self.lock_path = self.path / ".tf-workspace.lock"
        self.handle: Any = None

    def __enter__(self) -> "LockedPath":
        self.handle = open(self.lock_path, "a+b")
        try:
            _lock_file(self.handle)
        except OSError:
            self.handle.close()
            self.handle = None
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.handle is not None:
            _unlock_file(self.handle)
            self.handle.close()
            self.handle = None


def float_hex(value: float) -> str:
    return float(value).hex()


def as_tuple(value: Sequence[Any] | np.ndarray) -> tuple[Any, ...]:
    return tuple(value.tolist() if isinstance(value, np.ndarray) else value)


def jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    return value
