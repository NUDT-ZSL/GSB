"""Content-addressed multi-stage cache with stale dependency rejection."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .codec import (
    canonical_bytes,
    canonical_hash,
    decode_value,
    sha256_bytes,
)
from .errors import E_CACHE_CORRUPT, E_STALE_RESULT, MoeError


def build_cache_key(
    *,
    kind: str,
    case_id: str,
    block_index: int,
    dependencies: dict[str, Any],
) -> str:
    return canonical_hash(
        {
            "kind": kind,
            "case_id": case_id,
            "block_index": block_index,
            "dependencies": dependencies,
        }
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class ContentAddressedCache:
    """Disk-backed CAS for gate, assignment, block output, and block metadata."""

    KINDS = (
        "gate_distribution",
        "expert_assignment",
        "block_output",
        "block_metadata",
    )

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.entries_dir = self.root / "entries"
        self.index_dir = self.root / "index"
        for directory in (self.objects_dir, self.entries_dir, self.index_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _case_key(case_id: str) -> str:
        return sha256_bytes(case_id.encode("utf-8"))[:24]

    @staticmethod
    def _split_dir(root: Path, digest: str) -> Path:
        return root / digest[:2] / digest[2:4]

    def _object_path(self, content_hash: str) -> Path:
        return self._split_dir(self.objects_dir, content_hash) / f"{content_hash}.json"

    def _entry_path(self, key: str) -> Path:
        return self._split_dir(self.entries_dir, key) / f"{key}.json"

    def _index_path(self, case_id: str, block_index: int, kind: str) -> Path:
        return (
            self.index_dir
            / self._case_key(case_id)
            / f"block-{block_index:08d}"
            / f"{kind}.json"
        )

    def store_artifact(
        self,
        *,
        kind: str,
        case_id: str,
        block_index: int,
        dependencies: dict[str, Any],
        artifact: dict[str, Any],
        reused_from: str | None = None,
    ) -> dict[str, Any]:
        if kind not in self.KINDS:
            raise ValueError(f"unknown cache artifact kind: {kind}")
        payload = canonical_bytes(artifact)
        content_hash = sha256_bytes(payload)
        key = build_cache_key(
            kind=kind,
            case_id=case_id,
            block_index=block_index,
            dependencies=dependencies,
        )
        object_path = self._object_path(content_hash)
        if object_path.exists():
            existing = object_path.read_bytes()
            if sha256_bytes(existing) != content_hash:
                raise MoeError(
                    code=E_CACHE_CORRUPT,
                    message="content-addressed object hash verification failed",
                    case_id=case_id,
                    block_index=block_index,
                    details={"content_hash": content_hash},
                )
        else:
            _atomic_write(object_path, payload)

        entry_path = self._entry_path(key)
        existing_entry: dict[str, Any] | None = None
        if entry_path.exists():
            try:
                existing_entry = json.loads(entry_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise MoeError(
                    code=E_CACHE_CORRUPT,
                    message="cache entry is not valid JSON",
                    case_id=case_id,
                    block_index=block_index,
                    details={"key": key},
                ) from exc
            if existing_entry.get("content_hash") != content_hash:
                raise MoeError(
                    code=E_CACHE_CORRUPT,
                    message="same cache key produced different content",
                    case_id=case_id,
                    block_index=block_index,
                    details={
                        "key": key,
                        "existing_content_hash": existing_entry.get("content_hash"),
                        "new_content_hash": content_hash,
                    },
                )
        entry = {
            "key": key,
            "kind": kind,
            "case_id": case_id,
            "block_index": block_index,
            "content_hash": content_hash,
            "dependencies": dependencies,
            "reused_from": reused_from,
            "created_ns": time.time_ns(),
        }
        _atomic_write(entry_path, canonical_bytes(entry))
        index_record = {
            "key": key,
            "kind": kind,
            "case_id": case_id,
            "block_index": block_index,
            "content_hash": content_hash,
            "dependencies_digest": canonical_hash(dependencies),
            "reused_from": reused_from,
        }
        _atomic_write(self._index_path(case_id, block_index, kind), canonical_bytes(index_record))
        return dict(index_record)

    def _load_entry(self, key: str) -> dict[str, Any]:
        path = self._entry_path(key)
        if not path.exists():
            raise MoeError(
                code=E_STALE_RESULT,
                message="cache entry does not exist for the requested key",
                details={"key": key},
            )
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MoeError(
                code=E_CACHE_CORRUPT,
                message="cache entry is not valid JSON",
                details={"key": key},
            ) from exc
        expected_key = build_cache_key(
            kind=entry["kind"],
            case_id=entry["case_id"],
            block_index=int(entry["block_index"]),
            dependencies=entry["dependencies"],
        )
        if expected_key != key:
            raise MoeError(
                code=E_CACHE_CORRUPT,
                message="cache entry key does not match its dependencies",
                case_id=entry.get("case_id"),
                block_index=entry.get("block_index"),
                details={"actual_key": key, "expected_key": expected_key},
            )
        return entry

    def load_by_key(self, key: str) -> dict[str, Any]:
        entry = self._load_entry(key)
        object_path = self._object_path(entry["content_hash"])
        if not object_path.exists():
            raise MoeError(
                code=E_CACHE_CORRUPT,
                message="content-addressed object is missing",
                case_id=entry.get("case_id"),
                block_index=entry.get("block_index"),
                details={"key": key, "content_hash": entry["content_hash"]},
            )
        payload = object_path.read_bytes()
        if sha256_bytes(payload) != entry["content_hash"]:
            raise MoeError(
                code=E_CACHE_CORRUPT,
                message="content-addressed object failed verification",
                case_id=entry.get("case_id"),
                block_index=entry.get("block_index"),
                details={"key": key, "content_hash": entry["content_hash"]},
            )
        try:
            encoded_artifact = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MoeError(
                code=E_CACHE_CORRUPT,
                message="cached artifact is not valid canonical JSON",
                case_id=entry.get("case_id"),
                block_index=entry.get("block_index"),
            ) from exc
        return {
            "entry": entry,
            "artifact": decode_value(encoded_artifact),
        }

    def latest_index(
        self, case_id: str, block_index: int, kind: str
    ) -> dict[str, Any] | None:
        path = self._index_path(case_id, block_index, kind)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MoeError(
                code=E_CACHE_CORRUPT,
                message="cache index is not valid JSON",
                case_id=case_id,
                block_index=block_index,
                details={"kind": kind},
            ) from exc

    def load_checked_artifact(
        self,
        *,
        case_id: str,
        block_index: int,
        kind: str,
        expected_dependencies: dict[str, Any],
    ) -> dict[str, Any] | None:
        index_record = self.latest_index(case_id, block_index, kind)
        if index_record is None:
            return None
        expected_key = build_cache_key(
            kind=kind,
            case_id=case_id,
            block_index=block_index,
            dependencies=expected_dependencies,
        )
        actual_key = index_record["key"]
        if actual_key != expected_key:
            raise MoeError(
                code=E_STALE_RESULT,
                message="cached result was produced from an obsolete dependency key",
                case_id=case_id,
                block_index=block_index,
                details={
                    "kind": kind,
                    "actual_key": actual_key,
                    "expected_key": expected_key,
                    "actual_dependencies_digest": index_record.get(
                        "dependencies_digest"
                    ),
                    "expected_dependencies_digest": canonical_hash(
                        expected_dependencies
                    ),
                },
            )
        loaded = self.load_by_key(actual_key)
        if loaded["entry"]["content_hash"] != index_record.get("content_hash"):
            raise MoeError(
                code=E_CACHE_CORRUPT,
                message="cache index content hash does not match its entry",
                case_id=case_id,
                block_index=block_index,
                details={
                    "kind": kind,
                    "index_content_hash": index_record.get("content_hash"),
                    "entry_content_hash": loaded["entry"]["content_hash"],
                },
            )
        if loaded["entry"]["dependencies"] != expected_dependencies:
            raise MoeError(
                code=E_STALE_RESULT,
                message="cached result dependencies do not match the request",
                case_id=case_id,
                block_index=block_index,
                details={
                    "kind": kind,
                    "actual_key": actual_key,
                    "expected_key": expected_key,
                },
            )
        return loaded

    def verify_all_objects(self) -> dict[str, Any]:
        checked = 0
        bad: list[dict[str, str]] = []
        for path in sorted(self.objects_dir.rglob("*.json")):
            payload = path.read_bytes()
            actual = sha256_bytes(payload)
            expected = path.stem
            checked += 1
            if actual != expected:
                bad.append({"path": str(path), "expected": expected, "actual": actual})
        return {"checked_objects": checked, "bad_objects": bad, "ok": not bad}
