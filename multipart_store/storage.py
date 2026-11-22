"""Atomic local file materialization and deterministic fault injection."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Iterator

from .errors import DomainError, ErrorCode


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(staging_dir: Path, final_path: Path, content: bytes) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = staging_dir / f".part-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    try:
        with temp_path.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        fsync_directory(final_path.parent)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise DomainError(
            ErrorCode.STORAGE_DIR_INVALID,
            "failed to atomically write part file",
            {"path": str(final_path), "cause": str(exc)},
        ) from exc


class ObjectWriter:
    def __init__(self, staging_dir: Path, final_path: Path, algorithm: str = "sha256") -> None:
        self.staging_dir = staging_dir
        self.final_path = final_path
        self.algorithm = algorithm
        self.temp_path = staging_dir / f".object-{os.getpid()}-{uuid.uuid4().hex}.tmp"
        self._handle: object | None = None
        self._hash: hashlib._Hash | None = None
        self.size_bytes = 0

    def __enter__(self) -> "ObjectWriter":
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.final_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.temp_path.open("wb")
        self._hash = hashlib.new(self.algorithm)
        return self

    def write(self, chunk: bytes) -> None:
        if self._handle is None or self._hash is None:
            raise RuntimeError("writer is not open")
        self._handle.write(chunk)  # type: ignore[attr-defined]
        self._hash.update(chunk)
        self.size_bytes += len(chunk)

    def finish(self) -> tuple[str, bytes]:
        if self._handle is None or self._hash is None:
            raise RuntimeError("writer is not open")
        self._handle.flush()  # type: ignore[attr-defined]
        os.fsync(self._handle.fileno())  # type: ignore[attr-defined]
        self._handle.close()  # type: ignore[attr-defined]
        self._handle = None
        return self._hash.hexdigest(), self._hash.digest()

    def materialize(self) -> None:
        os.replace(self.temp_path, self.final_path)
        fsync_directory(self.final_path.parent)

    def discard(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()  # type: ignore[attr-defined]
            finally:
                self._handle = None
        try:
            self.temp_path.unlink(missing_ok=True)
        except OSError:
            pass

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is not None:
            self.discard()


def stream_digest(path: Path, algorithm: str) -> tuple[str, int]:
    digest = hashlib.new(algorithm)
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise DomainError(
            ErrorCode.PART_FILE_MISSING,
            "part file cannot be read",
            {"path": str(path), "cause": str(exc)},
        ) from exc
    return digest.hexdigest(), size


def chunks(path: Path) -> Iterator[bytes]:
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    return
                yield chunk
    except OSError as exc:
        raise DomainError(
            ErrorCode.PART_FILE_MISSING,
            "part file cannot be read",
            {"path": str(path), "cause": str(exc)},
        ) from exc


def fault(point: str) -> None:
    selected = os.environ.get("MULTIPART_FAULT_POINT")
    if selected and selected == point:
        code = int(os.environ.get("MULTIPART_FAULT_EXIT", "75"))
        os._exit(code)
