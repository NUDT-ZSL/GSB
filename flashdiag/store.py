"""Content-addressed, on-disk store for node artifacts."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .errors import DiagnosticError, E_CACHE_CORRUPT, E_SNAPSHOT_MISSING
from .hashing import array_sha256, canonical_json_bytes, sha256_json


ALLOWED_NODE_TYPES = frozenset(
    {"validation", "plan", "score", "norm", "writeback", "audit"}
)
ARRAY_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _jsonable(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class CachedArtifact:
    cache_key: str
    node_type: str
    content_sha256: str
    path: str
    meta: dict[str, Any]
    arrays: dict[str, np.ndarray]
    array_sha256: dict[str, str]

    def array(self, name: str) -> np.ndarray:
        return self.arrays[name]

    def descriptor(self) -> dict[str, Any]:
        return {
            "cache_key": self.cache_key,
            "node_type": self.node_type,
            "content_sha256": self.content_sha256,
            "path": self.path,
        }


class ArtifactStore:
    """A directory-per-cache-key store with canonical manifests and checksums."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()

    def _validate_key(self, node_type: str, cache_key: str) -> None:
        if node_type not in ALLOWED_NODE_TYPES:
            raise ValueError(f"unsupported node type: {node_type}")
        if not HEX_RE.fullmatch(cache_key):
            raise ValueError("cache key must be a lowercase SHA-256 hex digest")

    def artifact_dir(self, node_type: str, cache_key: str) -> Path:
        self._validate_key(node_type, cache_key)
        return self.root / node_type / cache_key[:2] / cache_key

    def manifest_path(self, node_type: str, cache_key: str) -> Path:
        return self.artifact_dir(node_type, cache_key) / "manifest.json"

    def exists(self, node_type: str, cache_key: str) -> bool:
        return self.manifest_path(node_type, cache_key).is_file()

    def try_load(
        self,
        node_type: str,
        cache_key: str,
        *,
        expected_content_sha256: str | None = None,
    ) -> CachedArtifact | None:
        if not self.exists(node_type, cache_key):
            return None
        return self.load(
            node_type,
            cache_key,
            expected_content_sha256=expected_content_sha256,
        )

    def load(
        self,
        node_type: str,
        cache_key: str,
        *,
        expected_content_sha256: str | None = None,
    ) -> CachedArtifact:
        manifest_path = self.manifest_path(node_type, cache_key)
        if not manifest_path.is_file():
            raise DiagnosticError(
                E_SNAPSHOT_MISSING,
                "artifact manifest is missing",
                cache_key=cache_key,
                node_type=node_type,
                path=str(manifest_path),
            )
        try:
            raw = manifest_path.read_bytes()
            manifest = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise DiagnosticError(
                E_CACHE_CORRUPT,
                "artifact manifest cannot be decoded",
                cache_key=cache_key,
                node_type=node_type,
                path=str(manifest_path),
                error=str(exc),
            ) from exc

        if manifest.get("cache_key") != cache_key or manifest.get("node_type") != node_type:
            raise DiagnosticError(
                E_CACHE_CORRUPT,
                "artifact manifest identity mismatch",
                cache_key=cache_key,
                node_type=node_type,
                path=str(manifest_path),
            )

        content_sha = manifest.get("content_sha256")
        if not isinstance(content_sha, str) or not HEX_RE.fullmatch(content_sha):
            raise DiagnosticError(
                E_CACHE_CORRUPT,
                "artifact manifest has an invalid content checksum",
                cache_key=cache_key,
                path=str(manifest_path),
            )
        if expected_content_sha256 is not None and content_sha != expected_content_sha256:
            raise DiagnosticError(
                E_CACHE_CORRUPT,
                "artifact checksum differs from ledger",
                cache_key=cache_key,
                expected=expected_content_sha256,
                actual=content_sha,
            )

        payload = {
            "schema_version": manifest.get("schema_version"),
            "node_type": manifest.get("node_type"),
            "meta": manifest.get("meta"),
            "arrays": manifest.get("arrays"),
        }
        recomputed_content_sha = sha256_json(payload)
        if recomputed_content_sha != content_sha:
            raise DiagnosticError(
                E_CACHE_CORRUPT,
                "artifact manifest checksum does not match its payload",
                cache_key=cache_key,
                expected=content_sha,
                actual=recomputed_content_sha,
            )

        array_root = manifest_path.parent
        arrays: dict[str, np.ndarray] = {}
        array_hashes: dict[str, str] = {}
        descriptors = manifest.get("arrays")
        if not isinstance(descriptors, dict):
            raise DiagnosticError(
                E_CACHE_CORRUPT,
                "artifact manifest arrays field is invalid",
                cache_key=cache_key,
            )
        for name, descriptor in descriptors.items():
            if not isinstance(name, str) or not ARRAY_NAME_RE.fullmatch(name):
                raise DiagnosticError(
                    E_CACHE_CORRUPT,
                    "artifact contains an invalid array name",
                    cache_key=cache_key,
                    array=name,
                )
            if not isinstance(descriptor, dict):
                raise DiagnosticError(
                    E_CACHE_CORRUPT,
                    "artifact array descriptor is invalid",
                    cache_key=cache_key,
                    array=name,
                )
            relative = descriptor.get("file")
            expected_hash = descriptor.get("sha256")
            if not isinstance(relative, str) or not isinstance(expected_hash, str):
                raise DiagnosticError(
                    E_CACHE_CORRUPT,
                    "artifact array descriptor lacks file/checksum",
                    cache_key=cache_key,
                    array=name,
                )
            array_path = (array_root / relative).resolve()
            if array_root.resolve() not in array_path.parents:
                raise DiagnosticError(
                    E_CACHE_CORRUPT,
                    "artifact array path escapes its artifact directory",
                    cache_key=cache_key,
                    array=name,
                )
            try:
                array = np.load(array_path, allow_pickle=False)
            except (OSError, ValueError) as exc:
                raise DiagnosticError(
                    E_CACHE_CORRUPT,
                    "artifact array cannot be loaded",
                    cache_key=cache_key,
                    array=name,
                    path=str(array_path),
                    error=str(exc),
                ) from exc
            actual_hash = array_sha256(array)
            if actual_hash != expected_hash:
                raise DiagnosticError(
                    E_CACHE_CORRUPT,
                    "artifact array checksum mismatch",
                    cache_key=cache_key,
                    array=name,
                    expected=expected_hash,
                    actual=actual_hash,
                )
            arrays[name] = array
            array_hashes[name] = actual_hash

        meta = manifest.get("meta")
        if not isinstance(meta, dict):
            raise DiagnosticError(
                E_CACHE_CORRUPT,
                "artifact metadata is not an object",
                cache_key=cache_key,
            )
        return CachedArtifact(
            cache_key=cache_key,
            node_type=node_type,
            content_sha256=content_sha,
            path=str(manifest_path),
            meta=meta,
            arrays=arrays,
            array_sha256=array_hashes,
        )

    def write(
        self,
        node_type: str,
        cache_key: str,
        *,
        meta: Mapping[str, Any],
        arrays: Mapping[str, np.ndarray] | None = None,
    ) -> CachedArtifact:
        self._validate_key(node_type, cache_key)
        array_map: dict[str, np.ndarray] = {}
        for name, array in (arrays or {}).items():
            if not ARRAY_NAME_RE.fullmatch(name):
                raise ValueError(f"invalid array name: {name}")
            array_map[name] = np.asarray(array)

        descriptors: dict[str, dict[str, Any]] = {}
        array_bytes: dict[str, bytes] = {}
        for name, array in sorted(array_map.items()):
            buffer = io.BytesIO()
            np.save(buffer, array, allow_pickle=False)
            data = buffer.getvalue()
            relative = f"data/{name}.npy"
            array_bytes[name] = data
            descriptors[name] = {
                "file": relative,
                "sha256": array_sha256(array),
                "dtype": array.dtype.str,
                "shape": list(array.shape),
            }

        payload = {
            "schema_version": 1,
            "node_type": node_type,
            "meta": _jsonable(dict(meta)),
            "arrays": descriptors,
        }
        content_sha = sha256_json(payload)
        manifest = dict(payload)
        manifest["cache_key"] = cache_key
        manifest["content_sha256"] = content_sha
        manifest_bytes = canonical_json_bytes(manifest) + b"\n"

        with self._write_lock:
            target = self.artifact_dir(node_type, cache_key)
            if target.exists():
                existing = self.load(node_type, cache_key)
                if existing.content_sha256 != content_sha:
                    raise DiagnosticError(
                        E_CACHE_CORRUPT,
                        "cache key collision: existing content differs",
                        cache_key=cache_key,
                        node_type=node_type,
                        existing=existing.content_sha256,
                        attempted=content_sha,
                    )
                return existing

            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.parent / f".tmp-{cache_key}-{uuid.uuid4().hex}"
            try:
                data_dir = temporary / "data"
                data_dir.mkdir(parents=True, exist_ok=False)
                for name, data in array_bytes.items():
                    (data_dir / f"{name}.npy").write_bytes(data)
                manifest_file = temporary / "manifest.json"
                manifest_file.write_bytes(manifest_bytes)
                with manifest_file.open("rb") as handle:
                    os.fsync(handle.fileno())
                try:
                    os.rename(temporary, target)
                except OSError:
                    existing = self.load(node_type, cache_key)
                    if existing.content_sha256 != content_sha:
                        raise DiagnosticError(
                            E_CACHE_CORRUPT,
                            "cache key collision while publishing artifact",
                            cache_key=cache_key,
                            node_type=node_type,
                        )
                    shutil.rmtree(temporary, ignore_errors=True)
                    return existing
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise

        artifact = self.load(node_type, cache_key)
        if artifact.content_sha256 != content_sha:
            raise DiagnosticError(
                E_CACHE_CORRUPT,
                "published artifact checksum mismatch",
                cache_key=cache_key,
                expected=content_sha,
                actual=artifact.content_sha256,
            )
        return artifact

    def index(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for node_type in sorted(ALLOWED_NODE_TYPES):
            node_root = self.root / node_type
            count = 0
            if node_root.is_dir():
                count = sum(
                    1
                    for path in node_root.glob("*/*/manifest.json")
                    if path.is_file()
                )
            counts[node_type] = count
        return {"root": str(self.root), "counts": counts}

