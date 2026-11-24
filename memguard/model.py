"""Normalized, integer-only data model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class RegionDecl:
    region_id: str
    generation: int
    tombstone: bool
    arena_size: int
    start: int
    end: int
    life_start_ms: int
    life_end_ms: Optional[int]  # None = open-ended
    parent_id: Optional[str]
    file_id: str = ""
    line_no: int = 0


@dataclass(frozen=True)
class TraceRecord:
    trace_id: str
    access_seq: int
    region_id: str
    base_offset: int
    access_size: int
    access_kind: str  # "read" | "write"
    arena_size: int
    ts_ms: int
    file_id: str = ""
    line_no: int = 0


@dataclass(frozen=True)
class Reject:
    file_id: str
    line_no: int
    code: str
    detail: str

    def as_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "line_no": self.line_no,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass
class ParseOutcome:
    decls: list = field(default_factory=list)        # list[RegionDecl]
    traces: list = field(default_factory=list)       # list[TraceRecord]
    rejects: list = field(default_factory=list)      # list[Reject]
