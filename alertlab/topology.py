from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import AlertLabError, invalid
from .util import canonical_json, read_json, sha256_json


@dataclass(frozen=True)
class Service:
    name: str
    fault_domain: str
    upstream: tuple[str, ...]
    instances: tuple[tuple[str, int], ...]

    def instance_weight(self, instance_id: str) -> int:
        for candidate, weight in self.instances:
            if candidate == instance_id:
                return weight
        raise KeyError(instance_id)


@dataclass(frozen=True)
class Topology:
    services: dict[str, Service]
    instance_to_service: dict[str, str]
    instance_weight: dict[str, int]
    document: dict[str, Any]
    fingerprint: str

    def service_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.services))

    def instance_ids(self, service: str) -> tuple[str, ...]:
        return tuple(instance for instance, _ in self.services[service].instances)

    def upstream_closure(
        self,
        service: str,
        override_upstream: Iterable[str] | None = None,
    ) -> dict[str, tuple[int, tuple[str, ...]]]:
        """Return service -> (shortest distance, lexicographically stable path)."""

        if service not in self.services:
            raise KeyError(service)
        result: dict[str, tuple[int, tuple[str, ...]]] = {service: (0, (service,))}
        queue: list[str] = [service]
        index = 0
        while index < len(queue):
            current = queue[index]
            index += 1
            distance, path = result[current]
            neighbours = set(self.services[current].upstream)
            if current == service:
                if override_upstream is not None:
                    neighbours = set(override_upstream)
            for upstream in sorted(neighbours):
                if upstream not in self.services:
                    continue
                candidate_path = (upstream,) + path
                existing = result.get(upstream)
                if existing is None:
                    result[upstream] = (distance + 1, candidate_path)
                    queue.append(upstream)
                elif distance + 1 == existing[0] and candidate_path < existing[1]:
                    result[upstream] = (distance + 1, candidate_path)
        return result


def _fail(code: str, message: str, **details: Any) -> AlertLabError:
    return invalid(code, message, **details)


def load_topology(path: str | Path) -> Topology:
    try:
        document = read_json(path)
    except FileNotFoundError:
        raise _fail("E_TOPOLOGY_FILE", "topology file does not exist", path=str(path))
    except (OSError, ValueError) as exc:
        raise _fail("E_TOPOLOGY_JSON", "topology file is not valid JSON", error=str(exc))

    if not isinstance(document, dict) or not isinstance(document.get("services"), list):
        raise _fail("E_TOPOLOGY_SCHEMA", "topology must contain a services list")

    services: dict[str, Service] = {}
    instance_to_service: dict[str, str] = {}
    instance_weight: dict[str, int] = {}
    pending_upstream: dict[str, tuple[str, ...]] = {}

    for raw in document["services"]:
        if not isinstance(raw, dict):
            raise _fail("E_TOPOLOGY_SCHEMA", "each service must be an object")
        name = raw.get("name")
        fault_domain = raw.get("fault_domain")
        upstream_raw = raw.get("upstream", [])
        instances_raw = raw.get("instances")
        if not isinstance(name, str) or not name:
            raise _fail("E_TOPOLOGY_SERVICE_NAME", "service name must be a non-empty string")
        if name in services:
            raise _fail("E_TOPOLOGY_DUPLICATE_SERVICE", "duplicate service name", service=name)
        if not isinstance(fault_domain, str) or not fault_domain:
            raise _fail(
                "E_TOPOLOGY_FAULT_DOMAIN",
                "fault_domain must be a non-empty string",
                service=name,
            )
        if not isinstance(upstream_raw, list) or any(
            not isinstance(item, str) or not item for item in upstream_raw
        ):
            raise _fail(
                "E_TOPOLOGY_UPSTREAM",
                "upstream must be a list of non-empty service names",
                service=name,
            )
        if len(upstream_raw) != len(set(upstream_raw)):
            raise _fail("E_TOPOLOGY_UPSTREAM", "upstream contains duplicates", service=name)
        if not isinstance(instances_raw, list) or not instances_raw:
            raise _fail(
                "E_TOPOLOGY_INSTANCES",
                "instances must be a non-empty list",
                service=name,
            )
        pairs: list[tuple[str, int]] = []
        for instance in instances_raw:
            if not isinstance(instance, dict):
                raise _fail(
                    "E_TOPOLOGY_INSTANCE_SCHEMA",
                    "instance must be an object",
                    service=name,
                )
            instance_id = instance.get("id")
            weight = instance.get("weight")
            if not isinstance(instance_id, str) or not instance_id:
                raise _fail(
                    "E_TOPOLOGY_INSTANCE_ID",
                    "instance id must be a non-empty string",
                    service=name,
                )
            if instance_id in instance_to_service:
                raise _fail(
                    "E_TOPOLOGY_DUPLICATE_INSTANCE",
                    "instance id must be globally unique",
                    instance_id=instance_id,
                )
            if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
                raise _fail(
                    "E_TOPOLOGY_INSTANCE_WEIGHT",
                    "instance weight must be a positive integer",
                    instance_id=instance_id,
                )
            pairs.append((instance_id, weight))
            instance_to_service[instance_id] = name
            instance_weight[instance_id] = weight
        pairs.sort()
        services[name] = Service(
            name=name,
            fault_domain=fault_domain,
            upstream=tuple(sorted(upstream_raw)),
            instances=tuple(pairs),
        )
        pending_upstream[name] = tuple(sorted(upstream_raw))

    for service_name, upstream in pending_upstream.items():
        for dependency in upstream:
            if dependency not in services:
                raise _fail(
                    "E_TOPOLOGY_UNKNOWN_UPSTREAM",
                    "upstream service is not declared",
                    service=service_name,
                    upstream=dependency,
                )
            if dependency == service_name:
                raise _fail(
                    "E_TOPOLOGY_CYCLE",
                    "service cannot depend on itself",
                    service=service_name,
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise _fail("E_TOPOLOGY_CYCLE", "upstream graph contains a cycle", service=name)
        visiting.add(name)
        for dependency in services[name].upstream:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in sorted(services):
        visit(name)

    topology_document = dict(document)
    topology_document["services"] = sorted(
        (dict(raw) for raw in document["services"]), key=lambda item: item["name"]
    )
    return Topology(
        services=services,
        instance_to_service=instance_to_service,
        instance_weight=instance_weight,
        document=topology_document,
        fingerprint=sha256_json(topology_document),
    )


def topology_from_document(document: dict[str, Any], fingerprint: str | None = None) -> Topology:
    """Rebuild a validated in-memory topology from a persisted document."""

    import tempfile

    from .util import atomic_write_text

    with tempfile.TemporaryDirectory(prefix="alertlab-topology-") as directory:
        path = Path(directory) / "topology.json"
        atomic_write_text(path, canonical_json(document))
        built = load_topology(path)
    return Topology(
        services=built.services,
        instance_to_service=built.instance_to_service,
        instance_weight=built.instance_weight,
        document=document,
        fingerprint=fingerprint or built.fingerprint,
    )
