"""Strict local JSON/NDJSON loaders.

All validation failures reject the complete input set.  Semantic JSON errors
carry a stable file identifier and one-based source line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .errors import DomainError, spec_error
from .models import (
    SEVERITIES,
    SUPPORTED_OPERATORS,
    AlarmEvent,
    Edge,
    Rule,
    Topology,
)
from .util import display_file, is_finite_number, is_int, locate_line, read_text


def _reject_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant {token}")


def _parse_json_text(text: str, path: Path) -> Any:
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise spec_error(
            f"invalid JSON: {exc.msg}",
            file=display_file(path),
            line=exc.lineno,
            detail={"column": exc.colno},
        ) from exc
    except ValueError as exc:
        raise spec_error(
            f"invalid JSON: {exc}",
            file=display_file(path),
            line=1,
        ) from exc


def _read_json_document(path: str | Path) -> tuple[Path, str, Any]:
    source = Path(path)
    try:
        text = read_text(source)
    except OSError as exc:
        raise spec_error(
            f"cannot read input: {exc}",
            file=display_file(source),
            line=1,
        ) from exc
    return source, text, _parse_json_text(text, source)


def _require_object(value: Any, *, path: Path, line: int, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise spec_error(f"{what} must be a JSON object", file=display_file(path), line=line)
    return value


def _require_string(
    obj: dict[str, Any],
    key: str,
    *,
    path: Path,
    line: int,
    what: str,
) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise spec_error(
            f"{what}.{key} must be a non-empty string",
            file=display_file(path),
            line=line,
        )
    return value


def _line_for_item(text: str, item: Any, fields: Iterable[str]) -> int:
    if isinstance(item, dict):
        for field in fields:
            candidate = item.get(field)
            if isinstance(candidate, str) and candidate:
                line = locate_line(text, json.dumps(candidate), default=0)
                if line:
                    return line
    return 1


def _condition(value: Any, *, path: Path, line: int, rule_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise spec_error(
            f"rule {rule_id} condition must be an object",
            file=display_file(path),
            line=line,
        )
    metric = value.get("metric")
    operator = value.get("operator")
    threshold = value.get("threshold")
    if not isinstance(metric, str) or not metric.strip():
        raise spec_error(
            f"rule {rule_id} condition.metric must be a non-empty string",
            file=display_file(path),
            line=line,
        )
    if operator not in SUPPORTED_OPERATORS:
        raise spec_error(
            f"rule {rule_id} condition.operator is unsupported",
            file=display_file(path),
            line=line,
            detail={"supported": list(SUPPORTED_OPERATORS)},
        )
    if not is_finite_number(threshold):
        raise spec_error(
            f"rule {rule_id} condition.threshold must be finite",
            file=display_file(path),
            line=line,
        )
    return {"metric": metric, "operator": operator, "threshold": threshold}


def load_rules(path: str | Path) -> tuple[Rule, ...]:
    source, text, document = _read_json_document(path)
    if isinstance(document, dict):
        raw_rules = document.get("rules")
    else:
        raw_rules = document
    if not isinstance(raw_rules, list):
        raise spec_error(
            "rules document must be an array or an object with a rules array",
            file=display_file(source),
            line=1,
        )
    result: list[Rule] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_rules):
        base_line = _line_for_item(text, item, ("rule_id", "service"))
        obj = _require_object(item, path=source, line=base_line, what=f"rules[{index}]")
        rule_id = _require_string(
            obj, "rule_id", path=source, line=base_line, what=f"rules[{index}]"
        )
        line = _line_for_item(text, obj, ("rule_id",))
        if rule_id in seen_ids:
            raise spec_error(
                f"duplicate rule_id {rule_id}",
                file=display_file(source),
                line=line,
                detail={"rule_id": rule_id},
            )
        service = _require_string(
            obj, "service", path=source, line=line, what=f"rule {rule_id}"
        )
        severity = obj.get("severity")
        if severity not in SEVERITIES:
            raise spec_error(
                f"rule {rule_id} severity is unsupported",
                file=display_file(source),
                line=line,
                detail={"severity": severity, "supported": list(SEVERITIES)},
            )
        condition_value = obj.get("condition", obj.get("metric_condition"))
        condition = _condition(condition_value, path=source, line=line, rule_id=rule_id)
        seen_ids.add(rule_id)
        result.append(
            Rule(
                rule_id=rule_id,
                service=service,
                severity=severity,
                condition=condition,
            )
        )
    if not result:
        raise spec_error("rules document contains no rules", file=display_file(source), line=1)
    return tuple(sorted(result, key=lambda rule: rule.rule_id))


def load_topology(path: str | Path) -> Topology:
    source, text, document = _read_json_document(path)
    obj = _require_object(document, path=source, line=1, what="topology")
    raw_services = obj.get("services")
    raw_edges = obj.get("edges")
    if raw_services is None and isinstance(raw_edges, list):
        raw_services = []
        for edge in raw_edges:
            if isinstance(edge, dict):
                for key in ("upstream", "downstream"):
                    value = edge.get(key)
                    if isinstance(value, str) and value not in raw_services:
                        raw_services.append(value)
    if not isinstance(raw_services, list):
        raise spec_error(
            "topology.services must be an array when no edges are supplied",
            file=display_file(source),
            line=locate_line(text, '"services"'),
        )
    if not isinstance(raw_edges, list):
        raise spec_error(
            "topology.edges must be an array",
            file=display_file(source),
            line=locate_line(text, '"edges"'),
        )

    services: list[str] = []
    seen_services: set[str] = set()
    for index, service in enumerate(raw_services):
        if not isinstance(service, str) or not service.strip():
            raise spec_error(
                f"topology.services[{index}] must be a non-empty string",
                file=display_file(source),
                line=locate_line(text, '"services"'),
            )
        if service in seen_services:
            raise spec_error(
                f"duplicate topology service {service}",
                file=display_file(source),
                line=locate_line(text, json.dumps(service)),
                detail={"service": service},
            )
        seen_services.add(service)
        services.append(service)

    edges: list[Edge] = []
    seen_edges: set[tuple[str, str]] = set()
    for index, item in enumerate(raw_edges):
        line = _line_for_item(text, item, ("upstream", "downstream"))
        edge_obj = _require_object(item, path=source, line=line, what=f"edges[{index}]")
        upstream = _require_string(
            edge_obj, "upstream", path=source, line=line, what=f"edges[{index}]"
        )
        downstream = _require_string(
            edge_obj, "downstream", path=source, line=line, what=f"edges[{index}]"
        )
        if upstream == downstream:
            raise spec_error(
                "self dependency edges are not allowed",
                file=display_file(source),
                line=line,
                detail={"service": upstream},
            )
        if upstream not in seen_services or downstream not in seen_services:
            missing = sorted(
                service
                for service in (upstream, downstream)
                if service not in seen_services
            )
            raise spec_error(
                "dependency edge references an unknown service",
                file=display_file(source),
                line=line,
                detail={"missing_services": missing},
            )
        key = (upstream, downstream)
        if key in seen_edges:
            raise spec_error(
                f"duplicate dependency edge {upstream}->{downstream}",
                file=display_file(source),
                line=line,
                detail={"upstream": upstream, "downstream": downstream},
            )
        weight = edge_obj.get("weight", 1.0)
        if not is_finite_number(weight) or weight <= 0:
            raise spec_error(
                "dependency edge weight must be a positive finite number",
                file=display_file(source),
                line=line,
            )
        seen_edges.add(key)
        edges.append(Edge(upstream=upstream, downstream=downstream, weight=float(weight)))

    topology = Topology(
        services=tuple(sorted(services)),
        edges=tuple(sorted(edges, key=lambda edge: (edge.upstream, edge.downstream))),
    )
    if not topology.services:
        raise spec_error(
            "topology contains no services", file=display_file(source), line=1
        )
    return topology


def validate_consistency(rules: tuple[Rule, ...], topology: Topology) -> None:
    rule_services = {rule.service for rule in rules}
    topology_services = set(topology.services)
    if rule_services != topology_services:
        raise spec_error(
            "rule and topology service sets differ",
            file="rules.json+topology.json",
            line=1,
            detail={
                "rule_only_services": sorted(rule_services - topology_services),
                "topology_only_services": sorted(topology_services - rule_services),
            },
        )


def load_events(path: str | Path, rules: tuple[Rule, ...]) -> tuple[AlarmEvent, ...]:
    source = Path(path)
    rules_by_id = {rule.rule_id: rule for rule in rules}
    events: list[AlarmEvent] = []
    seen_ids: set[str] = set()
    previous_timestamp: int | None = None
    try:
        handle = source.open("r", encoding="utf-8")
    except OSError as exc:
        raise spec_error(
            f"cannot read event stream: {exc}",
            file=display_file(source),
            line=1,
        ) from exc

    with handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                item = json.loads(raw_line, parse_constant=_reject_constant)
            except json.JSONDecodeError as exc:
                raise spec_error(
                    f"invalid NDJSON event: {exc.msg}",
                    file=display_file(source),
                    line=line_number,
                    detail={"column": exc.colno},
                ) from exc
            except ValueError as exc:
                raise spec_error(
                    f"invalid NDJSON event: {exc}",
                    file=display_file(source),
                    line=line_number,
                ) from exc
            if not isinstance(item, dict):
                raise spec_error(
                    "event line must be a JSON object",
                    file=display_file(source),
                    line=line_number,
                )
            event_id = item.get("event_id")
            if not isinstance(event_id, str) or not event_id.strip():
                raise spec_error(
                    "event_id must be a non-empty string",
                    file=display_file(source),
                    line=line_number,
                )
            if event_id in seen_ids:
                raise spec_error(
                    f"duplicate event_id {event_id}",
                    file=display_file(source),
                    line=line_number,
                    detail={"event_id": event_id},
                )
            rule_id = item.get("rule_id")
            if not isinstance(rule_id, str) or not rule_id.strip():
                raise spec_error(
                    "rule_id must be a non-empty string",
                    file=display_file(source),
                    line=line_number,
                )
            rule = rules_by_id.get(rule_id)
            if rule is None:
                raise spec_error(
                    f"event references unknown rule_id {rule_id}",
                    file=display_file(source),
                    line=line_number,
                    detail={"event_id": event_id, "rule_id": rule_id},
                )
            service = item.get("service")
            if not isinstance(service, str) or not service.strip():
                raise spec_error(
                    "service must be a non-empty string",
                    file=display_file(source),
                    line=line_number,
                )
            if service != rule.service:
                raise spec_error(
                    f"event service {service} does not match rule target {rule.service}",
                    file=display_file(source),
                    line=line_number,
                    detail={
                        "event_id": event_id,
                        "rule_id": rule_id,
                        "expected_service": rule.service,
                    },
                )
            timestamp = item.get("timestamp")
            if not is_int(timestamp) or timestamp < 0:
                raise spec_error(
                    "timestamp must be a non-negative integer millisecond value",
                    file=display_file(source),
                    line=line_number,
                    detail={"event_id": event_id, "timestamp": timestamp},
                )
            if previous_timestamp is not None and timestamp < previous_timestamp:
                raise DomainError(
                    "E_TIME",
                    "event timestamps must be nondecreasing",
                    file=display_file(source),
                    line=line_number,
                    detail={
                        "event_id": event_id,
                        "previous_timestamp": previous_timestamp,
                        "timestamp": timestamp,
                    },
                )
            value = item.get("value")
            if not is_finite_number(value):
                raise spec_error(
                    "observed value must be a finite number",
                    file=display_file(source),
                    line=line_number,
                    detail={"event_id": event_id, "value": value},
                )
            seen_ids.add(event_id)
            previous_timestamp = timestamp
            events.append(
                AlarmEvent(
                    event_id=event_id,
                    rule_id=rule_id,
                    service=service,
                    timestamp=timestamp,
                    value=float(value),
                )
            )
    return tuple(events)


def load_inputs(
    rules_path: str | Path,
    topology_path: str | Path,
    events_path: str | Path,
) -> tuple[tuple[Rule, ...], Topology, tuple[AlarmEvent, ...]]:
    rules = load_rules(rules_path)
    topology = load_topology(topology_path)
    validate_consistency(rules, topology)
    events = load_events(events_path, rules)
    return rules, topology, events
