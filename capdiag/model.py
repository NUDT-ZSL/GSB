from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class InstanceSpec:
    instance_id: str
    expected_request_rate: Decimal


@dataclass(frozen=True)
class ServiceSpec:
    service_name: str
    service_level: str
    expected_request_rate: Decimal
    instances: tuple[InstanceSpec, ...]


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    origin_logical_ms: int
    precision_tolerance: Decimal
    services: tuple[ServiceSpec, ...]
    instance_to_service: dict[str, str] = field(default_factory=dict)

    def service(self, name: str) -> ServiceSpec:
        for service in self.services:
            if service.service_name == name:
                return service
        raise KeyError(name)

    def service_for_instance(self, instance_id: str) -> str | None:
        return self.instance_to_service.get(instance_id)


@dataclass(frozen=True)
class TraceRecord:
    request_seq: int
    service_name: str
    instance_id: str
    logical_clock_ms: int
    duration_ms: Decimal
    success: bool
    dropped: bool
    source_line: int


@dataclass
class TraceBundle:
    records: list[TraceRecord] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Window:
    scope_type: str
    scope_id: str
    window_index: int
    start_ms: int
    end_ms: int
    step_ms: int

    @property
    def key(self) -> str:
        return f"{self.scope_type}:{self.scope_id}:{self.window_index}"
