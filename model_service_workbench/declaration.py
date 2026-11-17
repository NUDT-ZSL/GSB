"""Load and strongly validate local cluster declarations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import DeclarationError
from .models import ClusterDeclaration, ServiceSpec, sha256_text


def _fail(message: str, path: str, **details: Any) -> None:
    raise DeclarationError(message, path, details)


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_number(value: Any) -> bool:
    return type(value) in (int, float)


def _require_positive_int(value: Any, path: str) -> int:
    if not _is_int(value) or value <= 0:
        _fail("must be a positive integer", path, actual=value)
    return int(value)


def _require_nonnegative_int(value: Any, path: str) -> int:
    if not _is_int(value) or value < 0:
        _fail("must be a non-negative integer", path, actual=value)
    return int(value)


def _parse_json(raw_text: str) -> Mapping[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value}")

    try:
        parsed = json.loads(raw_text, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise DeclarationError(
            "declaration is not valid UTF-8 JSON",
            "$",
            {"reason": str(exc)},
        ) from exc
    if not isinstance(parsed, Mapping):
        _fail("declaration root must be an object", "$")
    return parsed


def _validate_service(
    item: Any,
    index: int,
    seen_names: set[str],
) -> ServiceSpec:
    base = f"$.services[{index}]"
    if not isinstance(item, Mapping):
        _fail("service entry must be an object", base)

    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        _fail("must be a non-empty string", f"{base}.name", actual=name)
    if name != name.strip():
        _fail("must not have surrounding whitespace", f"{base}.name", actual=name)
    if name in seen_names:
        _fail("service name must be unique", f"{base}.name", actual=name)
    seen_names.add(name)

    requests_value = item.get("requests")
    if not isinstance(requests_value, list):
        _fail("must be a non-empty array of minute request counts", f"{base}.requests")
    if not requests_value:
        _fail("must contain at least one minute", f"{base}.requests")
    requests: list[int] = []
    for minute, value in enumerate(requests_value):
        if not _is_int(value) or value < 0:
            _fail(
                "minute request count must be a non-negative integer",
                f"{base}.requests[{minute}]",
                actual=value,
            )
        requests.append(int(value))

    concurrency = _require_positive_int(
        item.get("concurrency_per_replica"),
        f"{base}.concurrency_per_replica",
    )
    latency = _require_positive_int(
        item.get("request_latency_ms"),
        f"{base}.request_latency_ms",
    )
    quota = _require_positive_int(
        item.get("concurrency_quota"),
        f"{base}.concurrency_quota",
    )
    min_replicas = _require_nonnegative_int(
        item.get("min_replicas"),
        f"{base}.min_replicas",
    )
    max_replicas = _require_positive_int(
        item.get("max_replicas"),
        f"{base}.max_replicas",
    )
    if min_replicas > max_replicas:
        _fail(
            "min_replicas must not exceed max_replicas",
            f"{base}.min_replicas",
            min_replicas=min_replicas,
            max_replicas=max_replicas,
        )

    threshold = item.get("scale_threshold")
    if not _is_number(threshold) or not (0 < float(threshold) <= 1):
        _fail(
            "scale_threshold must be a number in (0, 1]",
            f"{base}.scale_threshold",
            actual=threshold,
        )

    return ServiceSpec(
        name=name,
        requests=tuple(requests),
        concurrency_per_replica=concurrency,
        request_latency_ms=latency,
        scale_threshold=float(threshold),
        min_replicas=min_replicas,
        max_replicas=max_replicas,
        concurrency_quota=quota,
    )


def _build_declaration(
    document: Mapping[str, Any],
    raw_text: str,
    source_path: str,
) -> ClusterDeclaration:
    schema_version = _require_positive_int(
        document.get("schema_version"),
        "$.schema_version",
    )
    cluster_name = document.get("cluster_name")
    if not isinstance(cluster_name, str) or not cluster_name.strip():
        _fail("must be a non-empty string", "$.cluster_name", actual=cluster_name)

    authored_version = _require_positive_int(
        document.get("declaration_version"),
        "$.declaration_version",
    )
    global_budget = _require_positive_int(
        document.get("global_replica_budget"),
        "$.global_replica_budget",
    )

    services_raw = document.get("services")
    if not isinstance(services_raw, list) or not services_raw:
        _fail("must be a non-empty array", "$.services")

    seen_names: set[str] = set()
    services = [
        _validate_service(item, index, seen_names)
        for index, item in enumerate(services_raw)
    ]
    services.sort(key=lambda service: service.name)

    return ClusterDeclaration(
        cluster_name=cluster_name,
        authored_version=authored_version,
        declaration_version=authored_version,
        global_replica_budget=global_budget,
        services=tuple(services),
        raw_text=raw_text,
        fingerprint=sha256_text(raw_text),
        source_path=source_path,
        document=document,
        schema_version=schema_version,
    )


def load_declaration_text(
    raw_text: str,
    source_path: str = "<memory>",
) -> ClusterDeclaration:
    document = _parse_json(raw_text)
    return _build_declaration(document, raw_text, source_path)


def load_declaration(path: str | Path) -> ClusterDeclaration:
    source = Path(path).resolve()
    try:
        raw_bytes = source.read_bytes()
    except OSError as exc:
        raise DeclarationError(
            "cannot read declaration file",
            "$",
            {"source_path": str(source), "reason": str(exc)},
        ) from exc
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeclarationError(
            "declaration must be UTF-8 encoded",
            "$",
            {"source_path": str(source), "reason": str(exc)},
        ) from exc
    return load_declaration_text(raw_text, str(source))


def _service_path(name: str) -> str:
    return f"$.services[{json.dumps(name, ensure_ascii=False)}]"


def _add_diff(
    differences: list[dict[str, Any]],
    path: str,
    before: Any,
    after: Any,
) -> None:
    if before != after:
        differences.append({"path": path, "before": before, "after": after})


def _compare_requests(
    service_name: str,
    before: tuple[int, ...],
    after: tuple[int, ...],
    differences: list[dict[str, Any]],
) -> set[int]:
    changed_minutes: set[int] = set()
    base = f"{_service_path(service_name)}.requests"
    horizon = max(len(before), len(after))
    for minute in range(horizon):
        old = before[minute] if minute < len(before) else None
        new = after[minute] if minute < len(after) else None
        if old != new:
            changed_minutes.add(minute)
            _add_diff(differences, f"{base}[{minute}]", old, new)
    return changed_minutes


def diff_declarations(
    previous: ClusterDeclaration | None,
    current: ClusterDeclaration,
) -> dict[str, Any]:
    """Return a deterministic semantic diff and invalidation scope."""

    differences: list[dict[str, Any]] = []
    affected_services: set[str] = set()
    minute_indices: set[int] = set()
    all_services_affected = previous is None
    all_minutes_affected = previous is None

    if previous is not None:
        _add_diff(
            differences,
            "$.schema_version",
            previous.schema_version,
            current.schema_version,
        )
        _add_diff(
            differences,
            "$.cluster_name",
            previous.cluster_name,
            current.cluster_name,
        )
        _add_diff(
            differences,
            "$.declaration_version",
            previous.authored_version,
            current.authored_version,
        )
        if previous.global_replica_budget != current.global_replica_budget:
            _add_diff(
                differences,
                "$.global_replica_budget",
                previous.global_replica_budget,
                current.global_replica_budget,
            )
            all_services_affected = True
            all_minutes_affected = True

        old_map = previous.service_map
        new_map = current.service_map
        old_names = set(old_map)
        new_names = set(new_map)
        if old_names != new_names:
            all_services_affected = True
            all_minutes_affected = True
            for name in sorted(old_names - new_names):
                differences.append(
                    {
                        "path": _service_path(name),
                        "before": "present",
                        "after": None,
                    }
                )
            for name in sorted(new_names - old_names):
                differences.append(
                    {
                        "path": _service_path(name),
                        "before": None,
                        "after": "present",
                    }
                )

        for name in sorted(old_names & new_names):
            old = old_map[name]
            new = new_map[name]
            base = _service_path(name)
            changed_fields = False
            for field_name in (
                "concurrency_per_replica",
                "request_latency_ms",
                "scale_threshold",
                "min_replicas",
                "max_replicas",
                "concurrency_quota",
            ):
                before = getattr(old, field_name)
                after = getattr(new, field_name)
                if before != after:
                    changed_fields = True
                    _add_diff(
                        differences,
                        f"{base}.{field_name}",
                        before,
                        after,
                    )
            changed_minutes = _compare_requests(
                name,
                old.requests,
                new.requests,
                differences,
            )
            minute_indices.update(changed_minutes)
            if changed_fields or changed_minutes:
                affected_services.add(name)
            if changed_fields:
                minute_indices.update(range(len(new.requests)))

    differences.sort(key=lambda entry: str(entry["path"]))

    if all_services_affected:
        affected_services = set(current.service_names)
    if all_minutes_affected:
        minute_indices = set(range(current.total_minutes))

    affected_range: tuple[int, int] | None = None
    if minute_indices:
        affected_range = (min(minute_indices), max(minute_indices))

    return {
        "differences": tuple(differences),
        "affected_services": tuple(sorted(affected_services)),
        "affected_minute_range": affected_range,
    }


def service_path(name: str) -> str:
    """Public helper used by tests and diagnostics."""

    return _service_path(name)


def collect_load_sequences(
    declaration: ClusterDeclaration,
) -> Iterable[tuple[str, tuple[int, ...]]]:
    for service in declaration.services:
        yield service.name, service.requests
