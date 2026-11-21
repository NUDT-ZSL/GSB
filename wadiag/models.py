from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class WriteRecord:
    seq: int
    key: str
    value_bytes: int
    op: str
    clock_ms: int

    @property
    def payload_bytes(self) -> int:
        return len(self.key.encode("utf-8")) + self.value_bytes

    @property
    def footprint_bytes(self) -> int:
        return self.payload_bytes + 16

    @property
    def is_tombstone(self) -> bool:
        return self.op == "delete"

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "key": self.key,
            "value_bytes": self.value_bytes,
            "op": self.op,
            "clock_ms": self.clock_ms,
            "footprint_bytes": self.footprint_bytes,
        }


@dataclass(frozen=True)
class StoredRecord:
    seq: int
    key: str
    value_bytes: int
    op: str
    clock_ms: int
    origin_generation: int

    @property
    def payload_bytes(self) -> int:
        return len(self.key.encode("utf-8")) + self.value_bytes

    @property
    def footprint_bytes(self) -> int:
        return self.payload_bytes + 16

    @property
    def is_tombstone(self) -> bool:
        return self.op == "delete"

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "key": self.key,
            "value_bytes": self.value_bytes,
            "op": self.op,
            "clock_ms": self.clock_ms,
            "origin_generation": self.origin_generation,
        }

    @classmethod
    def from_write(cls, record: WriteRecord, generation: int = 0) -> "StoredRecord":
        return cls(
            seq=record.seq,
            key=record.key,
            value_bytes=record.value_bytes,
            op=record.op,
            clock_ms=record.clock_ms,
            origin_generation=generation,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StoredRecord":
        return cls(
            seq=int(payload["seq"]),
            key=str(payload["key"]),
            value_bytes=int(payload["value_bytes"]),
            op=str(payload["op"]),
            clock_ms=int(payload["clock_ms"]),
            origin_generation=int(payload.get("origin_generation", 0)),
        )


@dataclass(frozen=True)
class StorageConfig:
    schema_version: int
    memtable_capacity_bytes: int
    segment_size_limit_bytes: int
    level_count: int
    level_trigger_thresholds: tuple[int, ...]
    tombstone_retention_generations: int
    compression_block_size_bytes: int
    merge_budget_bytes: int
    logical_clock_step_ms: int
    level_compression_ratios: tuple[str, ...]
    bloom_bits_per_key: int = 10
    bloom_hash_count: int = 3

    def compression_decimal(self, level: int) -> Decimal:
        return Decimal(self.level_compression_ratios[level])

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "memtable_capacity_bytes": self.memtable_capacity_bytes,
            "segment_size_limit_bytes": self.segment_size_limit_bytes,
            "level_count": self.level_count,
            "level_trigger_thresholds": list(self.level_trigger_thresholds),
            "tombstone_retention_generations": self.tombstone_retention_generations,
            "compression_block_size_bytes": self.compression_block_size_bytes,
            "merge_budget_bytes": self.merge_budget_bytes,
            "logical_clock_step_ms": self.logical_clock_step_ms,
            "level_compression_ratios": list(self.level_compression_ratios),
            "bloom_bits_per_key": self.bloom_bits_per_key,
            "bloom_hash_count": self.bloom_hash_count,
        }


@dataclass(frozen=True)
class DiagnosticConfig:
    schema_version: int
    fixed_scale: int
    float_tolerance: str
    error_budget: str
    sample_ratio: str

    @property
    def tolerance_decimal(self) -> Decimal:
        return Decimal(self.float_tolerance)

    @property
    def error_budget_decimal(self) -> Decimal:
        return Decimal(self.error_budget)

    @property
    def sample_ratio_decimal(self) -> Decimal:
        return Decimal(self.sample_ratio)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fixed_scale": self.fixed_scale,
            "float_tolerance": self.float_tolerance,
            "error_budget": self.error_budget,
            "sample_ratio": self.sample_ratio,
        }


@dataclass(frozen=True)
class ClockTable:
    schema_version: int
    start_ms: int
    step_ms: int
    overrides: dict[int, int] = field(default_factory=dict)

    def timestamp_for(self, seq: int, explicit_ms: int | None = None) -> int:
        override = self.overrides.get(seq)
        if override is not None:
            return override
        if explicit_ms is not None:
            return explicit_ms
        return self.start_ms + seq * self.step_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "start_ms": self.start_ms,
            "step_ms": self.step_ms,
            "overrides": {str(key): self.overrides[key] for key in sorted(self.overrides)},
        }


@dataclass(frozen=True)
class Trajectory:
    schema_version: int
    trajectory_id: str
    records: tuple[WriteRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "trajectory_id": self.trajectory_id,
            "records": [record.to_dict() for record in self.records],
        }
