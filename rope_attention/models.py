"""Immutable domain models shared by parsing, migration and computation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .util import canonical_json_bytes, sha256_bytes


CONFIG_VERSION_V1 = "v1"
CONFIG_VERSION_V2 = "v2"
CONFIG_VERSION_V3 = "v3"
SUPPORTED_CONFIG_VERSIONS = (CONFIG_VERSION_V1, CONFIG_VERSION_V2, CONFIG_VERSION_V3)

V1_VARIANTS = ("rope",)
V2_VARIANTS = ("rope", "ntk")
V3_VARIANTS = ("rope", "ntk", "block_freq")
FREQUENCY_METHODS = ("identity", "global_scale", "blockwise")


@dataclass(frozen=True)
class FrequencyAdjustment:
    """Version-3 frequency adjustment block."""

    method: str = "identity"
    factor: float = 1.0
    block_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"method": self.method, "factor": self.factor}
        if self.block_tokens is not None:
            result["block_tokens"] = self.block_tokens
        return result


@dataclass(frozen=True)
class ParsedConfig:
    """Parsed source config plus provenance required for deterministic audits."""

    source_version: str
    source_line: int
    source_span: str
    source_text_sha256: str
    base_theta: float
    pos_scale: float
    block_size: int
    variant: str | None
    frequency_adjustment: FrequencyAdjustment | None
    per_head_scale: tuple[float, ...] | None
    explicit_fields: tuple[str, ...]
    field_locations: dict[str, str] = field(default_factory=dict)

    @property
    def effective_variant(self) -> str:
        return self.variant or "rope"

    @property
    def effective_frequency_adjustment(self) -> FrequencyAdjustment:
        return self.frequency_adjustment or FrequencyAdjustment()

    @property
    def effective_per_head_scale(self) -> tuple[float, ...]:
        return self.per_head_scale or (1.0,)

    def to_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "base_theta": self.base_theta,
            "block_size": self.block_size,
            "explicit_fields": list(self.explicit_fields),
            "field_locations": dict(sorted(self.field_locations.items())),
            "frequency_adjustment": (
                self.frequency_adjustment.to_dict()
                if self.frequency_adjustment is not None
                else None
            ),
            "per_head_scale": (
                list(self.per_head_scale) if self.per_head_scale is not None else None
            ),
            "pos_scale": self.pos_scale,
            "variant": self.variant,
        }
        if include_source:
            value.update(
                {
                    "source_line": self.source_line,
                    "source_span": self.source_span,
                    "source_text_sha256": self.source_text_sha256,
                    "source_version": self.source_version,
                }
            )
        return value

    def canonical_bytes(self, *, include_source: bool = True) -> bytes:
        return canonical_json_bytes(self.to_dict(include_source=include_source))

    def content_checksum(self) -> str:
        return sha256_bytes(self.canonical_bytes(include_source=True))

    def semantic_dict(self) -> dict[str, Any]:
        """Semantic view used to recognize lossless cross-version aliases."""

        return {
            "base_theta": self.base_theta,
            "block_size": self.block_size,
            "frequency_adjustment": self.effective_frequency_adjustment.to_dict(),
            "per_head_scale": list(self.effective_per_head_scale),
            "pos_scale": self.pos_scale,
            "variant": self.effective_variant,
        }


@dataclass(frozen=True)
class ResourceLimits:
    """Resource guards for one component invocation."""

    max_block_memory_bytes: int = 64 * 1024 * 1024
    max_cache_entries: int = 20_000
    max_parallel_recompute_nodes: int = 4
    cache_overflow_policy: str = "degrade"
    parallelism_overflow_policy: str = "degrade"

    def __post_init__(self) -> None:
        if self.max_block_memory_bytes <= 0:
            raise ValueError("max_block_memory_bytes must be positive")
        if self.max_cache_entries <= 0:
            raise ValueError("max_cache_entries must be positive")
        if self.max_parallel_recompute_nodes <= 0:
            raise ValueError("max_parallel_recompute_nodes must be positive")
        if self.cache_overflow_policy not in {"degrade", "error"}:
            raise ValueError("cache_overflow_policy must be degrade or error")
        if self.parallelism_overflow_policy not in {"degrade", "error"}:
            raise ValueError("parallelism_overflow_policy must be degrade or error")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_overflow_policy": self.cache_overflow_policy,
            "max_block_memory_bytes": self.max_block_memory_bytes,
            "max_cache_entries": self.max_cache_entries,
            "max_parallel_recompute_nodes": self.max_parallel_recompute_nodes,
            "parallelism_overflow_policy": self.parallelism_overflow_policy,
        }

