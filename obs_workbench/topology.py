from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Any, Iterable

from .canonical import digest
from .errors import (
    E_SILENCE_INVALID,
    E_SILENCE_RANGE_INVALID,
    E_TOPOLOGY_CYCLE,
    E_TOPOLOGY_CLOCK_INVALID,
    E_TOPOLOGY_DUPLICATE_INSTANCE,
    E_TOPOLOGY_DUPLICATE_SERVICE,
    E_TOPOLOGY_INVALID,
    E_TOPOLOGY_SELF_DEPENDENCY,
    E_TOPOLOGY_UNKNOWN_DEPENDENCY,
    E_TOPOLOGY_UNKNOWN_SERVICE,
    E_TOPOLOGY_VERSION_INVALID,
    WorkbenchError,
)


DEFAULT_WINDOW_MS = 60_000
DEFAULT_ALLOWED_LATENESS_MS = 10_000


@dataclass(frozen=True)
class Instance:
    instance_id: str
    service_name: str
    failure_domain: str
    weight: float


@dataclass(frozen=True)
class Service:
    service_name: str
    failure_domain: str
    upstream_services: tuple[str, ...]


@dataclass(frozen=True)
class SilenceWindow:
    silence_id: str
    instance_id: str | None
    service_name: str | None
    rule_name: str | None
    start_clock_ms: int
    end_clock_ms: int
    kind: str

    def matches(self, instance_id: str, service_name: str, rule_name: str, clock_ms: int) -> bool:
        if not (self.start_clock_ms <= clock_ms <= self.end_clock_ms):
            return False
        if self.instance_id is not None and self.instance_id != instance_id:
            return False
        if self.service_name is not None and self.service_name != service_name:
            return False
        if self.rule_name is not None and self.rule_name != rule_name:
            return False
        return True

    def overlaps(self, start_ms: int, end_ms: int) -> bool:
        return self.start_clock_ms <= end_ms and self.end_clock_ms >= start_ms

    def to_canonical(self) -> dict[str, Any]:
        return {
            "end_clock_ms": self.end_clock_ms,
            "instance_id": self.instance_id,
            "kind": self.kind,
            "rule_name": self.rule_name,
            "service_name": self.service_name,
            "silence_id": self.silence_id,
            "start_clock_ms": self.start_clock_ms,
        }


@dataclass(frozen=True)
class Topology:
    version: int
    effective_clock_ms: int
    window_ms: int
    allowed_lateness_ms: int
    services: dict[str, Service]
    instances: dict[str, Instance]
    silences: dict[str, SilenceWindow]
    parent_version: int | None = None
    source_kind: str = "snapshot"
    source_digest: str | None = None

    def instance(self, instance_id: str) -> Instance:
        try:
            return self.instances[instance_id]
        except KeyError as exc:
            raise WorkbenchError(
                E_TOPOLOGY_INVALID,
                "instance is not declared in topology",
                {"instance_id": instance_id, "topology_version": self.version},
            ) from exc

    def instances_for_service(self, service_name: str) -> tuple[Instance, ...]:
        return tuple(
            sorted(
                (item for item in self.instances.values() if item.service_name == service_name),
                key=lambda item: item.instance_id,
            )
        )

    def upstream_paths(self, source_service: str) -> tuple[tuple[str, ...], ...]:
        """Return every deterministic simple path from source to its upstream closure."""

        if source_service not in self.services:
            raise WorkbenchError(
                E_TOPOLOGY_UNKNOWN_SERVICE,
                "source service is not declared",
                {"service_name": source_service},
            )
        paths: list[tuple[str, ...]] = []

        def walk(path: tuple[str, ...]) -> None:
            paths.append(path)
            current = self.services[path[-1]]
            for upstream in sorted(current.upstream_services):
                if upstream in path:
                    raise WorkbenchError(
                        E_TOPOLOGY_CYCLE,
                        "dependency cycle detected while building evidence",
                        {"path": list(path + (upstream,))},
                    )
                walk(path + (upstream,))

        walk((source_service,))
        return tuple(sorted(set(paths), key=lambda item: (len(item), item)))

    def reverse_closure(self, roots: Iterable[str]) -> dict[str, tuple[str, ...]]:
        """Return affected dependents and one lexicographically stable path."""

        dependents: dict[str, list[str]] = {name: [] for name in self.services}
        for service in self.services.values():
            for upstream in service.upstream_services:
                dependents.setdefault(upstream, []).append(service.service_name)
        for values in dependents.values():
            values.sort()

        start = tuple(sorted(set(roots)))
        best: dict[str, tuple[str, ...]] = {}
        heap: list[tuple[int, tuple[str, ...]]] = []
        for root in start:
            if root in self.services:
                best[root] = (root,)
                heapq.heappush(heap, (1, (root,)))
        while heap:
            _, path = heapq.heappop(heap)
            current = path[-1]
            if best.get(current) != path:
                continue
            for dependent in dependents.get(current, []):
                candidate = path + (dependent,)
                previous = best.get(dependent)
                if previous is None or (len(candidate), candidate) < (len(previous), previous):
                    best[dependent] = candidate
                    heapq.heappush(heap, (len(candidate), candidate))
        return {key: best[key] for key in sorted(best)}

    def dependency_pairs(self) -> set[tuple[str, str]]:
        return {
            (service.service_name, upstream)
            for service in self.services.values()
            for upstream in service.upstream_services
        }

    def canonical(self) -> dict[str, Any]:
        return {
            "allowed_lateness_ms": self.allowed_lateness_ms,
            "effective_clock_ms": self.effective_clock_ms,
            "instances": [
                {
                    "failure_domain": item.failure_domain,
                    "instance_id": item.instance_id,
                    "service_name": item.service_name,
                    "weight": item.weight,
                }
                for item in sorted(self.instances.values(), key=lambda item: item.instance_id)
            ],
            "services": [
                {
                    "failure_domain": item.failure_domain,
                    "service_name": item.service_name,
                    "upstream_services": sorted(item.upstream_services),
                }
                for item in sorted(self.services.values(), key=lambda item: item.service_name)
            ],
            "silence_windows": [
                item.to_canonical()
                for item in sorted(self.silences.values(), key=lambda item: item.silence_id)
            ],
            "topology_version": self.version,
            "window_ms": self.window_ms,
        }

    def content_digest(self) -> str:
        return digest(self.canonical())


def _require_text(value: Any, field: str, code: str, details: dict[str, Any]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchError(code, f"{field} must be a non-empty string", {**details, "field": field})
    return value.strip()


def _require_int(
    value: Any,
    field: str,
    code: str,
    details: dict[str, Any],
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkbenchError(code, f"{field} must be an integer", {**details, "field": field})
    if minimum is not None and value < minimum:
        raise WorkbenchError(
            code,
            f"{field} must be >= {minimum}",
            {**details, "field": field, "minimum": minimum, "value": value},
        )
    return value


def _require_number(value: Any, field: str, details: dict[str, Any]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkbenchError(
            E_TOPOLOGY_INVALID,
            f"{field} must be numeric",
            {**details, "field": field},
        )
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise WorkbenchError(
            E_TOPOLOGY_INVALID,
            f"{field} must be a finite number greater than zero",
            {**details, "field": field, "value": number},
        )
    return number


def detect_cycle(services: dict[str, Service]) -> None:
    state: dict[str, int] = {}

    def visit(name: str, path: tuple[str, ...]) -> None:
        marker = state.get(name, 0)
        if marker == 1:
            raise WorkbenchError(
                E_TOPOLOGY_CYCLE,
                "topology dependency cycle detected",
                {"path": list(path + (name,))},
            )
        if marker == 2:
            return
        state[name] = 1
        for upstream in sorted(services[name].upstream_services):
            visit(upstream, path + (name,))
        state[name] = 2

    for service_name in sorted(services):
        visit(service_name, ())


def parse_silence_window(
    item: Any,
    instances: dict[str, Instance],
    services: dict[str, Service],
    index: int,
) -> SilenceWindow:
    if not isinstance(item, dict):
        raise WorkbenchError(E_TOPOLOGY_INVALID, "silence entry must be an object", {"index": index})
    silence_id = _require_text(
        item.get("silence_id", item.get("id")),
        "silence_id",
        E_TOPOLOGY_INVALID,
        {"index": index},
    )
    instance_id = item.get("instance_id")
    service_name = item.get("service_name")
    rule_name = item.get("rule_name")
    if instance_id is None and service_name is None:
        raise WorkbenchError(
            E_SILENCE_INVALID,
            "silence must target an instance or a service",
            {"silence_id": silence_id},
        )
    if instance_id is not None:
        instance_id = _require_text(
            instance_id, "instance_id", E_SILENCE_INVALID, {"silence_id": silence_id}
        )
        if instance_id not in instances:
            raise WorkbenchError(
                E_SILENCE_INVALID,
                "silence references an undeclared instance",
                {"silence_id": silence_id, "instance_id": instance_id},
            )
    if service_name is not None:
        service_name = _require_text(
            service_name, "service_name", E_SILENCE_INVALID, {"silence_id": silence_id}
        )
        if service_name not in services:
            raise WorkbenchError(
                E_SILENCE_INVALID,
                "silence references an undeclared service",
                {"silence_id": silence_id, "service_name": service_name},
            )
    if rule_name is not None:
        rule_name = _require_text(
            rule_name, "rule_name", E_SILENCE_INVALID, {"silence_id": silence_id}
        )
    start = _require_int(
        item.get("start_clock_ms"),
        "start_clock_ms",
        E_SILENCE_RANGE_INVALID,
        {"silence_id": silence_id},
        minimum=0,
    )
    end = _require_int(
        item.get("end_clock_ms"),
        "end_clock_ms",
        E_SILENCE_RANGE_INVALID,
        {"silence_id": silence_id},
        minimum=0,
    )
    if start > end:
        raise WorkbenchError(
            E_SILENCE_RANGE_INVALID,
            "silence start_clock_ms must not exceed end_clock_ms",
            {"end_clock_ms": end, "silence_id": silence_id, "start_clock_ms": start},
        )
    return SilenceWindow(
        silence_id=silence_id,
        instance_id=instance_id,
        service_name=service_name,
        rule_name=rule_name,
        start_clock_ms=start,
        end_clock_ms=end,
        kind=str(item.get("kind", "silence")),
    )


def parse_topology_payload(payload: Any) -> Topology:
    if not isinstance(payload, dict):
        raise WorkbenchError(E_TOPOLOGY_INVALID, "topology sample must be a JSON object")

    version = _require_int(
        payload.get("topology_version", payload.get("version")),
        "topology_version",
        E_TOPOLOGY_VERSION_INVALID,
        {},
        minimum=1,
    )
    effective_clock = _require_int(
        payload.get("effective_clock_ms", payload.get("effective_clock")),
        "effective_clock_ms",
        E_TOPOLOGY_CLOCK_INVALID,
        {"topology_version": version},
        minimum=0,
    )
    window_ms = _require_int(
        payload.get("window_ms", DEFAULT_WINDOW_MS),
        "window_ms",
        E_TOPOLOGY_INVALID,
        {"topology_version": version},
        minimum=1,
    )
    allowed_lateness = _require_int(
        payload.get("allowed_lateness_ms", DEFAULT_ALLOWED_LATENESS_MS),
        "allowed_lateness_ms",
        E_TOPOLOGY_INVALID,
        {"topology_version": version},
        minimum=0,
    )

    raw_services = payload.get("services", [])
    if isinstance(raw_services, dict):
        service_items = []
        for name, value in raw_services.items():
            item = dict(value) if isinstance(value, dict) else {}
            item.setdefault("service_name", name)
            service_items.append(item)
    elif isinstance(raw_services, list):
        service_items = raw_services
    else:
        raise WorkbenchError(E_TOPOLOGY_INVALID, "services must be a list or object")

    services: dict[str, Service] = {}
    nested_instances: list[tuple[dict[str, Any], str | None]] = []
    for index, item in enumerate(service_items):
        if not isinstance(item, dict):
            raise WorkbenchError(E_TOPOLOGY_INVALID, "service entry must be an object", {"index": index})
        name = _require_text(
            item.get("service_name", item.get("name")),
            "service_name",
            E_TOPOLOGY_INVALID,
            {"index": index},
        )
        if name in services:
            raise WorkbenchError(
                E_TOPOLOGY_DUPLICATE_SERVICE,
                "service names must be unique",
                {"service_name": name},
            )
        raw_upstreams = item.get("upstream_services", item.get("upstream", item.get("dependencies", [])))
        if not isinstance(raw_upstreams, list):
            raise WorkbenchError(
                E_TOPOLOGY_INVALID,
                "upstream_services must be a list",
                {"service_name": name},
            )
        upstreams: list[str] = []
        for raw_upstream in raw_upstreams:
            if isinstance(raw_upstream, dict):
                raw_upstream = raw_upstream.get("service_name", raw_upstream.get("service"))
            upstream = _require_text(
                raw_upstream,
                "upstream_service",
                E_TOPOLOGY_UNKNOWN_DEPENDENCY,
                {"service_name": name},
            )
            if upstream == name:
                raise WorkbenchError(
                    E_TOPOLOGY_SELF_DEPENDENCY,
                    "a service cannot depend on itself",
                    {"service_name": name},
                )
            if upstream not in upstreams:
                upstreams.append(upstream)
        services[name] = Service(
            service_name=name,
            failure_domain=str(item.get("failure_domain", "default")),
            upstream_services=tuple(sorted(upstreams)),
        )
        if isinstance(item.get("instances"), list):
            for instance_item in item["instances"]:
                if not isinstance(instance_item, dict):
                    raise WorkbenchError(
                        E_TOPOLOGY_INVALID,
                        "nested instance entry must be an object",
                        {"service_name": name},
                    )
                nested_instances.append((instance_item, name))

    for service in services.values():
        for upstream in service.upstream_services:
            if upstream not in services:
                raise WorkbenchError(
                    E_TOPOLOGY_UNKNOWN_DEPENDENCY,
                    "upstream dependency does not reference a declared service",
                    {"service_name": service.service_name, "upstream_service": upstream},
                )
    detect_cycle(services)

    raw_instances = payload.get("instances", [])
    if not isinstance(raw_instances, list):
        raise WorkbenchError(E_TOPOLOGY_INVALID, "instances must be a list")
    instance_items: list[tuple[dict[str, Any], str | None]] = [
        (item, None) for item in raw_instances
    ] + nested_instances
    instances: dict[str, Instance] = {}
    for index, (item, parent_service) in enumerate(instance_items):
        if not isinstance(item, dict):
            raise WorkbenchError(E_TOPOLOGY_INVALID, "instance entry must be an object", {"index": index})
        instance_id = _require_text(
            item.get("instance_id", item.get("id")),
            "instance_id",
            E_TOPOLOGY_INVALID,
            {"index": index},
        )
        service_name = _require_text(
            item.get("service_name", item.get("service", parent_service)),
            "service_name",
            E_TOPOLOGY_INVALID,
            {"instance_id": instance_id},
        )
        if instance_id in instances:
            raise WorkbenchError(
                E_TOPOLOGY_DUPLICATE_INSTANCE,
                "instance IDs must be unique",
                {"instance_id": instance_id},
            )
        if service_name not in services:
            raise WorkbenchError(
                E_TOPOLOGY_UNKNOWN_SERVICE,
                "instance references an undeclared service",
                {"instance_id": instance_id, "service_name": service_name},
            )
        weight = _require_number(
            item.get("weight", 1.0),
            "weight",
            {"instance_id": instance_id},
        )
        instances[instance_id] = Instance(
            instance_id=instance_id,
            service_name=service_name,
            failure_domain=str(item.get("failure_domain", services[service_name].failure_domain)),
            weight=weight,
        )

    raw_silences = payload.get("silence_windows", [])
    if not isinstance(raw_silences, list):
        raise WorkbenchError(E_TOPOLOGY_INVALID, "silence_windows must be a list")
    silences: dict[str, SilenceWindow] = {}
    for index, item in enumerate(raw_silences):
        silence = parse_silence_window(item, instances, services, index)
        if silence.silence_id in silences:
            raise WorkbenchError(
                E_TOPOLOGY_INVALID,
                "silence IDs must be unique",
                {"silence_id": silence.silence_id},
            )
        silences[silence.silence_id] = silence

    return Topology(
        version=version,
        effective_clock_ms=effective_clock,
        window_ms=window_ms,
        allowed_lateness_ms=allowed_lateness,
        services=services,
        instances=instances,
        silences=silences,
    )


def replace_service_upstreams(
    topology: Topology,
    *,
    add: Iterable[tuple[str, str]] = (),
    remove: Iterable[tuple[str, str]] = (),
    weights: dict[str, float] | None = None,
    silence_add: Iterable[SilenceWindow] = (),
    silence_remove: Iterable[str] = (),
    version: int,
    effective_clock_ms: int,
    parent_version: int,
    source_kind: str,
    source_digest: str,
) -> Topology:
    edges = topology.dependency_pairs()
    edges.update((service, upstream) for service, upstream in add)
    edges.difference_update((service, upstream) for service, upstream in remove)
    services: dict[str, Service] = {}
    for name, service in topology.services.items():
        upstreams = sorted(upstream for source, upstream in edges if source == name)
        services[name] = Service(
            service_name=name,
            failure_domain=service.failure_domain,
            upstream_services=tuple(upstreams),
        )
    detect_cycle(services)

    instances = dict(topology.instances)
    if weights:
        for instance_id, weight in weights.items():
            old = instances[instance_id]
            instances[instance_id] = Instance(
                instance_id=old.instance_id,
                service_name=old.service_name,
                failure_domain=old.failure_domain,
                weight=weight,
            )
    silences = dict(topology.silences)
    for silence_id in silence_remove:
        silences.pop(silence_id, None)
    for silence in silence_add:
        silences[silence.silence_id] = silence
    return Topology(
        version=version,
        effective_clock_ms=effective_clock_ms,
        window_ms=topology.window_ms,
        allowed_lateness_ms=topology.allowed_lateness_ms,
        services=services,
        instances=instances,
        silences=silences,
        parent_version=parent_version,
        source_kind=source_kind,
        source_digest=source_digest,
    )
