"""本地 JSON / NDJSON 加载与强校验。

任一约束不满足即抛出 :class:`ObsDiagError`（业务码 ``E_INPUT``），
fault 中携带首个出错记录的物理行号、字段路径、期望约束与实际值，
整个加载过程“整体拒绝”，不会返回部分结果。
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Iterator

from .errors import E_INPUT, ObsDiagError, ValidationFault
from .models import (
    Observation,
    Rule,
    RuleSet,
    SUPPORTED_METRICS,
    SUPPORTED_OPERATORS,
    SUPPORTED_SEVERITIES,
    Topology,
)


def _fault(line: int, path: str, expected: str, actual: Any) -> ObsDiagError:
    return ObsDiagError(
        E_INPUT,
        "input validation failed",
        ValidationFault(
            code=E_INPUT,
            line_number=line,
            field_path=path,
            expected=expected,
            actual=repr(actual),
        ),
    )


def _read_text(path: str) -> str:
    if not os.path.isfile(path):
        raise _fault(0, "<file>", "existing readable file", path)
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)


def _finite_number(value: Any) -> bool:
    return _is_number(value) and math.isfinite(float(value))


def _line_of_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _array_element_lines(text: str, array_key: str) -> list[int]:
    """定位顶层对象中某数组每个对象元素的起始行（括号配对扫描）。"""
    key_pos = text.find(json.dumps(array_key))
    if key_pos < 0:
        return []
    bracket = text.find("[", key_pos)
    if bracket < 0:
        return []
    lines: list[int] = []
    depth = 0
    in_string = False
    escaped = False
    elem_start: int | None = None
    for index in range(bracket, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            if depth == 1 and char == "{":
                elem_start = index
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 1 and char == "}" and elem_start is not None:
                lines.append(_line_of_offset(text, elem_start))
                elem_start = None
            if depth == 0:
                break
        elif char == "," and depth == 1:
            elem_start = None
    return lines


def _load_json_document(path: str) -> tuple[dict, str]:
    text = _read_text(path)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _fault(exc.lineno, "<json>", "valid JSON document", exc.msg) from exc
    if not isinstance(document, dict):
        raise _fault(1, "<root>", "JSON object", type(document).__name__)
    return document, text


def load_topology(path: str) -> Topology:
    document, text = _load_json_document(path)

    version = document.get("version")
    if not _is_int(version) or version < 1:
        raise _fault(1, "version", "positive integer", version)

    services = document.get("services")
    if not isinstance(services, list) or not services:
        raise _fault(1, "services", "non-empty array", services)
    seen: set[str] = set()
    for index, service in enumerate(services):
        if not isinstance(service, str) or not service.strip():
            raise _fault(1, f"services[{index}]", "non-empty string", service)
        if service in seen:
            line = _line_of_containing(text, json.dumps(service))
            raise _fault(line, f"services[{index}]", "unique service name", service)
        seen.add(service)

    edges = document.get("edges")
    if not isinstance(edges, list):
        raise _fault(1, "edges", "array", edges)
    edge_lines = _array_element_lines(text, "edges")
    edge_pairs: set[tuple[str, str]] = set()
    for index, edge in enumerate(edges):
        line = edge_lines[index] if index < len(edge_lines) else 1
        if not isinstance(edge, dict):
            raise _fault(line, f"edges[{index}]", "object", edge)
        for field_name in ("source", "target", "weight"):
            if field_name not in edge:
                raise _fault(line, f"edges[{index}].{field_name}", "present field", "<missing>")
        source, target, weight = edge["source"], edge["target"], edge["weight"]
        if not isinstance(source, str) or not source.strip():
            raise _fault(line, f"edges[{index}].source", "non-empty string", source)
        if not isinstance(target, str) or not target.strip():
            raise _fault(line, f"edges[{index}].target", "non-empty string", target)
        if source not in seen:
            raise _fault(line, f"edges[{index}].source", "service declared in services", source)
        if target not in seen:
            raise _fault(line, f"edges[{index}].target", "service declared in services", target)
        if not _is_int(weight) or weight <= 0:
            raise _fault(line, f"edges[{index}].weight", "positive integer", weight)
        if (source, target) in edge_pairs:
            raise _fault(line, f"edges[{index}]", "unique (source,target) edge", f"{source}->{target}")
        edge_pairs.add((source, target))

    return Topology.build(version, services, [(e["source"], e["target"], e["weight"]) for e in edges])


def _line_of_containing(text: str, token: str) -> int:
    position = text.find(token)
    return _line_of_offset(text, position) if position >= 0 else 1


def load_rules(path: str, topology: Topology) -> RuleSet:
    document, text = _load_json_document(path)

    version = document.get("version")
    if not _is_int(version) or version < 1:
        raise _fault(1, "version", "positive integer", version)

    rules = document.get("rules")
    if not isinstance(rules, list) or not rules:
        raise _fault(1, "rules", "non-empty array", rules)
    rule_lines = _array_element_lines(text, "rules")
    rule_ids: set[str] = set()
    parsed: list[Rule] = []
    for index, raw in enumerate(rules):
        line = rule_lines[index] if index < len(rule_lines) else 1
        prefix = f"rules[{index}]"
        if not isinstance(raw, dict):
            raise _fault(line, prefix, "object", raw)
        for field_name in ("rule_id", "service", "metric", "operator", "threshold", "severity"):
            if field_name not in raw:
                raise _fault(line, f"{prefix}.{field_name}", "present field", "<missing>")
        rule_id = raw["rule_id"]
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise _fault(line, f"{prefix}.rule_id", "non-empty string", rule_id)
        if rule_id in rule_ids:
            raise _fault(line, f"{prefix}.rule_id", "unique rule id", rule_id)
        service = raw["service"]
        if not isinstance(service, str) or service not in topology.services:
            raise _fault(line, f"{prefix}.service", "service declared in topology", service)
        metric = raw["metric"]
        if metric not in SUPPORTED_METRICS:
            raise _fault(
                line,
                f"{prefix}.metric",
                f"one of {sorted(SUPPORTED_METRICS)}",
                metric,
            )
        operator = raw["operator"]
        if operator not in SUPPORTED_OPERATORS:
            raise _fault(
                line,
                f"{prefix}.operator",
                f"one of {sorted(SUPPORTED_OPERATORS)}",
                operator,
            )
        threshold = raw["threshold"]
        if not _finite_number(threshold):
            raise _fault(line, f"{prefix}.threshold", "finite number", threshold)
        severity = raw["severity"]
        if severity not in SUPPORTED_SEVERITIES:
            raise _fault(
                line,
                f"{prefix}.severity",
                f"one of {sorted(SUPPORTED_SEVERITIES)}",
                severity,
            )
        rule_ids.add(rule_id)
        parsed.append(
            Rule(
                rule_id=rule_id,
                service=service,
                metric=metric,
                operator=operator,
                threshold=float(threshold),
                severity=severity,
            )
        )
    return RuleSet.build(version, parsed)


def iter_observations(path: str, topology: Topology, ruleset: RuleSet) -> Iterator[Observation]:
    text = _read_text(path)
    if not text.strip():
        raise _fault(1, "<file>", "non-empty NDJSON document", "<empty>")
    ruled_metrics = {rule.metric for rule in ruleset.rules}
    for physical_line, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise _fault(physical_line, "<json>", "valid JSON object", exc.msg) from exc
        if not isinstance(record, dict):
            raise _fault(physical_line, "<root>", "JSON object", type(record).__name__)
        prefix = ""
        for field_name in ("timestamp_ms", "service", "metric", "value"):
            if field_name not in record:
                raise _fault(physical_line, f"{prefix}{field_name}", "present field", "<missing>")
        timestamp = record["timestamp_ms"]
        if not _is_int(timestamp) or timestamp < 0:
            raise _fault(physical_line, "timestamp_ms", "non-negative integer milliseconds", timestamp)
        service = record["service"]
        if not isinstance(service, str) or service not in topology.services:
            raise _fault(physical_line, "service", "service declared in topology", service)
        metric = record["metric"]
        if metric not in SUPPORTED_METRICS:
            raise _fault(
                physical_line,
                "metric",
                f"one of {sorted(SUPPORTED_METRICS)}",
                metric,
            )
        value = record["value"]
        if not _finite_number(value):
            raise _fault(physical_line, "value", "finite number", value)
        if metric not in ruled_metrics:
            # 受支持但没有任何规则引用：允许录入，仅不会产生事件
            pass
        yield Observation(
            timestamp_ms=int(timestamp),
            service=service,
            metric=metric,
            value=float(value),
            line_number=physical_line,
        )


def load_observations(path: str, topology: Topology, ruleset: RuleSet) -> list[Observation]:
    return list(iter_observations(path, topology, ruleset))
