from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from .canonical import (
    canonical_json_bytes,
    pack_payload,
    sha256_bytes,
    sha256_json,
    unpack_payload,
)
from .errors import PipelineError

CACHE_SCHEMA = "cm-cache-entry-v1"
KEY_SCHEMA = "cm-cache-key-v1"


def build_key_components(
    *,
    node_type: str,
    instance: str,
    pair_id: str | None,
    inputs: dict[str, Any],
    upstream: dict[str, Any],
    params: dict[str, Any],
    vocab: dict[str, Any] | None = None,
    codebook: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": KEY_SCHEMA,
        "node_type": node_type,
        "instance": instance,
        "pair_id": pair_id,
        "inputs": inputs,
        "upstream": upstream,
        "params": params,
        "vocab": vocab,
        "codebook": codebook,
    }


def key_from_components(components: dict[str, Any]) -> str:
    return sha256_json(components)


class CacheStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.tmp = self.root / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)

    def _entry_dir(self, key: str) -> Path:
        if len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key):
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"Invalid cache key: {key}",
                stage="cache",
                field="key",
            )
        return self.objects / key[:2] / key

    def exists(self, key: str) -> bool:
        entry = self._entry_dir(key)
        return (entry / "meta.json").is_file() and (entry / "payload.bin").is_file()

    def get(self, key: str) -> tuple[dict[str, np.ndarray], dict[str, Any], dict[str, Any]]:
        entry = self._entry_dir(key)
        if not self.exists(key):
            raise PipelineError(
                "E_LINEAGE_BROKEN",
                f"Cache entry is missing: {key}",
                stage="cache",
                field="key",
                details={"cache_key": key},
            )
        try:
            meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineError(
                "E_INCONSISTENT",
                f"Cannot read cache metadata for {key}: {exc}",
                stage="cache",
                field="meta",
                details={"cache_key": key},
            ) from exc
        expected_key = key_from_components(meta.get("key_components", {}))
        if expected_key != key or meta.get("key") != key:
            raise PipelineError(
                "E_INCONSISTENT",
                "Cache key does not match its recorded components",
                stage="cache",
                field="key",
                details={"cache_key": key, "recomputed_key": expected_key},
            )
        payload = (entry / "payload.bin").read_bytes()
        actual_hash = sha256_bytes(payload)
        if actual_hash != meta.get("product_hash"):
            raise PipelineError(
                "E_INCONSISTENT",
                "Cache product hash mismatch",
                stage="cache",
                field="product_hash",
                details={
                    "cache_key": key,
                    "expected": meta.get("product_hash"),
                    "actual": actual_hash,
                },
            )
        arrays, payload_meta = unpack_payload(payload)
        return arrays, payload_meta, meta

    def put(
        self,
        components: dict[str, Any],
        arrays: dict[str, np.ndarray],
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = key_from_components(components)
        payload = pack_payload(arrays, meta or {})
        product_hash = sha256_bytes(payload)
        entry = self._entry_dir(key)
        if self.exists(key):
            _, _, existing = self.get(key)
            if existing["product_hash"] != product_hash:
                raise PipelineError(
                    "E_INCONSISTENT",
                    "Same cache key produced different content",
                    stage="cache",
                    field="product_hash",
                    details={
                        "cache_key": key,
                        "existing": existing["product_hash"],
                        "new": product_hash,
                    },
                )
            if existing.get("key_components") != components:
                raise PipelineError(
                    "E_INCONSISTENT",
                    "Same cache key has different component metadata",
                    stage="cache",
                    field="key_components",
                    details={"cache_key": key},
                )
            return existing
        entry.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="entry-", dir=self.tmp))
        try:
            (staging / "payload.bin").write_bytes(payload)
            meta_doc = {
                "schema": CACHE_SCHEMA,
                "key": key,
                "key_components": components,
                "product_hash": product_hash,
                "payload_size": len(payload),
                "array_names": sorted(arrays),
            }
            (staging / "meta.json").write_bytes(canonical_json_bytes(meta_doc))
            try:
                os.replace(staging, entry)
            except OSError:
                if not self.exists(key):
                    raise
                _, _, existing = self.get(key)
                if existing["product_hash"] != product_hash:
                    raise PipelineError(
                        "E_INCONSISTENT",
                        "Concurrent cache write produced different content",
                        stage="cache",
                        field="product_hash",
                        details={"cache_key": key},
                    )
                return existing
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return {
            "schema": CACHE_SCHEMA,
            "key": key,
            "key_components": components,
            "product_hash": product_hash,
            "payload_size": len(payload),
            "array_names": sorted(arrays),
        }

    def inspect(self, key: str) -> dict[str, Any]:
        _, payload_meta, meta = self.get(key)
        recomputed = key_from_components(meta["key_components"])
        return {
            "key": key,
            "valid": recomputed == key,
            "recomputed_key": recomputed,
            "product_hash": meta["product_hash"],
            "payload_size": meta["payload_size"],
            "array_names": meta.get("array_names", []),
            "key_components": meta["key_components"],
            "payload_meta": payload_meta,
        }

    def count(self) -> int:
        return sum(1 for _ in self.iter_entries())

    def iter_entries(self) -> Iterator[Path]:
        if not self.objects.exists():
            return
        for prefix in sorted(self.objects.iterdir()):
            if not prefix.is_dir():
                continue
            for entry in sorted(prefix.iterdir()):
                if entry.is_dir() and (entry / "meta.json").is_file():
                    yield entry

