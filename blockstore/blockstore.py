from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Iterator

from .errors import BlockStoreError, ErrorCode


class ContentAddressedBlockStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.blocks_dir = self.root / "blocks"

    def ensure_directories(self) -> None:
        self.blocks_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        return self.blocks_dir / f"{digest}.blk"

    def _hash_file(self, path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()

    def verify(self, digest: str) -> Path:
        path = self.path_for(digest)
        if not path.exists():
            raise BlockStoreError(
                ErrorCode.BLOCK_MISSING,
                "content-addressed block file is missing",
                {"digest": digest, "path": str(path)},
            )
        actual = self._hash_file(path)
        if actual != digest:
            raise BlockStoreError(
                ErrorCode.BLOCK_CORRUPT,
                "content-addressed block file was modified",
                {"digest": digest, "actual_sha256": actual, "path": str(path)},
            )
        return path

    def read(self, digest: str) -> bytes:
        return self.verify(digest).read_bytes()

    def materialize(self, digest: str, data: bytes) -> Path:
        self.ensure_directories()
        calculated = hashlib.sha256(data).hexdigest()
        if calculated != digest:
            raise BlockStoreError(
                ErrorCode.DIGEST_MISMATCH,
                "attempted to materialize data under the wrong digest",
                {"expected": digest, "actual": calculated},
            )
        final_path = self.path_for(digest)
        if final_path.exists():
            self.verify(digest)
            return final_path
        temp_path = self.blocks_dir / (
            f".tmp-{digest}-{os.getpid()}-{uuid.uuid4().hex}"
        )
        try:
            with temp_path.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.replace(temp_path, final_path)
            except OSError as exc:
                raise BlockStoreError(
                    ErrorCode.IO_ERROR,
                    "unable to atomically publish block file",
                    {"digest": digest, "path": str(final_path), "reason": str(exc)},
                ) from exc
            self._fsync_directory()
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
        self.verify(digest)
        return final_path

    def delete(self, digest: str) -> bool:
        path = self.path_for(digest)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise BlockStoreError(
                ErrorCode.IO_ERROR,
                "unable to delete block file",
                {"digest": digest, "path": str(path), "reason": str(exc)},
            ) from exc

    def iter_digests(self) -> Iterator[str]:
        if not self.blocks_dir.exists():
            return
        for path in sorted(self.blocks_dir.iterdir(), key=lambda item: item.name):
            if not path.is_file():
                continue
            name = path.name
            if name.endswith(".blk") and len(name) == 68:
                digest = name[:-4]
                if all(character in "0123456789abcdef" for character in digest):
                    yield digest

    def digest_paths(self) -> dict[str, Path]:
        return {digest: self.path_for(digest) for digest in self.iter_digests()}

    def clean_temp_files(self) -> list[str]:
        if not self.blocks_dir.exists():
            return []
        removed: list[str] = []
        for path in sorted(self.blocks_dir.glob(".tmp-*")):
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                pass
        return removed

    def _fsync_directory(self) -> None:
        if os.name == "nt":
            return
        try:
            descriptor = os.open(self.blocks_dir, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
