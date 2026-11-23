"""Content-addressed shard cache on the local filesystem.

Cache files are named by their deterministic ``cache_identity`` so identical
contents always map to the same path.  Writes go to a temp file in the same
directory and are atomically renamed, guaranteeing readers only ever observe a
complete shard payload.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .errors import E_CACHE_ALGEBRA_ROLLBACK, E_GEN_DIGEST_MISMATCH, fail
from .model import CacheEntryView, DatasetSpec
from .sharding import ShardManifest, build_shard_manifest, cache_algebra, shard_key


def _payload_path(cache_dir: str, cache_identity: str) -> str:
    return os.path.join(cache_dir, f"{cache_identity}.shard")


def _meta_path(cache_dir: str, cache_identity: str) -> str:
    return os.path.join(cache_dir, f"{cache_identity}.meta.json")


@dataclass
class InvalidationReport:
    trigger: str
    invalidated: List[str]
    retained: List[str]


class ContentCache:
    """Filesystem cache whose authoritative metadata also lives in SQLite."""

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def payload_path_for(self, manifest: ShardManifest) -> str:
        return _payload_path(self.cache_dir, manifest.cache_identity)

    def write_atomic(self, manifest: ShardManifest) -> str:
        """Atomically persist shard payload + metadata, returning the path."""
        payload_path = self.payload_path_for(manifest)
        payload = json.dumps(manifest.as_dict(), sort_keys=True).encode("utf-8")
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".shard", dir=self.cache_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, payload_path)
        except OSError as exc:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise fail(
                E_GEN_DIGEST_MISMATCH,
                "cache payload write failed",
                path=payload_path,
                detail=str(exc),
            )

        meta = {
            "cache_identity": manifest.cache_identity,
            "dataset": manifest.dataset,
            "shard_index": manifest.shard_index,
            "algebra": list(manifest.algebra_input),
            "byte_size": manifest.byte_size,
            "digest": manifest.digest,
        }
        meta_fd, meta_tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".meta", dir=self.cache_dir)
        try:
            with os.fdopen(meta_fd, "wb") as handle:
                handle.write(json.dumps(meta, sort_keys=True).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(meta_tmp, _meta_path(self.cache_dir, manifest.cache_identity))
        except OSError as exc:
            if os.path.exists(meta_tmp):
                os.unlink(meta_tmp)
            raise fail(
                E_GEN_DIGEST_MISMATCH,
                "cache metadata write failed",
                path=payload_path,
                detail=str(exc),
            )
        return payload_path

    def payload_exists(self, manifest: ShardManifest) -> bool:
        return os.path.exists(self.payload_path_for(manifest))


def cache_lookup_key(spec: DatasetSpec, shard_index: int) -> Tuple[str, int, int, Tuple[int, int]]:
    """The cache key: dataset + shard + algebra + generation params."""
    return (spec.name, shard_index, cache_algebra(spec), spec.gen_params.as_tuple())


def evaluate_invalidation(
    specs: Dict[str, DatasetSpec],
    cache_view: Tuple[CacheEntryView, ...],
) -> InvalidationReport:
    """Decide which pre-existing cache entries survive the current inputs.

    An entry is retained only when its algebra matches the current deterministic
    algebra AND its digest matches the freshly derived shard.  Anything else is
    invalidated precisely, while unaffected shards keep hitting.
    """
    invalidated: List[str] = []
    retained: List[str] = []
    triggers: List[str] = []
    for entry in cache_view:
        spec = specs[entry.dataset]
        key = shard_key(entry.dataset, entry.shard_index)
        current_algebra = cache_algebra(spec)
        manifest = build_shard_manifest(spec, entry.shard_index)
        if entry.algebra < current_algebra:
            invalidated.append(key)
            triggers.append("gen_params_or_content_version")
        elif entry.algebra > current_algebra:
            raise fail(
                E_CACHE_ALGEBRA_ROLLBACK,
                "stored cache algebra is ahead of current generation inputs",
                dataset=entry.dataset,
                shard_index=entry.shard_index,
                stored=entry.algebra,
                current=current_algebra,
            )
        elif entry.digest != manifest.digest or entry.byte_size != manifest.byte_size:
            invalidated.append(key)
            triggers.append("digest_mismatch")
        else:
            retained.append(key)
    trigger = triggers[0] if triggers else "none"
    return InvalidationReport(
        trigger=trigger,
        invalidated=sorted(invalidated),
        retained=sorted(retained),
    )
