from __future__ import annotations

import contextlib
import os
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

import numpy as np

from .errors import CacheCorruptError, ErrorCode, Issue, ResourceLimitError
from .hashing import (
    array_content_hash,
    canonical_array,
    canonical_json,
    sha256_bytes,
)


MAGIC = b"FACACHE1"
FORMAT_VERSION = 1


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    return value


@dataclass
class CacheEntry:
    cache_key: str
    key_version: int
    content_hash: str
    checksum: str
    arrays: dict[str, np.ndarray]
    metadata: dict[str, Any]
    source_node_id: str | None = None
    wrote: bool = False


@dataclass
class CacheDiagnostics:
    cache_dir: str
    entries: int
    valid_entries: int
    corrupt_entries: int
    total_bytes: int
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.corrupt_entries == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_dir": self.cache_dir,
            "entries": self.entries,
            "valid_entries": self.valid_entries,
            "corrupt_entries": self.corrupt_entries,
            "total_bytes": self.total_bytes,
            "ok": self.ok,
            "issues": [issue.to_dict() for issue in self.issues],
        }


class CacheStore:
    """Atomic, content-addressed snapshots with per-key write serialization."""

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        max_entries: int | None = None,
        lock_timeout_s: float = 10.0,
        lock_stale_s: float = 60.0,
    ):
        self.cache_dir = Path(cache_dir)
        self.entries_dir = self.cache_dir / "entries"
        self.versions_dir = self.cache_dir / "versions"
        self.locks_dir = self.cache_dir / "locks"
        self.tmp_dir = self.cache_dir / "tmp"
        for directory in (
            self.cache_dir,
            self.entries_dir,
            self.versions_dir,
            self.locks_dir,
            self.tmp_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.max_entries = max_entries
        self.lock_timeout_s = lock_timeout_s
        self.lock_stale_s = lock_stale_s
        self._thread_locks: dict[str, threading.Lock] = {}
        self._thread_locks_guard = threading.Lock()
        self._entry_count_thread_lock = threading.Lock()

    def _thread_lock(self, key: str) -> threading.Lock:
        with self._thread_locks_guard:
            return self._thread_locks.setdefault(key, threading.Lock())

    def _paths(self, key: str) -> tuple[Path, Path, Path]:
        return (
            self.entries_dir / f"{key}.bin",
            self.versions_dir / f"{key}.version",
            self.locks_dir / f"{key}.lock",
        )

    @contextlib.contextmanager
    def _key_lock(self, key: str) -> Iterator[None]:
        local_lock = self._thread_lock(key)
        if not local_lock.acquire(timeout=self.lock_timeout_s):
            raise ResourceLimitError(
                Issue(
                    ErrorCode.RESOURCE_LIMIT,
                    "timed out acquiring in-process cache lock",
                    details={"cache_key": key, "lock_kind": "thread"},
                )
            )
        _, _, lock_path = self._paths(key)
        deadline = time.monotonic() + self.lock_timeout_s
        acquired = False
        try:
            while True:
                try:
                    os.mkdir(lock_path)
                    acquired = True
                    break
                except FileExistsError:
                    try:
                        age = time.time() - lock_path.stat().st_mtime
                    except FileNotFoundError:
                        continue
                    if age > self.lock_stale_s:
                        try:
                            os.rmdir(lock_path)
                        except OSError:
                            pass
                        continue
                    if time.monotonic() >= deadline:
                        raise ResourceLimitError(
                            Issue(
                                ErrorCode.RESOURCE_LIMIT,
                                "timed out acquiring cross-process cache lock",
                                details={
                                    "cache_key": key,
                                    "lock_kind": "file",
                                    "lock_path": str(lock_path),
                                },
                            )
                        )
                    time.sleep(0.005)
            yield
        finally:
            if acquired:
                try:
                    os.rmdir(lock_path)
                except OSError:
                    pass
            local_lock.release()

    @contextlib.contextmanager
    def _entry_count_lock(self) -> Iterator[None]:
        if not self._entry_count_thread_lock.acquire(
            timeout=self.lock_timeout_s
        ):
            raise ResourceLimitError(
                Issue(
                    ErrorCode.RESOURCE_LIMIT,
                    "timed out acquiring global cache entry lock",
                    details={"lock_kind": "entry_count_thread"},
                )
            )
        lock_path = self.locks_dir / "__entry_count.lock"
        deadline = time.monotonic() + self.lock_timeout_s
        acquired = False
        try:
            while True:
                try:
                    os.mkdir(lock_path)
                    acquired = True
                    break
                except FileExistsError:
                    try:
                        age = time.time() - lock_path.stat().st_mtime
                    except FileNotFoundError:
                        continue
                    if age > self.lock_stale_s:
                        try:
                            os.rmdir(lock_path)
                        except OSError:
                            pass
                        continue
                    if time.monotonic() >= deadline:
                        raise ResourceLimitError(
                            Issue(
                                ErrorCode.RESOURCE_LIMIT,
                                "timed out acquiring global cache entry lock",
                                details={
                                    "lock_kind": "entry_count_file",
                                    "lock_path": str(lock_path),
                                },
                            )
                        )
                    time.sleep(0.005)
            yield
        finally:
            if acquired:
                try:
                    os.rmdir(lock_path)
                except OSError:
                    pass
            self._entry_count_thread_lock.release()

    def _read_version(self, key: str) -> int | None:
        _, version_path, _ = self._paths(key)
        if not version_path.exists():
            return None
        try:
            return int(version_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    def _write_version(self, key: str, version: int) -> None:
        _, version_path, _ = self._paths(key)
        fd, temp_name = tempfile.mkstemp(
            prefix=f"{key}.",
            suffix=".version.tmp",
            dir=self.tmp_dir,
        )
        try:
            with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
                handle.write(f"{version}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, version_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _serialize(
        self,
        key: str,
        version: int,
        content_hash: str,
        arrays: Mapping[str, np.ndarray],
        metadata: Mapping[str, Any],
        source_node_id: str | None,
    ) -> bytes:
        descriptors: list[dict[str, Any]] = []
        data_chunks: list[bytes] = []
        for name in sorted(arrays):
            array = canonical_array(arrays[name])
            data = array.tobytes(order="C")
            descriptors.append(
                {
                    "name": name,
                    "dtype": array.dtype.str,
                    "shape": list(array.shape),
                    "nbytes": len(data),
                }
            )
            data_chunks.append(data)
        data = b"".join(data_chunks)
        core = {
            "format_version": FORMAT_VERSION,
            "cache_key": key,
            "key_version": int(version),
            "content_hash": content_hash,
            "metadata": _json_safe(metadata),
            "source_node_id": source_node_id,
            "arrays": descriptors,
        }
        checksum = sha256_bytes(canonical_json(core) + b"\x00" + data)
        header = dict(core)
        header["checksum"] = checksum
        header_bytes = canonical_json(header)
        return b"".join(
            (
                MAGIC,
                struct.pack("<Q", len(header_bytes)),
                header_bytes,
                data,
            )
        )

    def _parse_entry_bytes(
        self,
        key: str,
        blob: bytes,
        *,
        expected_version: int | None = None,
    ) -> CacheEntry:
        prefix_size = len(MAGIC) + 8
        if len(blob) < prefix_size or not blob.startswith(MAGIC):
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    "cache entry has invalid magic or truncated prefix",
                    details={"cache_key": key, "category": "magic"},
                )
            )
        (header_len,) = struct.unpack("<Q", blob[len(MAGIC) : prefix_size])
        header_end = prefix_size + header_len
        if header_end > len(blob):
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    "cache entry header length exceeds file size",
                    details={"cache_key": key, "category": "header_length"},
                )
            )
        try:
            import json

            header = json.loads(blob[prefix_size:header_end].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    f"cache entry header cannot be parsed: {exc}",
                    details={"cache_key": key, "category": "header_json"},
                )
            ) from exc
        if not isinstance(header, dict):
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    "cache entry header must be an object",
                    details={"cache_key": key, "category": "header_type"},
                )
            )
        checksum = header.pop("checksum", None)
        data = blob[header_end:]
        try:
            expected_checksum = sha256_bytes(
                canonical_json(header) + b"\x00" + data
            )
        except (TypeError, ValueError) as exc:
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    "cache entry header is not canonicalizable",
                    details={"cache_key": key, "category": "header_content"},
                )
            ) from exc
        if checksum != expected_checksum:
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    "cache entry checksum mismatch",
                    details={"cache_key": key, "category": "checksum"},
                )
            )
        if header.get("format_version") != FORMAT_VERSION:
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    "cache entry format version is unsupported",
                    details={"cache_key": key, "category": "format_version"},
                )
            )
        if header.get("cache_key") != key:
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    "cache entry key does not match its path",
                    details={"cache_key": key, "category": "key_mismatch"},
                )
            )
        try:
            key_version = int(header.get("key_version", -1))
        except (TypeError, ValueError) as exc:
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    "cache entry key version is invalid",
                    details={"cache_key": key, "category": "key_version_type"},
                )
            ) from exc
        if expected_version is not None and key_version != expected_version:
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    "cache entry key version does not match version marker",
                    details={
                        "cache_key": key,
                        "category": "key_version",
                        "entry_version": key_version,
                        "marker_version": expected_version,
                    },
                )
            )
        arrays: dict[str, np.ndarray] = {}
        offset = 0
        try:
            for descriptor in header.get("arrays", []):
                name = str(descriptor["name"])
                dtype = np.dtype(descriptor["dtype"])
                shape = tuple(int(item) for item in descriptor["shape"])
                nbytes = int(descriptor["nbytes"])
                if nbytes < 0 or offset + nbytes > len(data):
                    raise ValueError("array descriptor exceeds payload")
                if any(dim < 0 for dim in shape):
                    raise ValueError("array shape cannot be negative")
                raw = data[offset : offset + nbytes]
                array = np.frombuffer(raw, dtype=dtype)
                if array.size * array.dtype.itemsize != nbytes:
                    raise ValueError("array byte count does not match descriptor")
                arrays[name] = array.reshape(shape).copy(order="C")
                offset += nbytes
        except (KeyError, TypeError, ValueError) as exc:
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    f"cache entry array descriptors are invalid: {exc}",
                    details={"cache_key": key, "category": "array_descriptor"},
                )
            ) from exc
        if offset != len(data):
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    "cache entry contains trailing or missing payload bytes",
                    details={"cache_key": key, "category": "payload_length"},
                )
            )
        metadata = header.get("metadata", {})
        if not isinstance(metadata, dict):
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    "cache entry metadata must be an object",
                    details={"cache_key": key, "category": "metadata_type"},
                )
            )
        try:
            actual_content_hash = array_content_hash(arrays, metadata)
        except Exception as exc:
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    f"cache entry content cannot be hashed: {exc}",
                    details={
                        "cache_key": key,
                        "category": "content_hash_type",
                    },
                )
            ) from exc
        if actual_content_hash != header.get("content_hash"):
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    "cache entry content hash mismatch",
                    details={"cache_key": key, "category": "content_hash"},
                )
            )
        return CacheEntry(
            cache_key=key,
            key_version=key_version,
            content_hash=actual_content_hash,
            checksum=expected_checksum,
            arrays=arrays,
            metadata=metadata,
            source_node_id=header.get("source_node_id"),
            wrote=False,
        )

    def _load_locked(self, key: str) -> CacheEntry | None:
        entry_path, _, _ = self._paths(key)
        if not entry_path.exists():
            return None
        marker_version = self._read_version(key)
        if marker_version is None:
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    "cache entry has no valid key version marker",
                    details={"cache_key": key, "category": "version_marker"},
                )
            )
        try:
            blob = entry_path.read_bytes()
        except OSError as exc:
            raise CacheCorruptError(
                Issue(
                    ErrorCode.CACHE_CORRUPT,
                    f"cache entry cannot be read: {exc}",
                    details={"cache_key": key, "category": "io"},
                )
            ) from exc
        return self._parse_entry_bytes(
            key,
            blob,
            expected_version=marker_version,
        )

    def get(self, key: str) -> CacheEntry | None:
        with self._key_lock(key):
            return self._load_locked(key)

    def put(
        self,
        key: str,
        arrays: Mapping[str, np.ndarray],
        metadata: Mapping[str, Any],
        *,
        source_node_id: str | None = None,
        content_hash: str | None = None,
    ) -> CacheEntry:
        canonical_arrays = {
            str(name): canonical_array(np.asarray(array))
            for name, array in arrays.items()
        }
        normalized_metadata = _json_safe(dict(metadata))
        computed_content_hash = array_content_hash(
            canonical_arrays,
            normalized_metadata,
        )
        if (
            content_hash is not None
            and content_hash != computed_content_hash
        ):
            raise ValueError(
                "provided content_hash does not match arrays and metadata"
            )
        actual_content_hash = computed_content_hash
        with self._key_lock(key):
            entry_path, _, _ = self._paths(key)
            existing: CacheEntry | None = None
            if entry_path.exists():
                try:
                    existing = self._load_locked(key)
                except CacheCorruptError:
                    existing = None
                if (
                    existing is not None
                    and existing.content_hash == actual_content_hash
                ):
                    return existing
            marker_version = self._read_version(key) or 0
            if existing is not None:
                marker_version = max(marker_version, existing.key_version)
            version = marker_version + 1
            blob = self._serialize(
                key,
                version,
                actual_content_hash,
                canonical_arrays,
                normalized_metadata,
                source_node_id,
            )
            def commit() -> None:
                fd, temp_name = tempfile.mkstemp(
                    prefix=f"{key}.",
                    suffix=".entry.tmp",
                    dir=self.tmp_dir,
                )
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(blob)
                        handle.flush()
                        os.fsync(handle.fileno())
                    self._write_version(key, version)
                    os.replace(temp_name, entry_path)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)

            if self.max_entries is not None:
                with self._entry_count_lock():
                    if not entry_path.exists() and (
                        self.count_entries() >= self.max_entries
                    ):
                        raise ResourceLimitError(
                            Issue(
                                ErrorCode.RESOURCE_LIMIT,
                                "cache entry limit exceeded",
                                category="cache_entries",
                                details={
                                    "max_cache_entries": self.max_entries,
                                    "cache_key": key,
                                },
                            )
                        )
                    commit()
            else:
                commit()
            written = self._parse_entry_bytes(
                key,
                self._paths(key)[0].read_bytes(),
                expected_version=version,
            )
            written.wrote = True
            return written

    def count_entries(self) -> int:
        return sum(1 for _ in self.entries_dir.glob("*.bin"))

    def diagnose(self) -> CacheDiagnostics:
        valid = 0
        corrupt = 0
        total_bytes = 0
        issues: list[Issue] = []
        paths = sorted(self.entries_dir.glob("*.bin"))
        for path in paths:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
            key = path.stem
            try:
                entry = self.get(key)
                if entry is None:
                    raise CacheCorruptError(
                        Issue(
                            ErrorCode.CACHE_CORRUPT,
                            "cache entry disappeared during diagnostics",
                            details={"cache_key": key, "category": "missing"},
                        )
                    )
                valid += 1
            except CacheCorruptError as exc:
                corrupt += 1
                issue = exc.issue
                issue.details.setdefault("cache_key", key)
                issue.details.setdefault("path", str(path))
                issues.append(issue)
            except ResourceLimitError as exc:
                corrupt += 1
                issues.append(exc.issue)
        return CacheDiagnostics(
            cache_dir=str(self.cache_dir),
            entries=len(paths),
            valid_entries=valid,
            corrupt_entries=corrupt,
            total_bytes=total_bytes,
            issues=issues,
        )

    def clear(self) -> None:
        for directory in (self.entries_dir, self.versions_dir):
            for path in directory.glob("*"):
                if path.is_file():
                    path.unlink()
