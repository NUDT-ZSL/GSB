"""Content-addressed chunk cache keyed by audio, rules, geometry, and upstream hash."""

from __future__ import annotations

from typing import Any

import numpy as np

from .config import EngineConfig
from .errors import E_FORMAT_CORRUPT, EngineError
from .util import canonical_json_bytes, object_hash, sha256_bytes


GENESIS_HASH = "0" * 64


def block_source_hash(samples: np.ndarray, sample_start: int, sample_end: int) -> str:
    return sha256_bytes(
        f"{sample_start}:{sample_end}:".encode("ascii")
        + np.asarray(samples[sample_start:sample_end], dtype="<i2").tobytes(order="C")
    )


def envelope_cache_key(
    *,
    pcm_sha256: str,
    rule_version: str,
    config: EngineConfig,
    upstream_envelope_hash: str,
) -> str:
    return object_hash(
        {
            "pcm_sha256": pcm_sha256,
            "vad_rule_version": rule_version,
            "frame_length_samples": config.frame_length_samples,
            "frame_shift_samples": config.frame_shift_samples,
            "frame_length_ms": config.frame_length_ms,
            "frame_shift_ms": config.frame_shift_ms,
            "sample_rate": config.sample_rate,
            "upstream_envelope_hash": upstream_envelope_hash,
        }
    )


class ContentAddressedCache:
    def __init__(self, store: Any, config: EngineConfig, stage_token: str):
        self.store = store
        self.config = config
        self.stage_token = stage_token
        self.reused: list[dict[str, Any]] = []
        self.affected: list[dict[str, Any]] = []

    def lookup(self, key: str, block_id: str) -> dict[str, Any] | None:
        payload = self.store.cache_lookup(key)
        if payload is not None:
            self.reused.append({"block_id": block_id, "key": key})
            return payload
        self.affected.append({"block_id": block_id, "key": key})
        return None

    def put(
        self,
        key: str,
        payload: Any,
        *,
        block_id: str,
        pcm_sha256: str,
        rule_version: str,
        upstream_hash: str,
    ) -> None:
        payload_bytes = canonical_json_bytes(payload)
        payload_sha256 = sha256_bytes(payload_bytes)
        if not self.store.cache_put_staged(
            key=key,
            payload_text=payload_bytes.decode("utf-8"),
            payload_sha256=payload_sha256,
            pcm_sha256=pcm_sha256,
            rule_version=rule_version,
            frame_length=self.config.frame_length_samples,
            frame_shift=self.config.frame_shift_samples,
            upstream_hash=upstream_hash,
            stage_token=self.stage_token,
            max_entries=self.config.max_cache_entries,
        ):
            existing = self.store.cache_lookup(key)
            if existing != payload:
                raise EngineError(
                    E_FORMAT_CORRUPT,
                    "cache key collision or corrupted cache entry",
                    {"key": key},
                )
        if not any(item["block_id"] == block_id for item in self.affected):
            self.affected.append({"block_id": block_id, "key": key})

    def result(self) -> dict[str, Any]:
        return {
            "reused_block_keys": list(self.reused),
            "affected_block_keys": list(self.affected),
            "reused_count": len(self.reused),
            "affected_count": len(self.affected),
            "mode": "REUSE" if not self.affected else "WHOLE_REPLACE",
            "cache_entry_limit": self.config.max_cache_entries,
        }

