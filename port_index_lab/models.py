"""Data models shared by generation, indexing, and reporting."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PortRecord:
    write_seq: int
    batch_id: int
    ship_id: str
    berth_id: str
    berth_time: str
    depart_time: str
    tonnage: int
    cargo_type: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PortRecord":
        return cls(
            write_seq=int(value["write_seq"]),
            batch_id=int(value["batch_id"]),
            ship_id=str(value["ship_id"]),
            berth_id=str(value["berth_id"]),
            berth_time=str(value["berth_time"]),
            depart_time=str(value["depart_time"]),
            tonnage=int(value["tonnage"]),
            cargo_type=str(value["cargo_type"]),
        )


@dataclass(frozen=True, slots=True)
class Batch:
    batch_id: int
    records: tuple[PortRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "record_count": len(self.records),
            "write_seq_range": [
                self.records[0].write_seq,
                self.records[-1].write_seq,
            ]
            if self.records
            else [],
            "records": [record.to_dict() for record in self.records],
        }


@dataclass(frozen=True, slots=True)
class QuerySpec:
    query_id: str
    kind: str
    start: str
    end: str
    berth_id: str | None
    description: str

    def matches(self, record: PortRecord) -> bool:
        if self.kind == "berth_overlap":
            return (
                record.berth_id == self.berth_id
                and record.berth_time <= self.end
                and record.depart_time >= self.start
            )
        if self.kind == "berth_time_closed":
            return self.start <= record.berth_time <= self.end
        raise ValueError(f"unknown query kind: {self.kind}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "berth_id": self.berth_id,
            "description": self.description,
        }
