"""Independent ordered-index reference implementation.

This module intentionally does not import the LSM engine. It starts from raw
write records and invalidation markers only, sorts the surviving records anew,
and therefore acts as an independent oracle for every query.
"""

from __future__ import annotations

import json
from typing import Any


def _reference_key(record: dict[str, Any]) -> tuple[str, str, str, int, str]:
    return (
        str(record["process"]),
        str(record["shift"]),
        str(record["batch_prefix"]),
        int(record["logical_time"]),
        str(record["event_id"]),
    )


def _matches(
    record: dict[str, Any],
    station: str | None,
    batch_prefix: str | None,
) -> bool:
    if station is not None and str(record["station"]) != station:
        return False
    if batch_prefix is not None and not str(
        record["batch_prefix"]
    ).startswith(batch_prefix):
        return False
    return True


class ReferenceIndex:
    """A flat, segment-free reference index."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.invalid_event_ids: set[str] = set()
        self.write_order: list[str] = []

    def ingest(self, raw_events: list[dict[str, Any]]) -> None:
        for raw in raw_events:
            event_id = str(raw["event_id"])
            if event_id in self.records:
                raise ValueError(f"参考索引收到重复事件: {event_id}")
            record = dict(raw)
            record["event_id"] = event_id
            record["process"] = str(record["process"])
            record["shift"] = str(record["shift"])
            record["batch_prefix"] = str(record["batch_prefix"])
            record["logical_time"] = int(record["logical_time"])
            record["station"] = str(record["station"])
            record["_key"] = _reference_key(record)
            self.records[event_id] = record
            self.write_order.append(event_id)

    def invalidate(self, spec: dict[str, Any]) -> list[str]:
        station_raw = spec.get("station")
        prefix_raw = spec.get("batch_prefix")
        station = str(station_raw) if station_raw else None
        batch_prefix = str(prefix_raw) if prefix_raw else None
        newly_invalid: list[str] = []
        for event_id in self.write_order:
            record = self.records[event_id]
            if event_id in self.invalid_event_ids:
                continue
            if _matches(record, station, batch_prefix):
                self.invalid_event_ids.add(event_id)
                newly_invalid.append(event_id)
        return newly_invalid

    def query(
        self, process: str, shift: str, batch_prefix: str
    ) -> list[dict[str, Any]]:
        candidates = [
            record
            for event_id, record in self.records.items()
            if event_id not in self.invalid_event_ids
            and record["process"] == process
            and record["shift"] == shift
            and record["batch_prefix"].startswith(batch_prefix)
        ]
        candidates.sort(key=lambda record: record["_key"])
        return candidates

    def active_records(self) -> list[dict[str, Any]]:
        return [
            record
            for event_id, record in self.records.items()
            if event_id not in self.invalid_event_ids
        ]

    def exported_records(self) -> list[dict[str, Any]]:
        result = []
        for event_id in self.write_order:
            record = dict(self.records[event_id])
            key = record.pop("_key")
            record["key"] = list(key)
            record["invalid"] = event_id in self.invalid_event_ids
            result.append(record)
        return result


def run_reference(workload: dict[str, Any]) -> dict[str, Any]:
    """Run the reference alone and return its per-query answers."""

    reference = ReferenceIndex()
    answers: list[dict[str, Any]] = []
    for step in workload["timeline"]:
        time = int(step["time"])
        reference.ingest(step.get("writes", []))
        for spec in step.get("invalidations", []):
            invalidated = reference.invalidate(spec)
            answers.append(
                {
                    "type": "invalidation",
                    "time": time,
                    "invalidation_id": spec.get("invalidation_id"),
                    "newly_invalidated_event_ids": invalidated,
                }
            )
        for query in step.get("queries", []):
            records = reference.query(
                str(query["process"]),
                str(query["shift"]),
                str(query["batch_prefix"]),
            )
            answers.append(
                {
                    "type": "query",
                    "time": time,
                    "query_id": str(query["query_id"]),
                    "hits": [
                        {
                            "event_id": record["event_id"],
                            "key": list(record["_key"]),
                        }
                        for record in records
                    ],
                }
            )
    return {
        "format": "quality-index-reference/v1",
        "answers": answers,
        "final_records": reference.exported_records(),
    }


def dumps_reference(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2) + "\n"
