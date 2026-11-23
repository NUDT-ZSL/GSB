"""Thread-safe content-addressed DAG node cache."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from .errors import DomainError, E_CACHE_CORRUPT, E_RESOURCE_LIMIT
from .hashing import (
    atomic_write_bytes,
    atomic_write_json,
    decode_artifact,
    encode_artifact,
    sha256_bytes,
    sha256_json,
)


CACHE_SCHEMA = "rope-content-cache-v1"


@dataclass(frozen=True)
class NodeAccess:
    status: str
    node: str
    case_id: str
    variant: str
    key: str
    key_components: dict[str, Any]
    artifact_hash: str
    meta: dict[str, Any]
    arrays: dict[str, np.ndarray]
    artifact_path: str
    artifact_size: int

    @property
    def reused(self) -> bool:
        return self.status == "reused"

    def summary(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "variant": self.variant,
            "node": self.node,
            "key": self.key,
            "key_components": self.key_components,
            "artifact_hash": self.artifact_hash,
            "artifact_path": self.artifact_path,
            "artifact_size": self.artifact_size,
            "status": self.status,
        }


class ContentAddressedCache:
    """Persistent cache whose keys are independent of snapshot version."""

    def __init__(self, root: str | Path, max_entries: int = 4096):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects_root = self.root / "objects"
        self.objects_root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.json"
        self.max_entries = int(max_entries)
        if self.max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._lock = threading.RLock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._entries: dict[str, dict[str, Any]] = {}
        self._access_log: list[dict[str, Any]] = []
        self._reserved_keys: set[str] = set()
        self._next_event_sequence = 1
        self._load_manifest()

    def _load_manifest(self) -> None:
        if not self.manifest_path.exists():
            return
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise DomainError(
                E_CACHE_CORRUPT,
                f"cache manifest cannot be read: {exc}",
                {"case_id": "*", "variant": "*"},
            ) from exc
        if value.get("schema") != CACHE_SCHEMA:
            raise DomainError(
                E_CACHE_CORRUPT,
                "cache manifest schema is unsupported",
                {"case_id": "*", "variant": "*"},
            )
        entries = value.get("entries")
        access_log = value.get("access_log", [])
        if not isinstance(entries, dict) or not isinstance(access_log, list):
            raise DomainError(
                E_CACHE_CORRUPT,
                "cache manifest has an invalid shape",
                {"case_id": "*", "variant": "*"},
            )
        self._entries = entries
        self._access_log = access_log
        if self._access_log:
            self._next_event_sequence = (
                max(int(item.get("sequence", 0)) for item in self._access_log) + 1
            )

    def _persist_manifest(self) -> None:
        atomic_write_json(
            self.manifest_path,
            {
                "schema": CACHE_SCHEMA,
                "entries": self._entries,
                "access_log": self._access_log,
            },
        )

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def _object_path(self, key: str) -> Path:
        return self.objects_root / key[:2] / f"{key}.rpa"

    def _append_access(
        self,
        *,
        status: str,
        key: str,
        record: Mapping[str, Any],
        snapshot_version: int,
    ) -> None:
        self._access_log.append(
            {
                "sequence": self._next_event_sequence,
                "snapshot_version": snapshot_version,
                "status": status,
                "key": key,
                "artifact_hash": record["artifact_hash"],
                "case_id": record["case_id"],
                "variant": record["variant"],
                "node": record["node"],
            }
        )
        self._next_event_sequence += 1

    def get_or_compute(
        self,
        *,
        case_id: str,
        variant: str,
        node: str,
        key_components: Mapping[str, Any],
        snapshot_version: int,
        compute: Callable[[], tuple[Mapping[str, Any], Mapping[str, np.ndarray]]],
    ) -> NodeAccess:
        key = sha256_json(key_components)
        lock = self._key_lock(key)
        with lock:
            with self._lock:
                record = self._entries.get(key)
            if record is not None:
                artifact_path = Path(record["artifact_path"])
                try:
                    blob = artifact_path.read_bytes()
                    actual_hash = sha256_bytes(blob)
                    if actual_hash == record["artifact_hash"]:
                        meta, arrays = decode_artifact(blob)
                        with self._lock:
                            self._append_access(
                                status="reused",
                                key=key,
                                record=record,
                                snapshot_version=snapshot_version,
                            )
                            self._persist_manifest()
                        return NodeAccess(
                            status="reused",
                            node=node,
                            case_id=case_id,
                            variant=variant,
                            key=key,
                            key_components=dict(key_components),
                            artifact_hash=record["artifact_hash"],
                            meta=meta,
                            arrays=arrays,
                            artifact_path=str(artifact_path),
                            artifact_size=len(blob),
                        )
                except Exception:
                    pass
                # A torn or externally corrupted object is discarded and
                # deterministically rebuilt from its key.
                with self._lock:
                    self._entries.pop(key, None)
                    self._persist_manifest()

            with self._lock:
                entry_count = len(self._entries)
                if len(self._entries) + len(self._reserved_keys) >= self.max_entries:
                    raise DomainError(
                        E_RESOURCE_LIMIT,
                        "content-addressed cache entry limit would be exceeded",
                        {
                            "case_id": case_id,
                            "variant": variant,
                            "node": node,
                            "limit": self.max_entries,
                            "current_entries": entry_count,
                            "reserved_entries": len(self._reserved_keys),
                            "degraded_category": "CACHE_ENTRY_LIMIT",
                        },
                    )
                self._reserved_keys.add(key)

            try:
                meta, arrays = compute()
                blob = encode_artifact(meta, arrays)
                artifact_hash = sha256_bytes(blob)
                artifact_path = self._object_path(key)
                atomic_write_bytes(artifact_path, blob)
                record = {
                    "case_id": case_id,
                    "variant": variant,
                    "node": node,
                    "key_components": dict(key_components),
                    "artifact_hash": artifact_hash,
                    "artifact_path": str(artifact_path),
                    "artifact_size": len(blob),
                    "upstream_hashes": dict(
                        key_components.get("upstream_hashes", {})
                    ),
                    "block_content_hash": key_components.get("block_content_hash"),
                }
            finally:
                with self._lock:
                    self._reserved_keys.discard(key)
            with self._lock:
                self._entries[key] = record
                self._append_access(
                    status="recomputed",
                    key=key,
                    record=record,
                    snapshot_version=snapshot_version,
                )
                self._persist_manifest()
            return NodeAccess(
                status="recomputed",
                node=node,
                case_id=case_id,
                variant=variant,
                key=key,
                key_components=dict(key_components),
                artifact_hash=artifact_hash,
                meta=meta,
                arrays=arrays,
                artifact_path=str(artifact_path),
                artifact_size=len(blob),
            )

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def diagnose(
        self,
        case_id: str | None = None,
        variant: str | None = None,
        node: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            records = [
                dict(record)
                for record in self._entries.values()
                if (case_id is None or record.get("case_id") == case_id)
                and (variant is None or record.get("variant") == variant)
                and (node is None or record.get("node") == node)
            ]
            access_log = [
                dict(event)
                for event in self._access_log
                if (case_id is None or event.get("case_id") == case_id)
                and (variant is None or event.get("variant") == variant)
                and (node is None or event.get("node") == node)
            ]
        records.sort(key=lambda item: (item["case_id"], item["variant"], item["node"], item["artifact_hash"]))
        access_log.sort(key=lambda item: int(item["sequence"]))
        return {
            "query": {"case_id": case_id, "variant": variant, "node": node},
            "count": len(records),
            "entries": records,
            "access_history": access_log,
            "entry_limit": self.max_entries,
            "current_entry_count": self.entry_count,
        }

    def object_keys(self) -> set[str]:
        return set(self._entries)

    def query(
        self,
        case_id: str | None = None,
        variant: str | None = None,
        node: str | None = None,
    ) -> dict[str, Any]:
        return self.diagnose(case_id=case_id, variant=variant, node=node)

    def cleanup_orphan_objects(self) -> int:
        """Remove object files not referenced by the durable manifest."""

        removed = 0
        with self._lock:
            known_paths = {
                Path(record["artifact_path"]).resolve()
                for record in self._entries.values()
            }
        for path in self.objects_root.glob("*/*.rpa"):
            if path.resolve() not in known_paths:
                try:
                    os.unlink(path)
                    removed += 1
                except FileNotFoundError:
                    pass
        return removed
