"""Canonical internal domain models."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from .canonical import sha256_json
from .errors import corrupt


PRECISIONS = ("fp16", "fp32", "fp64")
PRECISION_RANK = {name: index for index, name in enumerate(PRECISIONS)}
THRESHOLD_KEYS = ("image", "joint", "text")


@dataclass(frozen=True)
class ThresholdEntry:
    key: str
    threshold: float
    revision: int
    updated_at_ms: int

    def __post_init__(self) -> None:
        if self.key not in THRESHOLD_KEYS:
            raise corrupt("unsupported threshold key", key=self.key)
        if not math.isfinite(self.threshold) or not 0.0 <= self.threshold <= 1.0:
            raise corrupt("threshold must be finite and lie in [0, 1]", key=self.key)
        if self.revision < 0 or self.updated_at_ms < 0:
            raise corrupt("entry revision and timestamp must be non-negative", key=self.key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "threshold": self.threshold,
            "revision": self.revision,
            "updated_at_ms": self.updated_at_ms,
        }


@dataclass(frozen=True)
class Tombstone:
    key: str
    revision: int
    invalidated_at_ms: int

    def __post_init__(self) -> None:
        if not self.key:
            raise corrupt("tombstone key must be non-empty")
        if self.revision < 0 or self.invalidated_at_ms < 0:
            raise corrupt("tombstone revision and timestamp must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "revision": self.revision,
            "invalidated_at_ms": self.invalidated_at_ms,
        }


@dataclass(frozen=True)
class ProvenanceRecord:
    field: str
    source: str
    rule: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "source": self.source,
            "rule": self.rule,
            "value": self.value,
        }


@dataclass(frozen=True)
class DiscardRecord:
    field: str
    source_rule: str
    reason: str
    previous_value: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "source_rule": self.source_rule,
            "reason": self.reason,
            "previous_value": self.previous_value,
        }


@dataclass(frozen=True)
class ThresholdConfig:
    """Unified representation used by routing, cache, and scheduling."""

    schema_version: int
    generation: int
    revision: int
    created_at_ms: int
    updated_at_ms: int
    entries: tuple[ThresholdEntry, ...]
    tombstones: tuple[Tombstone, ...]
    route_budget: int
    candidate_plan_count: int
    scheduler_workers: int
    precision: str
    provenance: tuple[ProvenanceRecord, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.schema_version not in (1, 2):
            raise corrupt("unsupported internal schema version", schema_version=self.schema_version)
        if self.generation < 1:
            raise corrupt("config generation must be at least 1")
        if self.revision < 0:
            raise corrupt("config revision must be non-negative")
        if self.created_at_ms < 0 or self.updated_at_ms < self.created_at_ms:
            raise corrupt("configuration timestamps are inconsistent")
        if self.route_budget <= 0:
            raise corrupt("route budget must be positive")
        if not 1 <= self.candidate_plan_count <= 8:
            raise corrupt("candidate plan count must be in [1, 8]")
        if not 1 <= self.scheduler_workers <= 64:
            raise corrupt("scheduler workers must be in [1, 64]")
        if self.precision not in PRECISIONS:
            raise corrupt("unsupported precision", precision=self.precision)
        keys = [entry.key for entry in self.entries]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise corrupt("threshold entries must have unique sorted keys")
        if missing := set(THRESHOLD_KEYS).difference(keys):
            raise corrupt("threshold entries are incomplete", missing=sorted(missing))
        tombstone_keys = [item.key for item in self.tombstones]
        if tombstone_keys != sorted(tombstone_keys) or len(tombstone_keys) != len(set(tombstone_keys)):
            raise corrupt("tombstones must have unique sorted keys")
        for entry in self.entries:
            if entry.updated_at_ms > self.updated_at_ms:
                raise corrupt(
                    "threshold entry timestamp exceeds config timestamp",
                    key=entry.key,
                )
            if entry.revision > self.revision:
                raise corrupt(
                    "threshold entry revision exceeds config revision",
                    key=entry.key,
                )
        for tombstone in self.tombstones:
            if tombstone.invalidated_at_ms > self.updated_at_ms:
                raise corrupt(
                    "tombstone timestamp exceeds config timestamp",
                    key=tombstone.key,
                )
            if tombstone.revision > self.revision:
                raise corrupt(
                    "tombstone revision exceeds config revision",
                    key=tombstone.key,
                )

    @property
    def version_token(self) -> str:
        return f"g{self.generation}.r{self.revision}"

    def threshold(self, key: str) -> float:
        for entry in self.entries:
            if entry.key == key:
                return entry.threshold
        raise corrupt("threshold key is missing", key=key)

    def threshold_map(self) -> dict[str, float]:
        return {entry.key: entry.threshold for entry in self.entries}

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generation": self.generation,
            "revision": self.revision,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "entries": [entry.to_dict() for entry in self.entries],
            "tombstones": [item.to_dict() for item in self.tombstones],
            "route_budget": self.route_budget,
            "candidate_plan_count": self.candidate_plan_count,
            "scheduler_workers": self.scheduler_workers,
            "precision": self.precision,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.semantic_dict()
        payload["provenance"] = [record.to_dict() for record in self.provenance]
        return payload

    @classmethod
    def from_semantic_dict(cls, payload: dict[str, Any]) -> "ThresholdConfig":
        entries = tuple(
            ThresholdEntry(
                key=item["key"],
                threshold=item["threshold"],
                revision=item["revision"],
                updated_at_ms=item["updated_at_ms"],
            )
            for item in payload["entries"]
        )
        tombstones = tuple(
            Tombstone(
                key=item["key"],
                revision=item["revision"],
                invalidated_at_ms=item["invalidated_at_ms"],
            )
            for item in payload["tombstones"]
        )
        return cls(
            schema_version=int(payload["schema_version"]),
            generation=int(payload["generation"]),
            revision=int(payload["revision"]),
            created_at_ms=int(payload["created_at_ms"]),
            updated_at_ms=int(payload["updated_at_ms"]),
            entries=entries,
            tombstones=tombstones,
            route_budget=int(payload["route_budget"]),
            candidate_plan_count=int(payload["candidate_plan_count"]),
            scheduler_workers=int(payload["scheduler_workers"]),
            precision=str(payload["precision"]),
        )

    def fingerprint(self) -> str:
        return sha256_json(self.semantic_dict())

    def replace(self, **changes: Any) -> "ThresholdConfig":
        return replace(self, **changes)

    def with_provenance(self, records: Iterable[ProvenanceRecord]) -> "ThresholdConfig":
        return replace(self, provenance=tuple(records))


@dataclass(frozen=True)
class InferenceRequest:
    request_id: str
    prompt_text: str
    image_seed: int
    text_revision: int
    image_revision: int

    def __post_init__(self) -> None:
        if not self.request_id:
            raise corrupt("request_id must be non-empty")
        if not isinstance(self.prompt_text, str):
            raise corrupt("prompt_text must be a string", request_id=self.request_id)
        if self.image_seed < 0 or self.text_revision < 0 or self.image_revision < 0:
            raise corrupt("request numeric fields must be non-negative", request_id=self.request_id)

    def input_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "prompt_text": self.prompt_text,
            "image_seed": self.image_seed,
            "text_revision": self.text_revision,
            "image_revision": self.image_revision,
        }

    def input_digest(self) -> str:
        return sha256_json(self.input_dict())

    def revision_token(self) -> tuple[int, int]:
        return (self.text_revision, self.image_revision)


@dataclass(frozen=True)
class LoadResult:
    format_version: int
    config: ThresholdConfig
    provenance: tuple[ProvenanceRecord, ...] = field(default_factory=tuple)
    metadata_checksum: str | None = None
    shard_checksums: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "config": self.config.to_dict(),
            "provenance": [record.to_dict() for record in self.provenance],
            "metadata_checksum": self.metadata_checksum,
            "shard_checksums": list(self.shard_checksums),
        }

