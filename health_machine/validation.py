"""Deterministic validation for registry, observations and endpoint values."""
from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import (
    DuplicateInstanceId,
    DuplicateLevel,
    DuplicateServiceName,
    InvalidCount,
    InvalidGeneration,
    InvalidLatency,
    InvalidLogicalClock,
    InvalidLoopbackEndpoint,
    InvalidLevelSet,
    ObservationSequenceError,
    UnknownObservationInstance,
    UnknownObservationLevel,
)
from .util import is_finite_number, is_real_int, read_json


def validate_loopback_endpoint(endpoint: Any, instance_id: str | None = None) -> str:
    if not isinstance(endpoint, str) or not endpoint:
        raise InvalidLoopbackEndpoint(
            "endpoint must be a non-empty host:port or http(s) loopback URL",
            location={"instance_id": instance_id, "path": "endpoint"},
        )
    candidate = endpoint if "://" in endpoint else "//" + endpoint
    try:
        parsed = urlsplit(candidate)
    except ValueError as exc:
        raise InvalidLoopbackEndpoint(
            f"invalid endpoint {endpoint!r}: {exc}",
            location={"instance_id": instance_id, "path": "endpoint"},
        ) from exc
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        raise InvalidLoopbackEndpoint(
            f"unsupported endpoint scheme {parsed.scheme!r}",
            location={"instance_id": instance_id, "path": "endpoint"},
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise InvalidLoopbackEndpoint(
            "loopback endpoint must not contain a path, query or fragment",
            location={"instance_id": instance_id, "path": "endpoint"},
        )
    hostname = parsed.hostname
    port = parsed.port
    if not hostname or port is None:
        raise InvalidLoopbackEndpoint(
            f"endpoint {endpoint!r} must include host and port",
            location={"instance_id": instance_id, "path": "endpoint"},
        )
    if not (1 <= port <= 65535):
        raise InvalidLoopbackEndpoint(
            f"endpoint port {port} is outside 1..65535",
            location={"instance_id": instance_id, "path": "endpoint"},
        )
    if hostname != "localhost":
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise InvalidLoopbackEndpoint(
                f"endpoint host {hostname!r} is not a loopback IP literal",
                location={"instance_id": instance_id, "path": "endpoint"},
            ) from exc
        if not address.is_loopback:
            raise InvalidLoopbackEndpoint(
                f"endpoint host {hostname!r} is not loopback",
                location={"instance_id": instance_id, "path": "endpoint"},
            )
    return endpoint


def _positive_int_field(
    instance_id: str,
    data: dict[str, Any],
    field: str,
    *,
    default: int | None = None,
) -> int | None:
    if field not in data or data[field] is None:
        return default
    value = data[field]
    if not is_real_int(value) or value <= 0:
        raise InvalidGeneration(
            f"{field} must be a positive integer",
            location={"instance_id": instance_id, "path": field},
        )
    return value


def validate_registry(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), list):
        raise InvalidLevelSet("registry must contain a services list", location={"path": "services"})

    normalized: dict[str, Any] = {"services": {}, "instances": {}}
    seen_service_names: set[str] = set()
    seen_service_ids: set[str] = set()
    seen_instance_ids: set[str] = set()

    for service_index, service in enumerate(doc["services"]):
        path = f"services[{service_index}]"
        if not isinstance(service, dict):
            raise InvalidLevelSet("service entry must be an object", location={"path": path})
        service_id = service.get("service_id") or service.get("id")
        service_name = service.get("service_name") or service.get("name")
        if not isinstance(service_id, str) or not service_id:
            raise DuplicateServiceName("service_id must be a non-empty string", location={"path": f"{path}.service_id"})
        if service_id in seen_service_ids:
            raise DuplicateServiceName(
                f"duplicate service_id {service_id!r}",
                location={"path": f"{path}.service_id"},
                details={"service_id": service_id},
            )
        seen_service_ids.add(service_id)
        if not isinstance(service_name, str) or not service_name:
            raise DuplicateServiceName("service_name must be a non-empty string", location={"path": f"{path}.service_name"})
        if service_name in seen_service_names:
            raise DuplicateServiceName(
                f"duplicate service name {service_name!r}",
                location={"path": f"{path}.service_name", "instance_id": None},
                details={"service_name": service_name},
            )
        seen_service_names.add(service_name)
        normalized["services"][service_id] = {
            "service_id": service_id,
            "service_name": service_name,
        }

        instances = service.get("instances", [])
        if not isinstance(instances, list):
            raise InvalidLevelSet("instances must be a list", location={"path": f"{path}.instances"})
        for instance_index, raw_instance in enumerate(instances):
            ipath = f"{path}.instances[{instance_index}]"
            if not isinstance(raw_instance, dict):
                raise InvalidLevelSet("instance entry must be an object", location={"path": ipath})
            instance_id = raw_instance.get("instance_id") or raw_instance.get("id")
            if not isinstance(instance_id, str) or not instance_id:
                raise DuplicateInstanceId("instance_id must be a non-empty string", location={"path": f"{ipath}.instance_id"})
            if instance_id in seen_instance_ids:
                raise DuplicateInstanceId(
                    f"duplicate instance_id {instance_id!r}",
                    location={"instance_id": instance_id, "path": f"{ipath}.instance_id"},
                )
            seen_instance_ids.add(instance_id)

            endpoint = validate_loopback_endpoint(raw_instance.get("endpoint"), instance_id)
            generation = raw_instance.get("state_generation", raw_instance.get("generation", 1))
            if not is_real_int(generation) or generation < 0:
                raise InvalidGeneration(
                    "state_generation must be a non-negative integer",
                    location={"instance_id": instance_id, "path": f"{ipath}.state_generation"},
                )

            raw_levels = raw_instance.get("levels")
            if not isinstance(raw_levels, list) or not raw_levels:
                raise InvalidLevelSet(
                    "levels must be a non-empty list",
                    location={"instance_id": instance_id, "path": f"{ipath}.levels"},
                )
            levels: list[int] = []
            seen_levels: set[int] = set()
            for level_index, level in enumerate(raw_levels):
                if not is_real_int(level) or level <= 0:
                    raise InvalidLevelSet(
                        "probe level must be a positive integer",
                        location={"instance_id": instance_id, "level": level if is_real_int(level) else None,
                                  "path": f"{ipath}.levels[{level_index}]"},
                    )
                if level in seen_levels:
                    raise DuplicateLevel(
                        f"duplicate probe level {level}",
                        location={"instance_id": instance_id, "level": level,
                                  "path": f"{ipath}.levels[{level_index}]"},
                    )
                if levels and level <= levels[-1]:
                    raise DuplicateLevel(
                        "probe levels must be strictly increasing",
                        location={"instance_id": instance_id, "level": level,
                                  "path": f"{ipath}.levels[{level_index}]"},
                    )
                seen_levels.add(level)
                levels.append(level)

            window_ms = _positive_int_field(instance_id, raw_instance, "window_ms", default=1000)
            timeout_ms = _positive_int_field(instance_id, raw_instance, "timeout_ms", default=max(2500, window_ms * 2))
            normalized["instances"][instance_id] = {
                "instance_id": instance_id,
                "service_id": service_id,
                "service_name": service_name,
                "endpoint": endpoint,
                "state_generation": generation,
                "levels": levels,
                "window_ms": window_ms,
                "timeout_ms": timeout_ms,
            }
    if not normalized["instances"]:
        raise InvalidLevelSet("registry must declare at least one instance", location={"path": "services"})
    return normalized


def load_registry(path: str | Path) -> dict[str, Any]:
    return validate_registry(read_json(path))


def validate_observations(doc: Any, registry: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(doc, list):
        raw_observations = doc
    elif isinstance(doc, dict) and isinstance(doc.get("observations"), list):
        raw_observations = doc["observations"]
    else:
        raise ObservationSequenceError("observations must be a list or contain an observations list")

    normalized: list[dict[str, Any]] = []
    sequences: dict[tuple[str, int], list[int]] = {}
    clocks: dict[tuple[str, int], list[int]] = {}
    stream_sequences: dict[str, list[int]] = {}

    for index, raw in enumerate(raw_observations):
        path = f"observations[{index}]"
        if not isinstance(raw, dict):
            raise ObservationSequenceError("observation must be an object", location={"path": path})
        instance_id = raw.get("instance_id")
        instance = registry["instances"].get(instance_id) if isinstance(instance_id, str) else None
        if instance is None:
            raise UnknownObservationInstance(
                f"unknown instance_id {instance_id!r}",
                location={"instance_id": instance_id if isinstance(instance_id, str) else None, "path": f"{path}.instance_id"},
            )
        level = raw.get("level")
        if not is_real_int(level) or level not in instance["levels"]:
            raise UnknownObservationLevel(
                f"level {level!r} is not declared for instance {instance_id}",
                location={"instance_id": instance_id, "level": level if is_real_int(level) else None,
                          "path": f"{path}.level"},
            )

        seq = raw.get("seq", raw.get("observation_seq"))
        if not is_real_int(seq) or seq <= 0:
            raise ObservationSequenceError(
                "seq must be a positive integer",
                location={"instance_id": instance_id, "level": level, "path": f"{path}.seq"},
            )
        key = (instance_id, level)
        previous_seq = sequences.get(key, [0])[-1]
        if seq <= previous_seq:
            raise ObservationSequenceError(
                f"seq {seq} is not strictly greater than {previous_seq}",
                location={"instance_id": instance_id, "level": level, "path": f"{path}.seq"},
                details={"previous_seq": previous_seq, "seq": seq},
            )
        sequences.setdefault(key, []).append(seq)

        clock_ms = raw.get("clock_ms", raw.get("logical_clock_ms"))
        if not is_real_int(clock_ms) or clock_ms < 0:
            raise InvalidLogicalClock(
                "clock_ms must be a non-negative integer",
                location={"instance_id": instance_id, "level": level, "path": f"{path}.clock_ms"},
            )
        previous_clock = clocks.get(key, [-1])[-1]
        if previous_clock >= 0 and clock_ms < previous_clock:
            raise InvalidLogicalClock(
                f"clock_ms {clock_ms} precedes previous clock {previous_clock}",
                location={"instance_id": instance_id, "level": level, "path": f"{path}.clock_ms"},
            )
        clocks.setdefault(key, []).append(clock_ms)

        successes = raw.get("successes", 0)
        failures = raw.get("failures", 0)
        for field, value in (("successes", successes), ("failures", failures)):
            if not is_real_int(value) or value < 0:
                raise InvalidCount(
                    f"{field} must be a non-negative integer",
                    location={"instance_id": instance_id, "level": level, "path": f"{path}.{field}"},
                )

        latency = raw.get("avg_latency_ms", raw.get("latency_ms", 0))
        if not is_finite_number(latency):
            raise InvalidLatency(
                "avg_latency_ms must be finite",
                location={"instance_id": instance_id, "level": level, "path": f"{path}.avg_latency_ms"},
            )
        from .util import decimal_number

        latency_decimal = decimal_number(latency)
        if latency_decimal < 0:
            raise InvalidLatency(
                "avg_latency_ms must be non-negative",
                location={"instance_id": instance_id, "level": level, "path": f"{path}.avg_latency_ms"},
            )

        stream_seq = raw.get("stream_seq", index + 1)
        if not is_real_int(stream_seq) or stream_seq <= 0:
            raise ObservationSequenceError(
                "stream_seq must be a positive integer",
                location={"instance_id": instance_id, "level": level, "path": f"{path}.stream_seq"},
            )
        previous_stream_seq = stream_sequences.get(instance_id, [0])[-1]
        if stream_seq <= previous_stream_seq:
            raise ObservationSequenceError(
                f"stream_seq {stream_seq} is not strictly greater than {previous_stream_seq}",
                location={"instance_id": instance_id, "level": level, "path": f"{path}.stream_seq"},
                details={"previous_stream_seq": previous_stream_seq, "stream_seq": stream_seq},
            )
        stream_sequences.setdefault(instance_id, []).append(stream_seq)

        normalized.append(
            {
                "stream_seq": stream_seq,
                "instance_id": instance_id,
                "level": level,
                "seq": seq,
                "clock_ms": clock_ms,
                "successes": successes,
                "failures": failures,
                "avg_latency_ms": latency_decimal,
            }
        )
    return normalized


def load_observations(path: str | Path, registry: dict[str, Any]) -> list[dict[str, Any]]:
    return validate_observations(read_json(path), registry)
