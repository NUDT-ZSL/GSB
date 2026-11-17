"""Deterministic propagation and policy-consistency engine."""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Iterable

from .model import (
    BUSINESS_CODE_ORDER,
    FieldRecord,
    STATUS_RANK,
    compute_sync_marker,
    normalize_status,
    normalize_version,
    sort_codes,
)
from .observations import build_observations
from .paths import PathError, canonical_path


@dataclass(frozen=True)
class EngineOptions:
    timeout_ms: int = 2000
    max_fields: int = 10000

    def __post_init__(self) -> None:
        if not isinstance(self.timeout_ms, int) or self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be a positive integer")
        if not isinstance(self.max_fields, int) or self.max_fields <= 0:
            raise ValueError("max_fields must be a positive integer")


class _DatasetTimeout(RuntimeError):
    pass


class _DatasetFieldLimit(RuntimeError):
    pass


class _Deadline:
    def __init__(self, timeout_ms: int) -> None:
        self.timeout_seconds = timeout_ms / 1000.0
        self.started = time.monotonic()

    def check(self) -> None:
        if time.monotonic() - self.started >= self.timeout_seconds:
            raise _DatasetTimeout

    def simulate_delay(self, delay_ms: int) -> None:
        if delay_ms <= 0:
            return
        finish = time.monotonic() + delay_ms / 1000.0
        while True:
            self.check()
            remaining = finish - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.01))


def _record_reason(detail: str, **extra: Any) -> dict[str, Any]:
    return {"code": "E_RECORD", "detail": detail, **extra}


def _source_marker(raw: dict[str, Any]) -> tuple[Any, bool]:
    for key in ("transitive", "propagate", "transitive_marker"):
        if key in raw:
            return raw[key], True
    return None, False


def _parse_records(
    records: list[Any], deadline: _Deadline
) -> tuple[list[FieldRecord], list[dict[str, Any]]]:
    preliminary: dict[int, dict[str, Any]] = {}
    path_usage: dict[str, list[int]] = {}
    path_record_index: dict[str, int] = {}

    for record_index, raw in enumerate(records, start=1):
        deadline.check()
        item: dict[str, Any] = {
            "record_index": record_index,
            "path": None,
            "raw_path": raw.get("path") if isinstance(raw, dict) else None,
            "raw": raw,
            "sources": (),
            "reasons": [],
        }
        preliminary[record_index] = item

        if not isinstance(raw, dict):
            item["reasons"].append(_record_reason("record_not_object"))
            continue

        try:
            path = canonical_path(raw.get("path"))
        except PathError as exc:
            item["reasons"].append(
                _record_reason("invalid_path", message=str(exc))
            )
            path = None
        if path is not None:
            item["path"] = path
            path_usage.setdefault(path, []).append(record_index)
            path_record_index.setdefault(path, record_index)

        raw_sources = raw.get("sources")
        sources: list[str] = []
        if not isinstance(raw_sources, list):
            item["reasons"].append(_record_reason("sources_not_list"))
        else:
            for source_index, raw_source in enumerate(raw_sources, start=1):
                deadline.check()
                try:
                    sources.append(canonical_path(raw_source))
                except PathError as exc:
                    item["reasons"].append(
                        _record_reason(
                            "invalid_source_path",
                            source_index=source_index,
                            message=str(exc),
                        )
                    )
            item["sources"] = tuple(sources)

        status_value = raw.get("status", raw.get("masking", raw.get("sensitivity")))
        status = normalize_status(status_value)
        if status is None:
            item["reasons"].append(
                _record_reason("invalid_status", value=status_value)
            )

        version_value = raw.get("version")
        version = normalize_version(version_value)
        if version is None:
            item["reasons"].append(
                _record_reason("invalid_version", value=version_value)
            )

        raw_transitive, has_transitive = _source_marker(raw)
        transitive = False
        if version == "v1":
            if has_transitive and not isinstance(raw_transitive, bool):
                item["reasons"].append(
                    _record_reason("transitive_not_boolean", value=raw_transitive)
                )
            elif raw_transitive is True:
                item["reasons"].append(
                    _record_reason("v1_transitive_not_allowed")
                )
        elif version == "v2":
            if not has_transitive or not isinstance(raw_transitive, bool):
                item["reasons"].append(
                    _record_reason("v2_requires_boolean_transitive")
                )
            else:
                transitive = raw_transitive

        policy_id = raw.get("policy_id")
        if not isinstance(policy_id, str) or not policy_id.strip():
            item["reasons"].append(_record_reason("invalid_policy_id"))
        else:
            policy_id = policy_id.strip()

        if (
            path is not None
            and status is not None
            and version is not None
            and isinstance(policy_id, str)
        ):
            item["field"] = FieldRecord(
                record_index=record_index,
                path=path,
                sources=tuple(sources),
                status=status,
                policy_id=policy_id,
                version=version,
                transitive=transitive,
                raw=raw,
            )

    for path, indexes in path_usage.items():
        if len(indexes) <= 1:
            continue
        sorted_indexes = sorted(indexes)
        for record_index in sorted_indexes:
            preliminary[record_index]["reasons"].append(
                _record_reason(
                    "duplicate_path",
                    duplicate_count=len(sorted_indexes),
                    record_indexes=sorted_indexes,
                )
            )

    valid: dict[str, FieldRecord] = {}
    for record_index in sorted(preliminary):
        item = preliminary[record_index]
        if item["reasons"] or "field" not in item:
            continue
        valid[item["field"].path] = item["field"]

    while True:
        missing_by_path: dict[str, list[str]] = {}
        for path, field in valid.items():
            deadline.check()
            missing = sorted({source for source in field.sources if source not in valid})
            if missing:
                missing_by_path[path] = missing
        if not missing_by_path:
            break
        for path in sorted(missing_by_path):
            del valid[path]
            preliminary[path_record_index[path]]["reasons"].append(
                _record_reason(
                    "source_target_missing",
                    missing_sources=missing_by_path[path],
                )
            )

    rejected: list[dict[str, Any]] = []
    for record_index in sorted(preliminary):
        item = preliminary[record_index]
        if not item["reasons"]:
            continue
        reasons = sorted(
            item["reasons"],
            key=lambda reason: (
                str(reason.get("detail", "")),
                str(reason.get("source_index", "")),
            ),
        )
        rejected.append(
            {
                "record_index": record_index,
                "path": item["path"],
                "raw_path": item["raw_path"],
                "reasons": reasons,
            }
        )

    return [valid[path] for path in sorted(valid)], rejected


def _build_graph(
    fields: dict[str, FieldRecord], deadline: _Deadline
) -> tuple[
    dict[str, tuple[str, ...]],
    list[dict[str, Any]],
]:
    edge_counts: Counter[tuple[str, str]] = Counter()
    dependents: dict[str, set[str]] = {path: set() for path in fields}

    for target in sorted(fields):
        deadline.check()
        for source in fields[target].sources:
            deadline.check()
            edge_counts[(target, source)] += 1
            dependents[source].add(target)

    duplicate_edges = [
        {
            "target": target,
            "source": source,
            "occurrences": occurrences,
            "duplicates": occurrences - 1,
        }
        for (target, source), occurrences in sorted(edge_counts.items())
        if occurrences > 1
    ]
    sorted_dependents = {
        path: tuple(sorted(values)) for path, values in dependents.items()
    }
    return sorted_dependents, duplicate_edges


def _find_cycles(
    dependents: dict[str, tuple[str, ...]], deadline: _Deadline
) -> list[dict[str, Any]]:
    nodes = sorted(dependents)
    cycles: list[list[str]] = []
    for start in nodes:
        path = [start]
        in_path = {start}
        stack: list[tuple[str, Iterable[str]]] = [
            (start, iter(dependents.get(start, ())))
        ]
        while stack:
            deadline.check()
            current, iterator = stack[-1]
            try:
                neighbor = next(iterator)
            except StopIteration:
                stack.pop()
                in_path.remove(current)
                path.pop()
                continue
            if neighbor == start:
                cycles.append(path + [start])
            elif neighbor > start and neighbor not in in_path:
                path.append(neighbor)
                in_path.add(neighbor)
                stack.append((neighbor, iter(dependents.get(neighbor, ()))))

    result = [
        {"path": cycle, "length": len(cycle) - 1}
        for cycle in cycles
    ]
    result.sort(key=lambda item: (item["length"], item["path"]))
    return result


def _propagate(
    fields: dict[str, FieldRecord],
    dependents: dict[str, tuple[str, ...]],
    deadline: _Deadline,
) -> dict[str, dict[str, str]]:
    incoming: dict[str, dict[str, str]] = {path: {} for path in fields}
    for origin_path in sorted(fields):
        origin = fields[origin_path]
        extends = origin.version == "v2" and origin.transitive
        queue: deque[str] = deque(dependents.get(origin_path, ()))
        visited = {origin_path}
        while queue:
            deadline.check()
            target_path = queue.popleft()
            if target_path in visited or target_path not in fields:
                continue
            visited.add(target_path)
            incoming[target_path].setdefault(origin_path, origin.status)
            if extends:
                queue.extend(dependents.get(target_path, ()))
    return incoming


def _field_reports(
    fields: dict[str, FieldRecord],
    incoming: dict[str, dict[str, str]],
    deadline: _Deadline,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in sorted(fields):
        deadline.check()
        field = fields[path]
        origins = incoming.get(path, {})
        propagated_status: str | None = None
        if origins:
            propagated_status = max(
                origins.values(), key=lambda status: STATUS_RANK[status]
            )
        candidates = [field.status, *origins.values()]
        effective_status = max(candidates, key=lambda status: STATUS_RANK[status])
        truth_sources = {path} if STATUS_RANK[field.status] == STATUS_RANK[effective_status] else set()
        truth_sources.update(
            origin
            for origin, status in origins.items()
            if STATUS_RANK[status] == STATUS_RANK[effective_status]
        )
        reports.append(
            {
                "path": path,
                "policy_id": field.policy_id,
                "version": field.version,
                "transitive": field.transitive,
                "sources": sorted(field.sources),
                "declared_status": field.status,
                "propagated_status": propagated_status,
                "effective_status": effective_status,
                "truth_sources": sorted(truth_sources),
                "incoming": [
                    {"path": origin, "status": status}
                    for origin, status in sorted(origins.items())
                ],
            }
        )
    return reports


def _violations(
    fields: dict[str, FieldRecord],
    incoming: dict[str, dict[str, str]],
    deadline: _Deadline,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, str]] = {}
    for target_path in sorted(incoming):
        deadline.check()
        target = fields[target_path]
        for origin_path, origin_status in incoming[target_path].items():
            deadline.check()
            code: str | None = None
            if origin_status == "invisible":
                code = "E_LEAK"
            elif STATUS_RANK[origin_status] > STATUS_RANK[target.status]:
                code = "E_DOWNGRADE"
            if code is not None:
                grouped.setdefault((code, target_path), {})[
                    origin_path
                ] = origin_status

    violations: list[dict[str, Any]] = []
    for (code, field_path), sources in grouped.items():
        deadline.check()
        source_items = [
            {"path": source, "status": status}
            for source, status in sorted(sources.items())
        ]
        if code == "E_LEAK":
            message = "invisible source reached the target"
        else:
            message = "source protection level exceeds the target declaration"
        violations.append(
            {
                "code": code,
                "field": field_path,
                "sources": source_items,
                "message": message,
            }
        )

    violations.sort(
        key=lambda item: (
            list(BUSINESS_CODE_ORDER).index(item["code"]),
            item["field"],
        )
    )
    return violations


def _version_conflicts(
    fields: dict[str, FieldRecord],
    deadline: _Deadline,
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for target_path in sorted(fields):
        deadline.check()
        direct_sources = sorted(set(fields[target_path].sources))
        source_records = [fields[source] for source in direct_sources]
        versions = sorted({record.version for record in source_records})
        if len(versions) <= 1:
            continue
        conflicts.append(
            {
                "code": "E_VERSION_CONFLICT",
                "field": target_path,
                "sources": [
                    {"path": record.path, "version": record.version}
                    for record in source_records
                ],
                "source_keys": [record.path for record in source_records],
                "versions": versions,
                "message": "direct sources use mixed declaration versions",
            }
        )
    conflicts.sort(key=lambda item: item["field"])
    return conflicts


def _base_report(
    dataset: dict[str, Any],
    *,
    field_count: int,
    options: EngineOptions,
) -> dict[str, Any]:
    dataset_id = dataset.get("dataset_id", dataset.get("id", "dataset"))
    if not isinstance(dataset_id, str) or not dataset_id:
        dataset_id = str(dataset_id)
    records = dataset.get("records")
    if not isinstance(records, list):
        records = []
    injection = dataset.get("injection", {})
    if not isinstance(injection, dict):
        injection = {}

    supplied_marker = dataset.get("sync_marker")
    computed_marker: str | None = None
    marker_error: str | None = None
    try:
        computed_marker = compute_sync_marker(records, injection)
    except Exception as exc:  # pragma: no cover - JSON inputs are serializable
        marker_error = f"{type(exc).__name__}: {exc}"
    marker_ok = (
        not isinstance(supplied_marker, str)
        or (computed_marker is not None and supplied_marker == computed_marker)
    )
    if marker_error is not None:
        marker_ok = False

    return {
        "dataset_id": dataset_id,
        "status": "ok",
        "sync_marker": supplied_marker if isinstance(supplied_marker, str) else None,
        "computed_sync_marker": computed_marker,
        "sync_marker_ok": marker_ok,
        "field_count": field_count,
        "processed_field_count": 0,
        "limits": {"timeout_ms": options.timeout_ms, "max_fields": options.max_fields},
        "codes": [],
        "rejected_records": [],
        "duplicate_edges": [],
        "cycles": [],
        "violations": [],
        "fields": [],
        "injection": injection,
        "observations": {
            "checked": 0,
            "consistent": 0,
            "inconsistent": 0,
            "checks": [],
        },
    }


def analyze_dataset(
    dataset: dict[str, Any],
    options: EngineOptions | None = None,
) -> dict[str, Any]:
    """Analyze one description set without mutating it.

    All graph and propagation decisions are computed from sorted canonical
    keys, so record arrival order cannot affect the result.
    """

    if options is None:
        options = EngineOptions()
    if not isinstance(dataset, dict):
        dataset = {}

    deadline = _Deadline(options.timeout_ms)
    raw_records = dataset.get("records")
    field_count = len(raw_records) if isinstance(raw_records, list) else 0
    report = _base_report(dataset, field_count=field_count, options=options)
    codes: set[str] = set()
    if not report["sync_marker_ok"]:
        codes.add("E_SYNC")

    try:
        if not isinstance(raw_records, list):
            codes.add("E_RECORD")
            report["schema_errors"] = ["records_must_be_a_list"]
            raw_records = []
        if field_count > options.max_fields:
            raise _DatasetFieldLimit

        injection = report["injection"]
        delay_ms = injection.get("simulate_delay_ms", 0)
        if isinstance(delay_ms, int) and not isinstance(delay_ms, bool):
            deadline.simulate_delay(delay_ms)
        deadline.check()

        fields_list, rejected = _parse_records(raw_records, deadline)
        fields = {field.path: field for field in fields_list}
        report["rejected_records"] = rejected
        if rejected:
            codes.add("E_RECORD")
        report["processed_field_count"] = len(fields)

        dependents, duplicate_edges = _build_graph(fields, deadline)
        report["duplicate_edges"] = duplicate_edges

        cycles = _find_cycles(dependents, deadline)
        report["cycles"] = cycles
        if cycles:
            codes.add("E_CYCLE")

        incoming = _propagate(fields, dependents, deadline)
        report["fields"] = _field_reports(fields, incoming, deadline)

        violations = _version_conflicts(fields, deadline)
        violations.extend(_violations(fields, incoming, deadline))
        violations.sort(
            key=lambda item: (
                list(BUSINESS_CODE_ORDER).index(item["code"]),
                item["field"],
            )
        )
        report["violations"] = violations
        codes.update(item["code"] for item in violations)

    except _DatasetFieldLimit:
        codes.add("E_FIELDS")
    except _DatasetTimeout:
        codes.add("E_TIMEOUT")
    except Exception as exc:
        codes.add("E_INTERNAL")
        report["internal_error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }

    report["codes"] = sort_codes(codes)
    report["status"] = (
        "ok"
        if not report["codes"] and report["sync_marker_ok"]
        else "failed"
    )
    report["observations"] = build_observations(
        dataset,
        report,
        field_count=field_count,
        max_fields=options.max_fields,
        timeout_ms=options.timeout_ms,
    )
    return report

