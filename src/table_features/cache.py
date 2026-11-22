"""Content-addressed cache for encoding, crossing, and selection artifacts."""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .canonical import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_bytes,
    read_json,
    sha256_bytes,
    sha256_obj,
)
from .errors import E_CACHE_CORRUPT, BusinessError


INDEX_FORMAT_VERSION = 1


def matrix_to_bytes(matrix: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, np.asarray(matrix, dtype=np.float64), allow_pickle=False)
    return buffer.getvalue()


def matrix_to_base64(matrix: np.ndarray) -> str:
    return base64.b64encode(matrix_to_bytes(matrix)).decode("ascii")


def matrix_from_base64(value: str, *, expected_shape: tuple[int, int] | None = None) -> np.ndarray:
    raw = base64.b64decode(value.encode("ascii"), validate=True)
    matrix = np.load(io.BytesIO(raw), allow_pickle=False)
    if matrix.ndim != 2 or matrix.dtype != np.float64:
        raise ValueError("cached matrix must be a two-dimensional float64 array")
    if expected_shape is not None and matrix.shape != expected_shape:
        raise ValueError(
            f"cached matrix shape {matrix.shape} does not match expected {expected_shape}"
        )
    return matrix


@dataclass(frozen=True)
class CacheLookup:
    status: str
    stage: str
    feature: str
    cache_key: str
    content_hash: str | None
    components: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stage": self.stage,
            "feature": self.feature,
            "cache_key": self.cache_key,
            "content_hash": self.content_hash,
            "components": self.components,
        }


def build_cache_key(
    *,
    stage: str,
    feature: str,
    components: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a content-addressed key and its inspectable composition.

    The four required inputs are checked here so callers cannot omit the sample
    projection digest, feature-config version, slot digest, or random seed.
    """

    required = {"sample_content_digest", "config_version", "slots_digest", "seed"}
    missing = sorted(required - set(components))
    if missing:
        raise ValueError(f"cache key is missing required components: {', '.join(missing)}")
    key_document = {
        "stage": stage,
        "feature": feature,
        "components": dict(components),
    }
    return {"cache_key": sha256_obj(key_document), "components": key_document}


class ContentAddressedCache:
    """Small local CAS with an inspectable feature index."""

    def __init__(self, root: str | Path, *, clear: bool = False) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.index_path = self.root / "index.json"
        if clear and self.root.exists():
            for path in sorted(self.root.rglob("*"), reverse=True):
                if path.is_file() or path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self._write_index(self._empty_index())

    @staticmethod
    def _empty_index() -> dict[str, Any]:
        return {
            "format_version": INDEX_FORMAT_VERSION,
            "entries": {},
            "features": {},
        }

    def _write_index(self, index: dict[str, Any]) -> None:
        atomic_write_json(self.index_path, index)

    def _load_index(self) -> dict[str, Any]:
        try:
            index = read_json(self.index_path)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise BusinessError(
                E_CACHE_CORRUPT,
                f"cache index is unreadable: {exc}",
                stage="cache",
                feature="<index>",
                details={"path": str(self.index_path)},
            ) from exc
        if (
            not isinstance(index, dict)
            or index.get("format_version") != INDEX_FORMAT_VERSION
            or not isinstance(index.get("entries"), dict)
            or not isinstance(index.get("features"), dict)
        ):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache index has an unsupported structure",
                stage="cache",
                feature="<index>",
                details={"path": str(self.index_path)},
            )
        return index

    def _object_path(self, content_hash: str) -> Path:
        return self.objects_dir / f"{content_hash}.json"

    @staticmethod
    def _verify_entry_key(cache_key: str, entry: dict[str, Any], *, feature: str) -> None:
        components = entry.get("components")
        if not isinstance(components, dict) or sha256_obj(components) != cache_key:
            raise BusinessError(
                E_CACHE_CORRUPT,
                f"cache index key composition is invalid for feature {feature}",
                stage=str(entry.get("stage", "cache")),
                feature=feature,
                details={"cache_key": cache_key},
            )

    def _read_object(self, entry: dict[str, Any], *, feature: str) -> tuple[dict[str, Any], str]:
        content_hash = entry.get("content_hash")
        if not isinstance(content_hash, str) or len(content_hash) != 64:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache entry has an invalid content hash",
                stage=str(entry.get("stage", "cache")),
                feature=feature,
                details={"content_hash": content_hash},
            )
        path = self._object_path(content_hash)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise BusinessError(
                E_CACHE_CORRUPT,
                f"cache object is missing for feature {feature}",
                stage=str(entry.get("stage", "cache")),
                feature=feature,
                details={"path": str(path), "content_hash": content_hash},
            ) from exc
        actual_hash = sha256_bytes(data)
        if actual_hash != content_hash:
            raise BusinessError(
                E_CACHE_CORRUPT,
                f"cache object checksum mismatch for feature {feature}",
                stage=str(entry.get("stage", "cache")),
                feature=feature,
                details={
                    "path": str(path),
                    "expected_content_hash": content_hash,
                    "actual_content_hash": actual_hash,
                },
            )
        try:
            payload = read_json(path)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise BusinessError(
                E_CACHE_CORRUPT,
                f"cache object is unreadable for feature {feature}",
                stage=str(entry.get("stage", "cache")),
                feature=feature,
                details={"path": str(path), "content_hash": content_hash},
            ) from exc
        if not isinstance(payload, dict):
            raise BusinessError(
                E_CACHE_CORRUPT,
                f"cache object payload is invalid for feature {feature}",
                stage=str(entry.get("stage", "cache")),
                feature=feature,
                details={"content_hash": content_hash},
            )
        return payload, content_hash

    def get(
        self,
        *,
        stage: str,
        feature: str,
        key_info: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, CacheLookup]:
        cache_key = key_info["cache_key"]
        components = key_info["components"]
        index = self._load_index()
        entry = index["entries"].get(cache_key)
        if entry is None:
            return None, CacheLookup(
                status="miss",
                stage=stage,
                feature=feature,
                cache_key=cache_key,
                content_hash=None,
                components=components,
            )
        self._verify_entry_key(cache_key, entry, feature=feature)
        if entry.get("stage") != stage or entry.get("feature") != feature:
            raise BusinessError(
                E_CACHE_CORRUPT,
                f"cache entry attribution mismatch for feature {feature}",
                stage=stage,
                feature=feature,
                details={
                    "cache_key": cache_key,
                    "entry_stage": entry.get("stage"),
                    "entry_feature": entry.get("feature"),
                },
            )
        payload, content_hash = self._read_object(entry, feature=feature)
        if (
            payload.get("cache_key") != cache_key
            or payload.get("stage") != stage
            or payload.get("feature") != feature
        ):
            raise BusinessError(
                E_CACHE_CORRUPT,
                f"cache payload attribution mismatch for feature {feature}",
                stage=stage,
                feature=feature,
                details={"cache_key": cache_key, "content_hash": content_hash},
            )
        return payload, CacheLookup(
            status="hit",
            stage=stage,
            feature=feature,
            cache_key=cache_key,
            content_hash=content_hash,
            components=components,
        )

    def put(
        self,
        *,
        stage: str,
        feature: str,
        key_info: dict[str, Any],
        payload: dict[str, Any],
    ) -> CacheLookup:
        cache_key = key_info["cache_key"]
        components = key_info["components"]
        stored_payload = dict(payload)
        stored_payload["cache_key"] = cache_key
        stored_payload["stage"] = stage
        stored_payload["feature"] = feature
        data = canonical_bytes(stored_payload)
        content_hash = sha256_bytes(data)
        object_path = self._object_path(content_hash)
        rewrite = True
        if object_path.exists():
            try:
                rewrite = sha256_bytes(object_path.read_bytes()) != content_hash
            except OSError:
                rewrite = True
        if rewrite:
            atomic_write_bytes(object_path, data)

        index = self._load_index()
        index["entries"][cache_key] = {
            "stage": stage,
            "feature": feature,
            "content_hash": content_hash,
            "components": components,
        }
        feature_keys = index["features"].setdefault(feature, [])
        if cache_key not in feature_keys:
            feature_keys.append(cache_key)
            feature_keys.sort()
        self._write_index(index)
        return CacheLookup(
            status="stored",
            stage=stage,
            feature=feature,
            cache_key=cache_key,
            content_hash=content_hash,
            components=components,
        )

    def inspect_feature(self, feature: str, *, verify: bool = True) -> list[dict[str, Any]]:
        index = self._load_index()
        cache_keys = index["features"].get(feature, [])
        if not isinstance(cache_keys, list):
            raise BusinessError(
                E_CACHE_CORRUPT,
                f"cache feature index is invalid for feature {feature}",
                stage="cache",
                feature=feature,
            )
        results: list[dict[str, Any]] = []
        for cache_key in sorted(cache_keys):
            entry = index["entries"].get(cache_key)
            if not isinstance(entry, dict):
                raise BusinessError(
                    E_CACHE_CORRUPT,
                    f"cache entry is missing for feature {feature}",
                    stage="cache",
                    feature=feature,
                    details={"cache_key": cache_key},
                )
            self._verify_entry_key(cache_key, entry, feature=feature)
            content_hash = entry.get("content_hash")
            if verify:
                _, content_hash = self._read_object(entry, feature=feature)
            results.append(
                {
                    "feature": feature,
                    "stage": entry.get("stage"),
                    "cache_key": cache_key,
                    "content_hash": content_hash,
                    "components": entry.get("components"),
                }
            )
        return results

    def all_features(self) -> list[str]:
        return sorted(self._load_index()["features"])

