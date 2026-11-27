"""Core deterministic data structures for the quality-index solver."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable


IndexKey = tuple[str, str, str, int, str]


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def make_index_key(
    process: str,
    shift: str,
    batch_prefix: str,
    logical_time: int,
    event_id: str,
) -> IndexKey:
    """Build the mandatory ordered edge key.

    process and shift group first, batch prefix is lexicographic, logical time
    ascends inside a prefix, and event_id is the stable tie-breaker.
    """

    return (
        str(process),
        str(shift),
        str(batch_prefix),
        int(logical_time),
        str(event_id),
    )


def key_to_list(key: IndexKey) -> list[Any]:
    return list(key)


@dataclass(frozen=True)
class QualityEvent:
    event_id: str
    process: str
    shift: str
    batch_prefix: str
    logical_time: int
    station: str
    metric: str
    value: float
    result: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "QualityEvent":
        required = (
            "event_id",
            "process",
            "shift",
            "batch_prefix",
            "logical_time",
            "station",
            "metric",
            "value",
            "result",
        )
        missing = [name for name in required if name not in raw]
        if missing:
            raise ValueError(f"质量事件缺少字段: {', '.join(missing)}")
        return cls(
            event_id=str(raw["event_id"]),
            process=str(raw["process"]),
            shift=str(raw["shift"]),
            batch_prefix=str(raw["batch_prefix"]),
            logical_time=int(raw["logical_time"]),
            station=str(raw["station"]),
            metric=str(raw["metric"]),
            value=float(raw["value"]),
            result=str(raw["result"]),
        )

    @property
    def partition(self) -> tuple[str, str]:
        return (self.process, self.shift)

    @property
    def key(self) -> IndexKey:
        return make_index_key(
            self.process,
            self.shift,
            self.batch_prefix,
            self.logical_time,
            self.event_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "process": self.process,
            "shift": self.shift,
            "batch_prefix": self.batch_prefix,
            "logical_time": self.logical_time,
            "station": self.station,
            "metric": self.metric,
            "value": self.value,
            "result": self.result,
        }


@dataclass(frozen=True)
class IndexEntry:
    key: IndexKey
    event_id: str
    station: str
    memtable_id: str
    logical_offset: int
    metric: str
    value: float
    result: str

    @classmethod
    def from_event(
        cls, event: QualityEvent, memtable_id: str, logical_offset: int
    ) -> "IndexEntry":
        return cls(
            key=event.key,
            event_id=event.event_id,
            station=event.station,
            memtable_id=memtable_id,
            logical_offset=logical_offset,
            metric=event.metric,
            value=event.value,
            result=event.result,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": key_to_list(self.key),
            "event_id": self.event_id,
            "station": self.station,
            "memtable_id": self.memtable_id,
            "logical_offset": self.logical_offset,
            "metric": self.metric,
            "value": self.value,
            "result": self.result,
        }


def segment_payload_size(
    partition: tuple[str, str], entries: Iterable[IndexEntry]
) -> int:
    """Exact logical byte size used by all capacity and occupancy accounting.

    The payload contains the partition header and all entry records, including
    the key-to-event mapping data. Segment id and level are deliberately outside
    this payload so moving a segment between levels does not alter its size.
    """

    payload = {
        "partition": list(partition),
        "entries": [entry.to_dict() for entry in entries],
    }
    return len(canonical_bytes(payload))


@dataclass
class Segment:
    segment_id: str
    partition: tuple[str, str]
    level: int
    entries: tuple[IndexEntry, ...]
    created_order: int
    size_bytes: int

    @classmethod
    def create(
        cls,
        segment_id: str,
        partition: tuple[str, str],
        level: int,
        entries: Iterable[IndexEntry],
        created_order: int,
    ) -> "Segment":
        frozen_entries = tuple(entries)
        return cls(
            segment_id=segment_id,
            partition=partition,
            level=level,
            entries=frozen_entries,
            created_order=created_order,
            size_bytes=segment_payload_size(partition, frozen_entries),
        )

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    def key_event_id_mapping(self) -> dict[str, str]:
        return {
            json.dumps(
                key_to_list(entry.key),
                ensure_ascii=False,
                separators=(",", ":"),
            ): entry.event_id
            for entry in self.entries
        }

    def to_dict(self, include_entries: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "segment_id": self.segment_id,
            "partition": list(self.partition),
            "level": self.level,
            "entry_count": self.entry_count,
            "size_bytes": self.size_bytes,
            "created_order": self.created_order,
        }
        if include_entries:
            result["key_event_id_mapping"] = self.key_event_id_mapping()
            result["entries"] = [
                {
                    "segment_offset": offset,
                    **entry.to_dict(),
                }
                for offset, entry in enumerate(self.entries)
            ]
        return result


@dataclass(frozen=True)
class LevelConfig:
    name: str
    max_segments: int
    max_bytes: int

    @classmethod
    def from_dict(cls, raw: dict[str, Any], index: int) -> "LevelConfig":
        name = str(raw.get("name", f"L{index}"))
        max_segments = int(raw["max_segments"])
        max_bytes = int(raw["max_bytes"])
        if max_segments < 0 or max_bytes < 0:
            raise ValueError("层容量不得为负数")
        return cls(name=name, max_segments=max_segments, max_bytes=max_bytes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "max_segments": self.max_segments,
            "max_bytes": self.max_bytes,
        }


@dataclass(frozen=True)
class IndexConfig:
    flush_threshold: int
    merge_invalid_ratio: float
    partition_max_bytes: int
    levels: tuple[LevelConfig, ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "IndexConfig":
        levels_raw = raw.get("levels")
        if not isinstance(levels_raw, list) or not levels_raw:
            raise ValueError("config.levels 必须是非空数组")
        levels = tuple(
            LevelConfig.from_dict(item, index)
            for index, item in enumerate(levels_raw)
        )
        flush_threshold = int(raw["flush_threshold"])
        merge_invalid_ratio = float(raw["merge_invalid_ratio"])
        partition_max_bytes = int(raw["partition_max_bytes"])
        if flush_threshold < 1:
            raise ValueError("flush_threshold 必须 >= 1")
        if not 0.0 < merge_invalid_ratio <= 1.0:
            raise ValueError("merge_invalid_ratio 必须位于 (0, 1]")
        if partition_max_bytes < 1:
            raise ValueError("partition_max_bytes 必须 >= 1")
        return cls(
            flush_threshold=flush_threshold,
            merge_invalid_ratio=merge_invalid_ratio,
            partition_max_bytes=partition_max_bytes,
            levels=levels,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "flush_threshold": self.flush_threshold,
            "merge_invalid_ratio": self.merge_invalid_ratio,
            "partition_max_bytes": self.partition_max_bytes,
            "levels": [level.to_dict() for level in self.levels],
        }
