"""Domain records emitted by the deterministic generator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TruthEvent:
    incident_id: str
    occurred_at_ms: int
    event_type: str
    severity: int
    address: tuple[str, ...]
    persons: tuple[dict[str, str], ...]
    station_id: str
    vehicle_id: str


@dataclass(frozen=True)
class SourceBatch:
    version: int
    records: tuple[dict[str, Any], ...]


@dataclass
class GeneratedProblem:
    seed: int
    base_records: dict[int, list[dict[str, Any]]]
    vehicles: list[dict[str, Any]]
    water_sources: list[dict[str, Any]]
    queries: list[dict[str, Any]]
    updates: list[dict[str, Any]]
    truth_events: list[TruthEvent] = field(repr=False)

