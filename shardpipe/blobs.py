from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode
from .models import CacheKey
from .util import atomic_write_bytes, canonical_json, pretty_json, sha256_bytes, sha256_value


class BlobStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.cache_dir = state_dir / "cache"
        self.blob_dir = self.cache_dir / "blobs"
        self.manifest_dir = self.cache_dir / "manifests"
        self.tmp_dir = self.cache_dir / "tmp"
        self.orphan_dir = self.cache_dir / "orphans"
        for directory in (
            self.blob_dir,
            self.manifest_dir,
            self.tmp_dir,
            self.orphan_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_hash(key: CacheKey) -> str:
        return sha256_value(
            {
                "dataset": key.dataset,
                "shard_index": key.shard_index,
                "cache_generation": key.cache_generation,
                "generation_fingerprint": key.generation_fingerprint,
            }
        )

    def blob_path(self, key: CacheKey) -> Path:
        return self.blob_dir / f"{self.key_hash(key)}.bin"

    def manifest_path(self, key: CacheKey) -> Path:
        return self.manifest_dir / f"{self.key_hash(key)}.json"

    def write(self, key: CacheKey, data: bytes, digest: str) -> dict[str, Any]:
        actual_digest = sha256_bytes(data)
        if actual_digest != digest:
            raise BusinessError(
                ErrorCode.CACHE_FILE_CORRUPT,
                "generated shard digest changed before it was persisted",
                details={"expected": digest, "actual": actual_digest},
            )
        manifest = {
            "schema": 1,
            "key": key.as_dict(),
            "key_hash": self.key_hash(key),
            "digest": digest,
            "bytes": len(data),
            "blob": f"blobs/{self.key_hash(key)}.bin",
        }
        atomic_write_bytes(self.blob_path(key), data)
        atomic_write_bytes(
            self.manifest_path(key),
            (pretty_json(manifest) + "\n").encode("utf-8"),
        )
        if os.environ.get("SHARDPIPE_FAILPOINT") == "after_blob_write":
            os._exit(92)
        return manifest

    def read(self, key: CacheKey) -> tuple[bytes, dict[str, Any]] | None:
        blob_path = self.blob_path(key)
        manifest_path = self.manifest_path(key)
        if not blob_path.exists() or not manifest_path.exists():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(
                ErrorCode.CACHE_FILE_CORRUPT,
                "cannot read cache manifest",
                details={"key": key.as_dict()},
            ) from exc
        if manifest.get("key") != key.as_dict():
            raise BusinessError(
                ErrorCode.CACHE_FILE_CORRUPT,
                "cache manifest key does not match its path",
                details={"key": key.as_dict()},
            )
        data = blob_path.read_bytes()
        digest = sha256_bytes(data)
        if digest != manifest.get("digest") or len(data) != manifest.get("bytes"):
            raise BusinessError(
                ErrorCode.CACHE_FILE_CORRUPT,
                "cache blob does not match its manifest",
                details={"key": key.as_dict()},
            )
        return data, manifest

    def iter_manifests(self) -> list[tuple[CacheKey, dict[str, Any]]]:
        result: list[tuple[CacheKey, dict[str, Any]]] = []
        for path in sorted(self.manifest_dir.glob("*.json")):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                raw_key = manifest["key"]
                key = CacheKey(
                    dataset=raw_key["dataset"],
                    shard_index=int(raw_key["shard_index"]),
                    cache_generation=int(raw_key["cache_generation"]),
                    generation_fingerprint=raw_key["generation_fingerprint"],
                )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            result.append((key, manifest))
        return result

    def quarantine_unpaired(self, clock: int) -> list[dict[str, Any]]:
        """Best-effort cleanup must not race an active cache commit.

        A blob is renamed into place before its sidecar manifest and before
        the SQLite content row.  Therefore another scheduler process may
        legitimately observe a short "blob without manifest" window.  Moving
        that blob would corrupt the writer's otherwise valid commit.  Normal
        recovery is strictly read-only for unpaired files: complete sidecars
        are registered, while temporary or unpaired files are left untouched
        for a later offline maintenance pass.
        """

        return []

    def write_recovery_manifest(
        self, key: CacheKey, digest: str, byte_count: int
    ) -> dict[str, Any]:
        manifest = {
            "schema": 1,
            "key": key.as_dict(),
            "key_hash": self.key_hash(key),
            "digest": digest,
            "bytes": byte_count,
            "blob": f"blobs/{self.key_hash(key)}.bin",
        }
        atomic_write_bytes(
            self.manifest_path(key),
            (canonical_json(manifest) + "\n").encode("utf-8"),
        )
        return manifest
