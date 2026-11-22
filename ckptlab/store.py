from __future__ import annotations

import itertools
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from .errors import BusinessError, fail
from .util import sha256_hex


class FileLock:
    """Cross-process advisory lock with Windows and POSIX implementations."""

    def __init__(self, path: Path):
        self.path = path
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open("a+b")
        try:
            if file.tell() == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            file.close()
            fail("FILE_LOCK_TIMEOUT", "获取文件锁失败", path=str(self.path), reason=str(exc))
        self._file = file

    def release(self) -> None:
        file = self._file
        if file is None:
            return
        try:
            file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()
            self._file = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class ObjectStore:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.manifests_dir = self.root / "manifests"
        self.locks_dir = self.root / ".locks"
        self._counter = itertools.count()
        self._counter_lock = threading.Lock()

    def prepare(self) -> None:
        for directory in (self.root, self.objects_dir, self.manifests_dir, self.locks_dir):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                fail("IO_ERROR", "无法创建存储目录", path=str(directory), reason=str(exc))

    @contextmanager
    def checkpoint_lock(self) -> Iterator[None]:
        self.prepare()
        with FileLock(self.locks_dir / "checkpoint.lock"):
            yield

    @contextmanager
    def object_lock(self, digest: str) -> Iterator[None]:
        with FileLock(self.locks_dir / f"object-{digest}.lock"):
            yield

    def _next_temp_path(self, directory: Path, prefix: str) -> Path:
        with self._counter_lock:
            serial = next(self._counter)
        return directory / (
            f".tmp-{prefix}-{os.getpid()}-{threading.get_ident()}-{serial}"
        )

    def _fsync_directory(self, directory: Path) -> None:
        if os.name == "nt":
            return
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _atomic_write(
        self,
        dest: Path,
        data: bytes,
        *,
        tag: str,
        before_rename=None,
    ) -> None:
        directory = dest.parent
        directory.mkdir(parents=True, exist_ok=True)
        tmp = self._next_temp_path(directory, tag)
        try:
            with tmp.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
                if before_rename is not None:
                    before_rename(tmp)
            os.replace(tmp, dest)
            self._fsync_directory(directory)
        except BusinessError:
            raise
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            fail(
                "IO_ERROR",
                "原子写入失败",
                path=str(dest),
                temp_path=str(tmp),
                reason=str(exc),
            )

    def _read_verified(self, path: Path, digest: str, kind: str) -> bytes:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            code = "SHARD_MISSING" if kind == "shard" else "MANIFEST_MISSING"
            fail(
                code,
                f"{kind} 对象缺失",
                path=str(path),
                digest=digest,
                expected_digest=digest,
            )
        except OSError as exc:
            fail("IO_ERROR", "读取对象失败", path=str(path), reason=str(exc))
        actual = sha256_hex(data)
        if actual != digest:
            code = "SHARD_DIGEST_MISMATCH" if kind == "shard" else "MANIFEST_DIGEST_MISMATCH"
            fail(
                code,
                f"{kind} 摘要不匹配",
                path=str(path),
                expected_digest=digest,
                actual_digest=actual,
            )
        return data

    def object_path(self, digest: str) -> Path:
        return self.objects_dir / f"{digest}.shard"

    def manifest_path(self, digest: str) -> Path:
        return self.manifests_dir / f"{digest}.json"

    def has_valid_object(self, digest: str) -> bool:
        path = self.object_path(digest)
        try:
            if not path.is_file():
                return False
            return sha256_hex(path.read_bytes()) == digest
        except OSError:
            return False

    def write_object(
        self,
        data: bytes,
        digest: str,
        *,
        version: int,
        before_rename=None,
    ) -> bool:
        if sha256_hex(data) != digest:
            fail("OBJECT_CORRUPT", "待写分片内容与摘要不一致", digest=digest)
        dest = self.object_path(digest)
        with self.object_lock(digest):
            if dest.is_file():
                try:
                    existing = dest.read_bytes()
                except OSError:
                    existing = b""
                if sha256_hex(existing) == digest:
                    return False
                fail(
                    "OBJECT_CORRUPT",
                    "同名内容寻址对象已存在但不匹配",
                    path=str(dest),
                    digest=digest,
                )
            self._atomic_write(
                dest,
                data,
                tag=f"v{version}-object",
                before_rename=before_rename,
            )
            return True

    def read_object(self, digest: str) -> bytes:
        return self._read_verified(self.object_path(digest), digest, "shard")

    def write_manifest(
        self,
        data: bytes,
        digest: str,
        *,
        version: int,
        before_rename=None,
    ) -> bool:
        if sha256_hex(data) != digest:
            fail("OBJECT_CORRUPT", "待写清单内容与摘要不一致", digest=digest)
        dest = self.manifest_path(digest)
        with self.object_lock(digest):
            if dest.is_file():
                try:
                    existing = dest.read_bytes()
                except OSError:
                    existing = b""
                if sha256_hex(existing) == digest:
                    return False
                fail(
                    "OBJECT_CORRUPT",
                    "同名清单对象已存在但不匹配",
                    path=str(dest),
                    digest=digest,
                )
            self._atomic_write(
                dest,
                data,
                tag=f"v{version}-manifest",
                before_rename=before_rename,
            )
            return True

    def read_manifest(self, digest: str) -> bytes:
        return self._read_verified(self.manifest_path(digest), digest, "manifest")

    def cleanup_version_temp_files(self, version: int) -> list[str]:
        removed: list[str] = []
        for directory in (self.objects_dir, self.manifests_dir):
            for prefix in (f".tmp-v{version}-", f".tmp-manifest-v{version}-"):
                for path in sorted(directory.glob(prefix + "*")):
                    try:
                        path.unlink()
                        removed.append(str(path))
                    except OSError:
                        pass
        return removed
