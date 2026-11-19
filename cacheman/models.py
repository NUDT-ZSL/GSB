"""Internal data structures shared by loaders and the index engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceInfo:
    source_id: str
    source_version: int
    manifest_id: str
    manifest_version: int
    logical_clock_ms: int
    source_root: str
    source_hash: str
    generation_min: int | None = None
    generation_max: int | None = None
    shard_hashes: tuple[str, ...] = ()
    descriptor: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "logical_clock_ms": self.logical_clock_ms,
            "source_root": self.source_root,
            "source_hash": self.source_hash,
            "generation_min": self.generation_min,
            "generation_max": self.generation_max,
            "shard_hashes": list(self.shard_hashes),
            "descriptor": self.descriptor,
        }


@dataclass(frozen=True)
class Candidate:
    task_key: str
    artifact: str
    original_digests: tuple[str, ...]
    size_bytes: int
    absolute_written_ms: int
    source_version: int
    source_id: str
    source_manifest_id: str
    source_manifest_version: int
    source_logical_clock_ms: int
    source_cache_generation: int | None
    source_hit_tag: str | None
    original_time: dict[str, Any]
    content_path: str
    source_root: str
    resolved_content_path: Path

    @property
    def digests(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.original_digests)))

    @property
    def unified_cache_generation(self) -> int:
        return 0 if self.source_cache_generation is None else self.source_cache_generation

    @property
    def unified_hit_tag(self) -> str:
        return self.source_hit_tag or "legacy-unknown"

    @property
    def precision_tag(self) -> str:
        return "relative_seconds_migrated" if self.source_version == 1 else "absolute_milliseconds"

    def rank(self) -> tuple[Any, ...]:
        return (
            self.source_logical_clock_ms,
            self.unified_cache_generation,
            self.absolute_written_ms,
            2 if self.source_version == 2 else 1,
            self.source_manifest_version,
            self.source_id,
            self.digests,
            self.size_bytes,
            self.content_path,
        )

    def to_report_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "artifact": self.artifact,
            "digests": list(self.digests),
            "original_digests": list(self.original_digests),
            "size_bytes": self.size_bytes,
            "absolute_written_ms": self.absolute_written_ms,
            "source_version": self.source_version,
            "source_id": self.source_id,
            "source_manifest_id": self.source_manifest_id,
            "source_manifest_version": self.source_manifest_version,
            "source_logical_clock_ms": self.source_logical_clock_ms,
            "source_cache_generation": self.source_cache_generation,
            "source_hit_tag": self.source_hit_tag,
            "original_time": self.original_time,
            "precision_tag": self.precision_tag,
            "content_path": self.content_path,
            "rank_key": list(self.rank()),
        }


@dataclass(frozen=True)
class Tombstone:
    task_key: str
    artifact: str
    digest: str
    invalidated_at_ms: int
    cache_generation: int
    source_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "artifact": self.artifact,
            "digest": self.digest,
            "invalidated_at_ms": self.invalidated_at_ms,
            "cache_generation": self.cache_generation,
            "source_id": self.source_id,
        }


@dataclass
class LoadedDataset:
    candidates: list[Candidate] = field(default_factory=list)
    tombstones: list[Tombstone] = field(default_factory=list)
    sources: list[SourceInfo] = field(default_factory=list)

    def add(self, other: "LoadedDataset") -> None:
        self.candidates.extend(other.candidates)
        self.tombstones.extend(other.tombstones)
        self.sources.extend(other.sources)

    def default_now_ms(self) -> int:
        clocks = [source.logical_clock_ms for source in self.sources]
        clocks.extend(candidate.absolute_written_ms for candidate in self.candidates)
        return max(clocks, default=0)

    def semantic_descriptor(self) -> dict[str, Any]:
        return {
            "sources": sorted(
                (source.to_dict() for source in self.sources),
                key=lambda item: item["source_id"],
            ),
            "tombstones": sorted(
                (tombstone.to_dict() for tombstone in self.tombstones),
                key=lambda item: (
                    item["task_key"],
                    item["artifact"],
                    item["digest"],
                    item["source_id"],
                ),
            ),
        }
