"""Atomic content-addressed block files."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import BusinessError


class BlockStorage:
    """Immutable SHA-256 addressed blocks under one state directory."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.blocks_dir = self.state_dir / "blocks"
        self.tmp_dir = self.state_dir / "tmp"
        self.trash_dir = self.state_dir / "trash"
        self.lock_dir = self.state_dir / "locks" / "blocks"

    def ensure_directories(self) -> None:
        for directory in (
            self.blocks_dir,
            self.tmp_dir,
            self.trash_dir,
            self.lock_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def path_for(self, digest: str) -> Path:
        return self.blocks_dir / digest

    @contextmanager
    def _digest_lock(self, digest: str) -> Iterator[None]:
        lock_path = self.lock_dir / f"{digest}.lock"
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def put(self, data: bytes) -> str:
        digest = self.digest(data)
        final_path = self.path_for(digest)
        with self._digest_lock(digest):
            if final_path.is_file():
                if not self.verify_file(digest, expected_size=len(data)):
                    raise BusinessError(
                        "BLOCK_CORRUPTION",
                        "an existing block file does not match its digest",
                        details={"digest": digest},
                    )
                return digest

            self.tmp_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{digest}.", suffix=".tmp", dir=self.tmp_dir
            )
            try:
                with os.fdopen(fd, "wb") as tmp_file:
                    tmp_file.write(data)
                    tmp_file.flush()
                    os.fsync(tmp_file.fileno())
                os.replace(tmp_name, final_path)
                self._fsync_directory(self.blocks_dir)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
                raise
        return digest

    def verify_file(self, digest: str, *, expected_size: int | None = None) -> bool:
        path = self.path_for(digest)
        if not path.is_file():
            return False
        hasher = hashlib.sha256()
        size = 0
        with path.open("rb") as block_file:
            while True:
                chunk = block_file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                hasher.update(chunk)
        return hasher.hexdigest() == digest and (
            expected_size is None or size == expected_size
        )

    def list_digests(self) -> list[str]:
        if not self.blocks_dir.is_dir():
            return []
        result = []
        for entry in sorted(self.blocks_dir.iterdir(), key=lambda item: item.name):
            if not entry.is_file() or entry.name.startswith("."):
                continue
            if len(entry.name) == 64 and all(
                char in "0123456789abcdef" for char in entry.name
            ):
                result.append(entry.name)
        return result

    def list_unexpected_files(self) -> list[str]:
        if not self.blocks_dir.is_dir():
            return []
        result = []
        for entry in sorted(self.blocks_dir.iterdir(), key=lambda item: item.name):
            valid_name = (
                entry.is_file()
                and len(entry.name) == 64
                and all(char in "0123456789abcdef" for char in entry.name)
            )
            if not valid_name:
                result.append(entry.name)
        return result

    def quarantine_and_remove(self, digest: str, run_id: str) -> bool:
        source = self.path_for(digest)
        if not source.is_file():
            return False
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        target = self.trash_dir / f"{digest}.{run_id}.delete"
        os.replace(source, target)
        self._fsync_directory(self.blocks_dir)
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass
        self._fsync_directory(self.trash_dir)
        return True

    def clean_tmp(self) -> int:
        if not self.tmp_dir.is_dir():
            return 0
        removed = 0
        for entry in sorted(self.tmp_dir.iterdir(), key=lambda item: item.name):
            if entry.is_file():
                try:
                    entry.unlink()
                    removed += 1
                except FileNotFoundError:
                    pass
        return removed

    def clean_trash(self) -> int:
        if not self.trash_dir.is_dir():
            return 0
        removed = 0
        for entry in sorted(self.trash_dir.iterdir(), key=lambda item: item.name):
            if entry.is_file():
                try:
                    entry.unlink()
                    removed += 1
                except FileNotFoundError:
                    pass
        return removed

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
