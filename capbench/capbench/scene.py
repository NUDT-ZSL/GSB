"""Scene JSON loading with strong validation.

Any structural or semantic violation rejects the WHOLE file with E_SCENE and
reports every offending field path (e.g. "stages[1].concurrency").
"""
from __future__ import annotations

import ipaddress
import json
import os
from typing import Any

from .errors import SceneError
from .models import Scene, SloThresholds, StageSpec, TargetSpec

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def load_scene(path: str) -> Scene:
    if not os.path.isfile(path):
        raise SceneError(f"scene file not found: {path}", field_paths=["<file>"])
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SceneError(
            "scene file is not valid JSON",
            field_paths=["<file>"],
            details={"parse_error": str(exc)},
        ) from exc
    if not isinstance(data, dict):
        raise SceneError("scene root must be a JSON object", field_paths=["$"])
    return _parse_scene(data)


def _parse_scene(data: dict[str, Any]) -> Scene:
    errors: list[str] = []

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("name")

    target = _parse_target(data.get("target"), errors)
    slo = _parse_slo(data.get("slo"), errors)
    sample_interval_ms = _parse_positive_int(
        data, "sample_interval_ms", errors, minimum=10)
    stages = _parse_stages(data.get("stages"), errors)

    if errors:
        raise SceneError(
            "scene validation failed; load rejected as a whole",
            field_paths=errors,
        )
    return Scene(
        name=name.strip(),
        target=target,
        stages=tuple(stages),
        slo=slo,
        sample_interval_ms=sample_interval_ms,
        raw=data,
    )


def _parse_target(raw: Any, errors: list[str]) -> TargetSpec:
    if not isinstance(raw, dict):
        errors.append("target")
        return TargetSpec(host="127.0.0.1", port=0)
    host = raw.get("host")
    port = raw.get("port")
    if not isinstance(host, str) or host not in LOOPBACK_HOSTS:
        errors.append("target.host")
        host = host if isinstance(host, str) else ""
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        errors.append("target.port")
        port = port if isinstance(port, int) else 0
    else:
        # reject non-loopback numeric IP explicitly
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_loopback:
                errors.append("target.host")
        except ValueError:
            if host not in LOOPBACK_HOSTS:
                errors.append("target.host")
    return TargetSpec(host=host, port=port)


def _parse_slo(raw: Any, errors: list[str]) -> SloThresholds:
    if not isinstance(raw, dict):
        errors.append("slo")
        return SloThresholds(min_success_rate_bp=0, max_p95_latency_ms=1)
    min_sr = raw.get("min_success_rate_bp")
    if not isinstance(min_sr, int) or isinstance(min_sr, bool) \
            or not (0 <= min_sr <= 10000):
        errors.append("slo.min_success_rate_bp")
        min_sr = min_sr if isinstance(min_sr, int) else -1
    max_p95 = raw.get("max_p95_latency_ms")
    if not isinstance(max_p95, int) or isinstance(max_p95, bool) or max_p95 <= 0:
        errors.append("slo.max_p95_latency_ms")
        max_p95 = max_p95 if isinstance(max_p95, int) else 0
    return SloThresholds(min_success_rate_bp=min_sr, max_p95_latency_ms=max_p95)


def _parse_positive_int(data: dict[str, Any], key: str,
                        errors: list[str], minimum: int = 1) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        errors.append(key)
        return 0
    return value


def _parse_stages(raw: Any, errors: list[str]) -> list[StageSpec]:
    if not isinstance(raw, list) or not raw:
        errors.append("stages")
        return []
    stages: list[StageSpec] = []
    seen_indexes: set[int] = set()
    for pos, item in enumerate(raw):
        prefix = f"stages[{pos}]"
        if not isinstance(item, dict):
            errors.append(prefix)
            continue
        index = item.get("index")
        duration = item.get("duration_s")
        concurrency = item.get("concurrency")
        limit = item.get("throughput_limit_rps")
        local_bad = False
        if not isinstance(index, int) or isinstance(index, bool) or index <= 0:
            errors.append(f"{prefix}.index")
            local_bad = True
        elif index in seen_indexes:
            errors.append(f"{prefix}.index(duplicate)")
            local_bad = True
        else:
            seen_indexes.add(index)
        if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
            errors.append(f"{prefix}.duration_s")
            local_bad = True
        if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency <= 0:
            errors.append(f"{prefix}.concurrency")
            local_bad = True
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            errors.append(f"{prefix}.throughput_limit_rps")
            local_bad = True
        if not local_bad:
            stages.append(StageSpec(index=index, duration_s=duration,
                                    concurrency=concurrency, throughput_limit_rps=limit))
    # consecutive uniqueness check (1..N in order)
    valid_indexes = sorted(seen_indexes)
    if valid_indexes and valid_indexes != list(range(1, len(valid_indexes) + 1)):
        errors.append("stages[].index(not_consecutive_from_1)")
    if stages and [s.index for s in stages] != sorted(seen_indexes):
        # declared order must follow index order
        errors.append("stages[].order(not_ascending)")
    return stages
