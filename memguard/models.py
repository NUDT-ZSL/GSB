"""Core immutable data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Tuple


def _json_int(value: int) -> int:
    return int(value)


@dataclass(frozen=True)
class Rejection:
    source_file: str
    line_number: Optional[int]
    business_code: str
    message: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "line_number": self.line_number,
            "business_code": self.business_code,
            "message": self.message,
            "detail": dict(self.detail),
        }

    @property
    def sort_key(self) -> tuple[str, int, str, str]:
        return (
            self.source_file,
            -1 if self.line_number is None else self.line_number,
            self.business_code,
            self.message,
        )


@dataclass(frozen=True)
class Region:
    region_id: str
    arena_id: str
    arena_size: int
    start_offset: int
    end_offset: int
    lifetime_start_ms: int
    lifetime_end_ms: int
    generation: int
    parent_region_id: Optional[str]
    source_file: str
    line_number: int

    @property
    def interval(self) -> tuple[int, int]:
        return (self.start_offset, self.end_offset)

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id,
            "arena_id": self.arena_id,
            "arena_size": self.arena_size,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "lifetime_start_ms": self.lifetime_start_ms,
            "lifetime_end_ms": self.lifetime_end_ms,
            "generation": self.generation,
            "parent_region_id": self.parent_region_id,
        }


@dataclass(frozen=True)
class TraceRecord:
    trace_id: str
    access_seq: int
    region_id: str
    base_offset: int
    access_size: int
    access_kind: str
    arena_size: int
    timestamp_ms: int
    source_file: str
    line_number: int
    sender_interval: Optional[tuple[int, int]] = None
    sync_marker: Optional[str] = None
    expected_interval: Optional[tuple[int, int]] = None
    expected_business_code: Optional[str] = None

    @property
    def access_interval(self) -> tuple[int, int]:
        return (self.base_offset, self.base_offset + self.access_size)

    @property
    def stable_input_key(self) -> tuple[str, int, str]:
        return (self.source_file, self.line_number, self.trace_id)


@dataclass(frozen=True)
class TraceRef:
    source_file: str
    line_number: int
    byte_offset: int
    byte_length: int
    arena_id: str
    access_seq: int
    region_id: str
    access_kind: str
    trace_id: str


@dataclass
class Manifest:
    regions: dict[str, Region]
    refs_by_arena: dict[str, list[TraceRef]]
    rejections: list[Rejection]
    region_files: list[str]
    trace_files: list[str]
    region_layout_digest: str
    trace_input_digest: str
    layout_key: str
    trace_records_seen: int = 0


@dataclass(frozen=True)
class Verdict:
    trace_id: str
    access_seq: int
    region_id: str
    arena_id: str
    access_kind: str
    business_code: str
    status: str
    evidence: Mapping[str, Any]
    access_end: int
    source_file: str
    line_number: int

    @property
    def is_oob(self) -> bool:
        from .codes import OOB_CODES

        return self.business_code in OOB_CODES

    @property
    def is_error(self) -> bool:
        return self.status == "error"

    @property
    def stable_key(self) -> tuple[int, str, int, str]:
        return (self.access_seq, self.trace_id, self.line_number, self.source_file)


@dataclass(frozen=True)
class ArenaRun:
    arena_id: str
    records: Tuple[TraceRecord, ...]
    verdicts: Tuple[Verdict, ...]
    loaded_bytes: int
    elapsed_ms: float
    degradation_codes: Tuple[str, ...]
    rejections: Tuple[Rejection, ...] = ()


@dataclass(frozen=True)
class BatchDeltaEntry:
    region_id: str
    access_kind: str
    access_seq: int
    oob: bool
    access_end: int


@dataclass(frozen=True)
class LedgerBatch:
    batch_id: str
    first_seq: int
    last_seq: int
    entries: Tuple[BatchDeltaEntry, ...]

    @property
    def record_count(self) -> int:
        return len(self.entries)


def nullable_interval(value: Optional[Sequence[int]]) -> Optional[tuple[int, int]]:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    left, right = value
    if type(left) is not int or type(right) is not int:
        return None
    if left < 0 or right < left:
        return None
    return (_json_int(left), _json_int(right))
