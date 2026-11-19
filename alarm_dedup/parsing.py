"""Validation and canonicalization for topology, events, patches, and windows."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .canonical import digest
from .errors import fail

SEVERITIES = {"info": 1, "warning": 2, "critical": 3}
WINDOW_KINDS = {"silence", "maintenance"}


@dataclass(frozen=True)
class Instance:
    instance_id: str
    service: str
    fault_domain: str
    weight: float


@dataclass(frozen=True)
class Service:
    name: str
    upstream_dependencies: tuple[str, ...]
    instances: tuple[Instance, ...]


@dataclass(frozen=True)
class Topology:
    version: int
    services: dict[str, Service]
    instances: dict[str, Instance]
    payload: dict[str, Any]
    digest: str

    @property
    def instance_service(self) -> dict[str, str]:
        return {key: value.service for key, value in self.instances.items()}

    @property
    def upstream(self) -> dict[str, tuple[str, ...]]:
        return {name: svc.upstream_dependencies for name, svc in self.services.items()}

    @property
    def downstream(self) -> dict[str, tuple[str, ...]]:
        reverse: dict[str, set[str]] = {name: set() for name in self.services}
        for service, dependencies in self.upstream.items():
            for dependency in dependencies:
                reverse[dependency].add(service)
        return {name: tuple(sorted(values)) for name, values in reverse.items()}


@dataclass(frozen=True)
class Event:
    event_seq: int
    instance_id: str
    service: str
    rule: str
    severity: str
    logical_ts: int
    metric_value: float
    silenced: bool
    maintenance: bool

    def payload(self) -> dict[str, Any]:
        return {
            "event_seq": self.event_seq,
            "instance_id": self.instance_id,
            "service": self.service,
            "rule": self.rule,
            "severity": self.severity,
            "logical_ts": self.logical_ts,
            "metric_value": self.metric_value,
            "silenced": self.silenced,
            "maintenance": self.maintenance,
        }


@dataclass(frozen=True)
class Window:
    window_id: str
    kind: str
    service: str
    instance_id: str | None
    rule: str | None
    start_ts: int
    end_ts: int

    def payload(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "kind": self.kind,
            "service": self.service,
            "instance_id": self.instance_id,
            "rule": self.rule,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
        }

    def covers(self, event: dict[str, Any]) -> bool:
        if self.service != event["service"]:
            return False
        if self.instance_id is not None and self.instance_id != event["instance_id"]:
            return False
        if self.rule is not None and self.rule != event["rule"]:
            return False
        return self.start_ts <= event["logical_ts"] < self.end_ts


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        fail("INPUT_SHAPE_INVALID", "Required text field is invalid", field=field, value=value)
    return value.strip()


def _name(entry: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key in entry:
            return _required_text(entry[key], key)
    fail("INPUT_SHAPE_INVALID", "Required identifier is missing", fields=list(keys))


def _detect_cycle(upstream: dict[str, tuple[str, ...]]) -> list[str] | None:
    state: dict[str, int] = {name: 0 for name in upstream}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for dependency in upstream[node]:
            if state[dependency] == 0:
                found = visit(dependency)
                if found:
                    return found
            elif state[dependency] == 1:
                start = stack.index(dependency)
                return stack[start:] + [dependency]
        stack.pop()
        state[node] = 2
        return None

    for service in sorted(upstream):
        if state[service] == 0:
            found = visit(service)
            if found:
                return found
    return None


def parse_topology(payload: Any) -> Topology:
    if not isinstance(payload, dict):
        fail("INPUT_SHAPE_INVALID", "Topology must be a JSON object")
    version = payload.get("topology_version", payload.get("version"))
    if not _is_int(version) or version <= 0:
        fail("TOPOLOGY_VERSION_INVALID", "topology_version must be a positive integer")

    raw_services = payload.get("services")
    if raw_services is None and isinstance(payload.get("instances"), list):
        raw_services = []
    if isinstance(raw_services, dict):
        raw_services = [
            {"name": name, **(value if isinstance(value, dict) else {})}
            for name, value in sorted(raw_services.items())
        ]
    if not isinstance(raw_services, list):
        fail("INPUT_SHAPE_INVALID", "services must be a list or object")

    top_instances = payload.get("instances", [])
    if top_instances is None:
        top_instances = []
    if not isinstance(top_instances, list):
        fail("INPUT_SHAPE_INVALID", "instances must be a list")
    top_dependencies = payload.get("dependencies")
    if top_dependencies is not None and not isinstance(top_dependencies, dict):
        fail("INPUT_SHAPE_INVALID", "Top-level dependencies must be an object keyed by service")

    service_entries: dict[str, dict[str, Any]] = {}
    for raw in raw_services:
        if not isinstance(raw, dict):
            fail("INPUT_SHAPE_INVALID", "Each service entry must be an object")
        name = _name(raw, "service", "name")
        if name in service_entries:
            fail("TOPOLOGY_DUPLICATE_SERVICE", "Duplicate service name", service=name)
        if any(key in raw for key in ("upstream_dependencies", "dependencies", "upstreams")):
            dependencies = raw.get(
                "upstream_dependencies",
                raw.get("dependencies", raw.get("upstreams", [])),
            )
        else:
            dependencies = top_dependencies.get(name, []) if top_dependencies else []
        if dependencies is None:
            dependencies = []
        if not isinstance(dependencies, list):
            fail("INPUT_SHAPE_INVALID", "upstream_dependencies must be a list", service=name)
        service_entries[name] = {
            "name": name,
            "dependencies": sorted({_required_text(dep, "upstream_dependency") for dep in dependencies}),
            "instances": raw.get("instances", []),
        }

    known_services = set(service_entries)
    for name, entry in service_entries.items():
        for dependency in entry["dependencies"]:
            if dependency == name:
                fail("TOPOLOGY_SELF_DEPENDENCY", "Service depends on itself", service=name)
            if dependency not in known_services:
                fail(
                    "TOPOLOGY_UNKNOWN_DEPENDENCY",
                    "Dependency references an undeclared service",
                    service=name,
                    dependency=dependency,
                )

    instance_rows: list[dict[str, Any]] = []
    for name, entry in service_entries.items():
        embedded = entry["instances"]
        if embedded is None:
            embedded = []
        raw_service = next(
            (
                item
                for item in raw_services
                if isinstance(item, dict)
                and _name(item, "service", "name") == name
            ),
            {},
        )
        if not embedded and ("instance_id" in raw_service or "id" in raw_service):
            embedded = [raw_service]
        if not isinstance(embedded, list):
            fail("INPUT_SHAPE_INVALID", "Service instances must be a list", service=name)
        for raw in embedded:
            if not isinstance(raw, dict):
                fail("INPUT_SHAPE_INVALID", "Instance must be an object", service=name)
            instance_rows.append({**raw, "_declared_service": name})
    for raw in top_instances:
        if not isinstance(raw, dict):
            fail("INPUT_SHAPE_INVALID", "Instance must be an object")
        instance_rows.append(dict(raw))

    instances: dict[str, Instance] = {}
    for raw in instance_rows:
        instance_id = _name(raw, "instance_id", "id")
        if instance_id in instances:
            fail("TOPOLOGY_DUPLICATE_INSTANCE", "Duplicate instance ID", instance_id=instance_id)
        explicit_service = raw.get(
            "service",
            raw.get("service_name", raw.get("_declared_service")),
        )
        if explicit_service is None:
            fail(
                "TOPOLOGY_INSTANCE_UNKNOWN_SERVICE",
                "Instance does not declare a service",
                instance_id=instance_id,
            )
        service = _required_text(explicit_service, "service")
        if service not in known_services:
            fail(
                "TOPOLOGY_INSTANCE_UNKNOWN_SERVICE",
                "Instance references an undeclared service",
                instance_id=instance_id,
                service=service,
            )
        declared = raw.get("_declared_service")
        if declared is not None and declared != service:
            fail(
                "TOPOLOGY_INSTANCE_UNKNOWN_SERVICE",
                "Instance service conflicts with its embedding service",
                instance_id=instance_id,
                service=service,
                declared_service=declared,
            )
        if raw.get("weight") is None:
            weight = 1.0
        elif _is_number(raw.get("weight")):
            weight = float(raw["weight"])
        else:
            fail("TOPOLOGY_WEIGHT_INVALID", "Instance weight must be numeric", instance_id=instance_id)
        if not math.isfinite(weight) or weight <= 0.0:
            fail("TOPOLOGY_WEIGHT_INVALID", "Instance weight must be finite and positive", instance_id=instance_id)
        fault_domain = raw.get(
            "fault_domain",
            raw.get("failure_domain", raw.get("domain", "default")),
        )
        fault_domain = _required_text(fault_domain, "fault_domain")
        instances[instance_id] = Instance(instance_id, service, fault_domain, weight)

    services: dict[str, Service] = {}
    for name in sorted(service_entries):
        entry = service_entries[name]
        service_instances = tuple(
            sorted(
                (inst for inst in instances.values() if inst.service == name),
                key=lambda inst: inst.instance_id,
            )
        )
        services[name] = Service(name, tuple(entry["dependencies"]), service_instances)

    upstream = {name: svc.upstream_dependencies for name, svc in services.items()}
    cycle = _detect_cycle(upstream)
    if cycle:
        fail("TOPOLOGY_CYCLE", "Dependency graph contains a cycle", cycle_path=cycle)

    canonical = {
        "topology_version": version,
        "services": [
            {
                "name": service.name,
                "upstream_dependencies": list(service.upstream_dependencies),
                "instances": [
                    {
                        "instance_id": inst.instance_id,
                        "fault_domain": inst.fault_domain,
                        "weight": inst.weight,
                    }
                    for inst in service.instances
                ],
            }
            for service in (services[name] for name in sorted(services))
        ],
    }
    topology_digest = digest(canonical)
    return Topology(version, services, instances, canonical, topology_digest)


def parse_events(payload: Any, topology: Topology) -> list[Event]:
    raw_events = payload.get("events", payload.get("event_stream")) if isinstance(payload, dict) else payload
    if not isinstance(raw_events, list):
        fail("INPUT_SHAPE_INVALID", "Event stream must be a list or an object with events")
    events: list[Event] = []
    previous = -1
    for index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            fail("EVENT_SHAPE_INVALID", "Each event must be an object", index=index)
        event_seq = raw.get("event_seq", raw.get("event_id", raw.get("seq")))
        if not _is_int(event_seq) or event_seq <= 0:
            fail("EVENT_SHAPE_INVALID", "event_seq must be a positive integer", index=index)
        if event_seq <= previous:
            fail(
                "EVENT_SEQUENCE_NOT_STRICT",
                "Event sequence numbers must be strictly increasing",
                previous_event_seq=previous,
                event_seq=event_seq,
            )
        previous = event_seq
        instance_id = _name(raw, "instance_id", "instance")
        if instance_id not in topology.instances:
            fail(
                "EVENT_UNKNOWN_INSTANCE",
                "Event references an undeclared instance",
                event_seq=event_seq,
                instance_id=instance_id,
            )
        service = topology.instances[instance_id].service
        declared_service = raw.get("service")
        if declared_service is not None and declared_service != service:
            fail(
                "EVENT_SHAPE_INVALID",
                "Event service does not match its instance",
                event_seq=event_seq,
                instance_id=instance_id,
                service=declared_service,
                expected_service=service,
            )
        rule = _name(raw, "rule", "alert_rule", "rule_name")
        severity_raw = raw.get("severity")
        if not isinstance(severity_raw, str) or severity_raw.lower() not in SEVERITIES:
            fail(
                "EVENT_INVALID_SEVERITY",
                "Severity must be one of info, warning, critical",
                event_seq=event_seq,
                severity=severity_raw,
            )
        severity = severity_raw.lower()
        logical_ts = raw.get("logical_ts", raw.get("logical_clock_ms", raw.get("ts")))
        if not _is_int(logical_ts):
            fail("EVENT_SHAPE_INVALID", "logical_ts must be an integer", event_seq=event_seq)
        if logical_ts < 0:
            fail("EVENT_NEGATIVE_TS", "logical_ts must be non-negative", event_seq=event_seq, logical_ts=logical_ts)
        metric_value = raw.get("metric_value", raw.get("value"))
        if not _is_number(metric_value):
            fail("EVENT_METRIC_NOT_FINITE", "metric_value must be numeric", event_seq=event_seq)
        metric_value = float(metric_value)
        if not math.isfinite(metric_value):
            fail("EVENT_METRIC_NOT_FINITE", "metric_value must be finite", event_seq=event_seq)
        silenced = raw.get("silenced", raw.get("in_silence_window", False))
        maintenance = raw.get("maintenance", raw.get("in_maintenance_window", False))
        if not isinstance(silenced, bool):
            fail("EVENT_INVALID_FLAG", "silenced must be boolean", event_seq=event_seq)
        if not isinstance(maintenance, bool):
            fail("EVENT_INVALID_FLAG", "maintenance must be boolean", event_seq=event_seq)
        events.append(
            Event(
                event_seq=event_seq,
                instance_id=instance_id,
                service=service,
                rule=rule,
                severity=severity,
                logical_ts=logical_ts,
                metric_value=metric_value,
                silenced=silenced,
                maintenance=maintenance,
            )
        )
    return events


def parse_window(raw: Any, topology: Topology, *, context: str = "PATCH_INVALID_SILENCE_WINDOW") -> Window:
    if not isinstance(raw, dict):
        fail(context, "Window must be an object")
    window_id = _name(raw, "window_id", "id")
    kind = raw.get("kind", "silence")
    if not isinstance(kind, str) or kind.lower() not in WINDOW_KINDS:
        fail(context, "Window kind must be silence or maintenance", window_id=window_id)
    kind = kind.lower()
    service = _name(raw, "service")
    if service not in topology.services:
        fail("PATCH_UNKNOWN_SERVICE", "Window references an undeclared service", service=service, window_id=window_id)
    instance_id = raw.get("instance_id")
    if instance_id is not None:
        instance_id = _required_text(instance_id, "instance_id")
        if instance_id not in topology.instances:
            fail(
                "PATCH_UNKNOWN_INSTANCE",
                "Window references an undeclared instance",
                instance_id=instance_id,
                window_id=window_id,
            )
        if topology.instances[instance_id].service != service:
            fail(
                "PATCH_INSTANCE_SERVICE_MISMATCH",
                "Window instance does not belong to the window service",
                instance_id=instance_id,
                service=service,
                window_id=window_id,
            )
    rule = raw.get("rule", raw.get("alert_rule"))
    if rule is not None:
        rule = _required_text(rule, "rule")
    start_ts = raw.get("start_ts", raw.get("start"))
    end_ts = raw.get("end_ts", raw.get("end"))
    if not _is_int(start_ts) or not _is_int(end_ts):
        fail(
            context,
            "Window start_ts/end_ts must be integers",
            window_id=window_id,
            start_ts=start_ts,
            end_ts=end_ts,
        )
    if start_ts < 0 or end_ts <= start_ts:
        fail(
            context,
            "Window interval must satisfy 0 <= start_ts < end_ts",
            window_id=window_id,
            start_ts=start_ts,
            end_ts=end_ts,
        )
    return Window(window_id, kind, service, instance_id, rule, start_ts, end_ts)


def parse_initial_windows(payload: Any, topology: Topology) -> list[Window]:
    if not isinstance(payload, dict):
        return []
    raw_windows = payload.get("silence_windows", payload.get("windows", []))
    if raw_windows is None:
        return []
    if not isinstance(raw_windows, list):
        fail("PATCH_INVALID_SILENCE_WINDOW", "silence_windows must be a list")
    windows: list[Window] = []
    seen: set[str] = set()
    for raw in raw_windows:
        window = parse_window(raw, topology)
        if window.window_id in seen:
            fail(
                "PATCH_INVALID_SILENCE_WINDOW",
                "Duplicate window ID",
                window_id=window.window_id,
            )
        seen.add(window.window_id)
        windows.append(window)
    return sorted(windows, key=lambda window: window.window_id)


def _normalize_changes_from_aliases(payload: dict[str, Any]) -> list[Any]:
    changes = payload.get("changes")
    if changes is not None:
        if not isinstance(changes, list):
            fail("PATCH_SHAPE_INVALID", "changes must be a list")
        return list(changes)
    changes = []
    dependencies = payload.get("dependencies", payload.get("dependency_changes", []))
    if isinstance(dependencies, dict):
        dependencies = [
            {"type": "dependency", "service": service, "upstream_dependencies": deps}
            for service, deps in sorted(dependencies.items())
        ]
    if dependencies:
        if not isinstance(dependencies, list):
            fail("PATCH_SHAPE_INVALID", "dependencies must be a list or object")
        changes.extend(dependencies)
    weights = payload.get("weights", payload.get("weight_changes", []))
    if isinstance(weights, dict):
        weights = [
            {"type": "weight", "instance_id": instance_id, "weight": weight}
            for instance_id, weight in sorted(weights.items())
        ]
    if weights:
        if not isinstance(weights, list):
            fail("PATCH_SHAPE_INVALID", "weights must be a list or object")
        changes.extend(weights)
    raw_windows = payload.get("silence_windows", payload.get("windows", []))
    if raw_windows:
        if not isinstance(raw_windows, list):
            fail("PATCH_SHAPE_INVALID", "silence_windows must be a list")
        changes.extend(
            {"type": "silence", "action": "add", "window": window}
            for window in raw_windows
        )
    removed_windows = payload.get("remove_silence_windows", [])
    if removed_windows:
        if not isinstance(removed_windows, list):
            fail("PATCH_SHAPE_INVALID", "remove_silence_windows must be a list")
        changes.extend(
            {"type": "silence", "action": "remove", "window_id": window_id}
            for window_id in removed_windows
        )
    return changes


def parse_patch(payload: Any, topology: Topology) -> dict[str, Any]:
    if not isinstance(payload, dict):
        fail("PATCH_SHAPE_INVALID", "Patch must be a JSON object")
    raw_patch_id = payload.get("patch_id", payload.get("id"))
    auto_patch_id = raw_patch_id is None
    patch_id = "auto" if auto_patch_id else _required_text(raw_patch_id, "patch_id")
    base_version = payload.get("base_topology_version", payload.get("base_version"))
    target_version = payload.get("topology_version", payload.get("target_version", payload.get("version")))
    if not _is_int(base_version):
        fail("PATCH_SHAPE_INVALID", "base_topology_version must be an integer", patch_id=patch_id)
    if not _is_int(target_version):
        fail("PATCH_SHAPE_INVALID", "topology_version must be an integer", patch_id=patch_id)
    raw_changes = _normalize_changes_from_aliases(payload)

    changes: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_changes):
        if not isinstance(raw, dict):
            fail("PATCH_SHAPE_INVALID", "Each change must be an object", patch_id=patch_id, index=index)
        change_type = raw.get("type", raw.get("kind"))
        if change_type not in {"dependency", "weight", "silence", "silence_window"}:
            fail(
                "PATCH_UNSUPPORTED_CHANGE",
                "Unsupported patch change type",
                patch_id=patch_id,
                index=index,
                change_type=change_type,
            )
        if change_type == "dependency":
            service = _name(raw, "service")
            if service not in topology.services:
                fail(
                    "PATCH_UNKNOWN_SERVICE",
                    "Dependency patch references an undeclared service",
                    patch_id=patch_id,
                    service=service,
                )
            action = raw.get("action", "set")
            if action not in {"set", "add", "remove"}:
                fail(
                    "PATCH_UNSUPPORTED_CHANGE",
                    "Dependency action must be set/add/remove",
                    patch_id=patch_id,
                    action=action,
                )
            dependencies = raw.get(
                "upstream_dependencies",
                raw.get("dependencies", raw.get("upstreams")),
            )
            if not isinstance(dependencies, list):
                fail(
                    "PATCH_SHAPE_INVALID",
                    "upstream_dependencies must be a list",
                    patch_id=patch_id,
                    service=service,
                )
            normalized_deps = sorted(
                {_required_text(dep, "upstream_dependency") for dep in dependencies}
            )
            changes.append(
                {
                    "type": "dependency",
                    "service": service,
                    "action": action,
                    "upstream_dependencies": normalized_deps,
                }
            )
        elif change_type == "weight":
            instance_id = _name(raw, "instance_id", "id")
            if instance_id not in topology.instances:
                fail(
                    "PATCH_UNKNOWN_INSTANCE",
                    "Weight patch references an undeclared instance",
                    patch_id=patch_id,
                    instance_id=instance_id,
                )
            weight = raw.get("weight", raw.get("value"))
            if not _is_number(weight):
                fail("TOPOLOGY_WEIGHT_INVALID", "Weight must be numeric", instance_id=instance_id)
            weight = float(weight)
            if not math.isfinite(weight) or weight <= 0.0:
                fail(
                    "TOPOLOGY_WEIGHT_INVALID",
                    "Weight must be finite and positive",
                    instance_id=instance_id,
                )
            changes.append(
                {
                    "type": "weight",
                    "instance_id": instance_id,
                    "weight": weight,
                }
            )
        else:
            action = raw.get("action", "add")
            if action not in {"add", "remove"}:
                fail(
                    "PATCH_UNSUPPORTED_CHANGE",
                    "Silence action must be add/remove",
                    patch_id=patch_id,
                    action=action,
                )
            if action == "remove":
                window_id = _name(raw, "window_id", "id")
                changes.append(
                    {
                        "type": "silence",
                        "action": "remove",
                        "window_id": window_id,
                    }
                )
            else:
                raw_window = raw.get("window", raw)
                window = parse_window(raw_window, topology, context="PATCH_INVALID_SILENCE_WINDOW")
                changes.append(
                    {
                        "type": "silence",
                        "action": "add",
                        "window": window.payload(),
                    }
                )

    canonical = {
        "patch_id": patch_id,
        "base_topology_version": base_version,
        "topology_version": target_version,
        "changes": changes,
    }
    if auto_patch_id:
        identity = {
            "base_topology_version": base_version,
            "topology_version": target_version,
            "changes": changes,
        }
        canonical["patch_id"] = "auto-" + digest(identity)[:20]
    canonical["digest"] = digest(canonical)
    return canonical


def apply_topology_changes(topology: Topology, changes: Iterable[dict[str, Any]], target_version: int) -> Topology:
    dependencies = {name: set(svc.upstream_dependencies) for name, svc in topology.services.items()}
    weights = {instance_id: inst.weight for instance_id, inst in topology.instances.items()}
    changed_services: set[str] = set()
    changed_instances: set[str] = set()
    seen_windows: set[str] = set()
    for change in changes:
        if change["type"] == "dependency":
            service = change["service"]
            action = change["action"]
            requested = set(change["upstream_dependencies"])
            if action == "set":
                dependencies[service] = requested
            elif action == "add":
                dependencies[service] |= requested
            else:
                dependencies[service] -= requested
            changed_services.add(service)
        elif change["type"] == "weight":
            instance_id = change["instance_id"]
            weights[instance_id] = change["weight"]
            changed_instances.add(instance_id)
        elif change["type"] == "silence":
            window_id = change["window_id"] if change["action"] == "remove" else change["window"]["window_id"]
            if window_id in seen_windows:
                fail(
                    "PATCH_INVALID_SILENCE_WINDOW",
                    "A patch may reference each window ID only once",
                    window_id=window_id,
                )
            seen_windows.add(window_id)

    services_payload = []
    for service in sorted(topology.services):
        svc = topology.services[service]
        services_payload.append(
            {
                "name": service,
                "upstream_dependencies": sorted(dependencies[service]),
                "instances": [
                    {
                        "instance_id": inst.instance_id,
                        "fault_domain": inst.fault_domain,
                        "weight": weights[inst.instance_id],
                    }
                    for inst in svc.instances
                ],
            }
        )
    new_topology = parse_topology({"topology_version": target_version, "services": services_payload})
    return new_topology


def events_from_rows(rows: Iterable[dict[str, Any]]) -> list[Event]:
    return [
        Event(
            event_seq=row["event_seq"],
            instance_id=row["instance_id"],
            service=row["service"],
            rule=row["rule"],
            severity=row["severity"],
            logical_ts=row["logical_ts"],
            metric_value=row["metric_value"],
            silenced=bool(row["silenced"]),
            maintenance=bool(row["maintenance"]),
        )
        for row in rows
    ]
