"""Content-addressed object storage with atomic publication."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Iterable, Iterator

from .errors import E_DIGEST_MISMATCH, E_IO, E_OBJECT_MISSING, BusinessError
from .model import iter_artifact_bytes

CHUNK_SIZE = 64 * 1024


class ObjectStore:
    def __init__(self, root: str | Path, temp_dir: str | Path) -> None:
        self.root = Path(root)
        self.temp_dir = Path(temp_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    def relative_path(self, digest: str) -> str:
        return str(Path("objects") / digest[:2] / digest)

    @staticmethod
    def _digest_stream(chunks: Iterable[bytes]) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        for chunk in chunks:
            digest.update(chunk)
            size += len(chunk)
        return digest.hexdigest(), size

    def put_bytes(self, chunks: Iterable[bytes], expected_digest: str, expected_size: int) -> tuple[str, int]:
        """Atomically publish bytes and return (digest, size).

        If the expected digest already exists it is reused, after size and
        digest validation, without creating another object file.
        """
        target = self.path_for(expected_digest)
        if target.exists():
            actual_size, actual_digest = self.hash_file(target)
            if actual_size != expected_size or actual_digest != expected_digest:
                raise BusinessError(
                    E_DIGEST_MISMATCH,
                    "existing CAS object failed digest validation",
                    expected_digest=expected_digest,
                    actual_digest=actual_digest,
                    expected_size=expected_size,
                    actual_size=actual_size,
                    path=str(target),
                )
            return actual_digest, actual_size

        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        digest = hashlib.sha256()
        size = 0
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.temp_dir, prefix="pub-", delete=False
            ) as handle:
                temp_path = Path(handle.name)
                for chunk in chunks:
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            actual_digest = digest.hexdigest()
            if actual_digest != expected_digest or size != expected_size:
                raise BusinessError(
                    E_DIGEST_MISMATCH,
                    "generated CAS object failed digest validation",
                    expected_digest=expected_digest,
                    actual_digest=actual_digest,
                    expected_size=expected_size,
                    actual_size=size,
                )
            os.replace(temp_path, target)
            temp_path = None
            self._fsync_dir(target.parent)
            return actual_digest, size
        except BusinessError:
            raise
        except OSError as exc:
            raise BusinessError(E_IO, f"cannot publish CAS object: {exc}", path=str(target)) from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def materialize_generated(
        self, task_name: str, written_at_ms: int, size_bytes: int, digest: str
    ) -> tuple[str, int]:
        return self.put_bytes(
            iter_artifact_bytes(task_name, written_at_ms, size_bytes),
            expected_digest=digest,
            expected_size=size_bytes,
        )

    def hash_file(self, path: str | Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        try:
            with Path(path).open("rb") as handle:
                while True:
                    chunk = handle.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
        except OSError as exc:
            raise BusinessError(E_IO, f"cannot read CAS object: {exc}", path=str(path)) from exc
        return size, digest.hexdigest()

    def verify(self, digest: str, expected_size: int | None = None) -> dict[str, object]:
        target = self.path_for(digest)
        if not target.is_file():
            raise BusinessError(
                E_OBJECT_MISSING,
                "CAS object does not exist",
                digest=digest,
                path=str(target),
            )
        size, actual = self.hash_file(target)
        if actual != digest or (expected_size is not None and size != expected_size):
            raise BusinessError(
                E_DIGEST_MISMATCH,
                "CAS object digest or size mismatch",
                expected_digest=digest,
                actual_digest=actual,
                expected_size=expected_size,
                actual_size=size,
                path=str(target),
            )
        return {"digest": actual, "size_bytes": size, "path": str(target)}

    def reclaim(self, digest: str) -> dict[str, object]:
        target = self.path_for(digest)
        if not target.exists():
            return {
                "digest": digest,
                "reclaimed_bytes": 0,
                "status": "already_missing",
                "path": str(target),
            }
        size, actual = self.hash_file(target)
        if actual != digest:
            # The object is already unreferenced. Remove the corrupt file and
            # account its physical size rather than claiming zero bytes.
            pass
        try:
            target.unlink()
            self._fsync_dir(target.parent)
        except FileNotFoundError:
            size = 0
        except OSError as exc:
            raise BusinessError(E_IO, f"cannot reclaim CAS object: {exc}", path=str(target)) from exc
        return {
            "digest": digest,
            "reclaimed_bytes": size,
            "status": "reclaimed",
            "path": str(target),
            "actual_digest": actual,
        }

    def iter_objects(self) -> Iterator[Path]:
        if not self.root.exists():
            return
        yield from sorted(path for path in self.root.rglob("*") if path.is_file())

    def clean_temp_files(self) -> int:
        removed = 0
        if not self.temp_dir.exists():
            return removed
        for path in sorted(self.temp_dir.iterdir()):
            if path.is_file():
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
