from __future__ import annotations

import ipaddress
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .canonical import (
    canonical_json,
    digest_matches,
    normalize_digest,
    semantic_digest,
    sha256_digest,
)
from .errors import EXIT_USAGE, BusinessError
from .models import InstanceSpec, Observation, ParsedShard, STATES


@dataclass
class ParsedRegistry:
    manifest: dict[str, Any]
    digest: str
    instances: dict[str, InstanceSpec]
    services: list[str]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(
    value: Any,
    code: str,
    message: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    if not _is_int(value):
        raise BusinessError(code, message, details or {}, EXIT_USAGE)
    number = int(value)
    if minimum is not None and number < minimum:
        raise BusinessError(code, message, details or {}, EXIT_USAGE)
    if maximum is not None and number > maximum:
        raise BusinessError(code, message, details or {}, EXIT_USAGE)
    return number


def _require_string(
    value: Any,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BusinessError(code, message, details or {}, EXIT_USAGE)
    return value.strip()


def _number(
    value: Any,
    code: str,
    message: str,
    details: dict[str, Any],
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BusinessError(code, message, details, EXIT_USAGE)
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise BusinessError(code, message, details, EXIT_USAGE)
    return int(number) if number.is_integer() else number


def read_json(path: str | os.PathLike[str]) -> Any:
    file_path = Path(path)
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise BusinessError(
            "E_FILE_READ",
            "无法读取输入文件",
            {"file": str(file_path), "error": str(exc)},
            EXIT_USAGE,
        ) from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BusinessError(
            "E_BAD_ENCODING",
            "输入文件必须为 UTF-8",
            {"file": str(file_path), "error": str(exc)},
            EXIT_USAGE,
        ) from exc
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise BusinessError(
            "E_BAD_JSON",
            "输入文件不是合法 JSON",
            {
                "file": str(file_path),
                "line": getattr(exc, "lineno", None),
                "column": getattr(exc, "colno", None),
                "error": str(exc),
            },
            EXIT_USAGE,
        ) from exc


def _validate_loopback_endpoint(value: Any, instance_id: str) -> str:
    endpoint = _require_string(
        value,
        "E_BAD_ENDPOINT",
        "观测端点必须是非空字符串",
        details={"instance_id": instance_id},
    )
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        raise BusinessError(
            "E_BAD_ENDPOINT",
            "观测端点必须使用 http 或 https",
            {"instance_id": instance_id, "endpoint": endpoint},
            EXIT_USAGE,
        )
    if parsed.username or parsed.password:
        raise BusinessError(
            "E_BAD_ENDPOINT",
            "观测端点不得包含认证信息",
            {"instance_id": instance_id, "endpoint": endpoint},
            EXIT_USAGE,
        )
    host = parsed.hostname
    if not host:
        raise BusinessError(
            "E_BAD_ENDPOINT",
            "观测端点缺少主机名",
            {"instance_id": instance_id, "endpoint": endpoint},
            EXIT_USAGE,
        )
    host_lower = host.lower().rstrip(".")
    is_loopback = host_lower == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host_lower).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise BusinessError(
            "E_BAD_ENDPOINT",
            "观测端点必须是回环地址",
            {"instance_id": instance_id, "endpoint": endpoint, "host": host},
            EXIT_USAGE,
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise BusinessError(
            "E_BAD_ENDPOINT",
            "观测端点端口非法",
            {"instance_id": instance_id, "endpoint": endpoint},
            EXIT_USAGE,
        ) from exc
    if port is None or not (1 <= port <= 65535):
        raise BusinessError(
            "E_BAD_ENDPOINT",
            "观测端点必须包含 1..65535 端口",
            {"instance_id": instance_id, "endpoint": endpoint},
            EXIT_USAGE,
        )
    return endpoint


def _validate_status_codes(
    value: Any,
    *,
    instance_id: str,
    code: str = "E_BAD_EXPECTED_STATUS",
) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise BusinessError(
            code,
            "期望状态码集合必须是非空数组",
            {"instance_id": instance_id},
            EXIT_USAGE,
        )
    codes: list[int] = []
    for item in value:
        if not _is_int(item) or not (100 <= int(item) <= 999):
            raise BusinessError(
                code,
                "状态码必须是三位整数",
                {"instance_id": instance_id, "status_code": item},
                EXIT_USAGE,
            )
        codes.append(int(item))
    if not codes:
        raise BusinessError(
            code,
            "期望状态码集合不得为空",
            {"instance_id": instance_id},
            EXIT_USAGE,
        )
    return tuple(sorted(set(codes)))


def _source_instances(document: Any) -> list[Any]:
    if isinstance(document, list):
        return document
    if not isinstance(document, dict):
        raise BusinessError(
            "E_BAD_MANIFEST",
            "注册清单必须是 JSON 对象或实例数组",
            {},
            EXIT_USAGE,
        )
    if isinstance(document.get("instances"), list):
        return list(document["instances"])
    services = document.get("services")
    if not isinstance(services, list):
        raise BusinessError(
            "E_BAD_MANIFEST",
            "注册清单缺少 services 或 instances 数组",
            {},
            EXIT_USAGE,
        )
    flattened: list[Any] = []
    for service in services:
        if not isinstance(service, dict):
            raise BusinessError(
                "E_BAD_SERVICE",
                "服务条目必须是 JSON 对象",
                {},
                EXIT_USAGE,
            )
        service_name = service.get("service_name", service.get("service"))
        if not isinstance(service.get("instances"), list):
            raise BusinessError(
                "E_BAD_SERVICE",
                "服务条目必须包含 instances 数组",
                {"service_name": service_name},
                EXIT_USAGE,
            )
        for item in service["instances"]:
            if not isinstance(item, dict):
                raise BusinessError(
                    "E_BAD_INSTANCE",
                    "实例条目必须是 JSON 对象",
                    {"service_name": service_name},
                    EXIT_USAGE,
                )
            merged = dict(item)
            merged.setdefault("service_name", service_name)
            flattened.append(merged)
    return flattened


def parse_registry(path: str | os.PathLike[str]) -> ParsedRegistry:
    document = read_json(path)
    defaults = document if isinstance(document, dict) else {}
    if isinstance(document, dict) and isinstance(document.get("services"), list):
        seen_services: set[str] = set()
        for service in document["services"]:
            if not isinstance(service, dict):
                continue
            name = service.get("service_name", service.get("service"))
            if not isinstance(name, str) or not name.strip():
                continue
            normalized_name = name.strip()
            if normalized_name in seen_services:
                raise BusinessError(
                    "E_DUPLICATE_SERVICE",
                    "服务名在清单中必须唯一",
                    {"service_name": normalized_name},
                    EXIT_USAGE,
                )
            seen_services.add(normalized_name)
    raw_instances = _source_instances(document)
    specs: dict[str, InstanceSpec] = {}
    service_names: list[str] = []

    for raw in raw_instances:
        if not isinstance(raw, dict):
            raise BusinessError(
                "E_BAD_INSTANCE",
                "实例条目必须是 JSON 对象",
                {},
                EXIT_USAGE,
            )
        instance_id = _require_string(
            raw.get("instance_id", raw.get("id")),
            "E_BAD_INSTANCE",
            "实例 ID 必须是非空字符串",
            details={"instance_id": raw.get("instance_id", raw.get("id"))},
        )
        if instance_id in specs:
            raise BusinessError(
                "E_DUPLICATE_INSTANCE",
                "实例 ID 在清单中必须唯一",
                {"instance_id": instance_id},
                EXIT_USAGE,
            )
        service_name = _require_string(
            raw.get("service_name", raw.get("service")),
            "E_BAD_SERVICE",
            "服务名必须是非空字符串",
            details={"instance_id": instance_id},
        )
        if service_name not in service_names:
            service_names.append(service_name)

        endpoint = _validate_loopback_endpoint(
            raw.get("endpoint", raw.get("observation_endpoint", raw.get("probe_endpoint"))),
            instance_id,
        )
        statuses = _validate_status_codes(
            raw.get(
                "expected_status_codes",
                raw.get("expected_statuses", raw.get("expected_codes")),
            ),
            instance_id=instance_id,
        )
        lease_generation = _require_int(
            raw.get("lease_generation", raw.get("lease_epoch", raw.get("lease_algebra"))),
            "E_BAD_LEASE",
            "租约代数必须是非负整数",
            minimum=0,
            details={"instance_id": instance_id},
        )
        state_generation = _require_int(
            raw.get("state_generation", raw.get("generation")),
            "E_BAD_STATE_GENERATION",
            "状态世代号必须是正整数",
            minimum=1,
            details={"instance_id": instance_id},
        )
        baseline = raw.get(
            "serialization_baseline",
            raw.get("baseline", defaults.get("serialization_baseline", "canon-v1")),
        )
        baseline = _require_string(
            baseline,
            "E_BAD_BASELINE",
            "观测序列化基线必须是非空字符串",
            details={"instance_id": instance_id},
        )
        initial_state = raw.get(
            "initial_state",
            raw.get("state", defaults.get("initial_state", "healthy")),
        )
        if initial_state not in STATES:
            raise BusinessError(
                "E_BAD_STATE",
                "初始状态必须是 healthy、suspect 或 lost",
                {"instance_id": instance_id, "state": initial_state},
                EXIT_USAGE,
            )

        suspect_after = _require_int(
            raw.get("suspect_after", defaults.get("suspect_after", 1)),
            "E_BAD_THRESHOLD",
            "suspect_after 必须是正整数",
            minimum=1,
            details={"instance_id": instance_id},
        )
        lost_after = _require_int(
            raw.get("lost_after", defaults.get("lost_after", 3)),
            "E_BAD_THRESHOLD",
            "lost_after 必须是正整数",
            minimum=1,
            details={"instance_id": instance_id},
        )
        if lost_after < suspect_after:
            raise BusinessError(
                "E_BAD_THRESHOLD",
                "lost_after 不得小于 suspect_after",
                {
                    "instance_id": instance_id,
                    "suspect_after": suspect_after,
                    "lost_after": lost_after,
                },
                EXIT_USAGE,
            )
        backoff_base_ms = _require_int(
            raw.get("backoff_base_ms", defaults.get("backoff_base_ms", 100)),
            "E_BAD_BACKOFF",
            "backoff_base_ms 必须是正整数",
            minimum=1,
            details={"instance_id": instance_id},
        )
        backoff_max_ms = _require_int(
            raw.get("backoff_max_ms", defaults.get("backoff_max_ms", 30_000)),
            "E_BAD_BACKOFF",
            "backoff_max_ms 必须是正整数",
            minimum=backoff_base_ms,
            details={"instance_id": instance_id},
        )
        lease_decay = _require_int(
            raw.get(
                "lease_decay_per_observation",
                defaults.get("lease_decay_per_observation", 1),
            ),
            "E_BAD_LEASE",
            "lease_decay_per_observation 必须是非负整数",
            minimum=0,
            details={"instance_id": instance_id},
        )
        lease_refresh = raw.get(
            "lease_refresh_on_success",
            defaults.get("lease_refresh_on_success", False),
        )
        if not isinstance(lease_refresh, bool):
            raise BusinessError(
                "E_BAD_LEASE",
                "lease_refresh_on_success 必须是布尔值",
                {"instance_id": instance_id},
                EXIT_USAGE,
            )
        clock_tolerance = _require_int(
            raw.get(
                "clock_rollback_tolerance_ms",
                defaults.get("clock_rollback_tolerance_ms", 5),
            ),
            "E_BAD_CLOCK_TOLERANCE",
            "clock_rollback_tolerance_ms 必须是非负整数",
            minimum=0,
            details={"instance_id": instance_id},
        )
        version_time_tolerance = _require_int(
            raw.get(
                "version_time_tolerance_ms",
                defaults.get("version_time_tolerance_ms", 5),
            ),
            "E_BAD_VERSION_TOLERANCE",
            "version_time_tolerance_ms 必须是非负整数",
            minimum=0,
            details={"instance_id": instance_id},
        )
        version_latency_tolerance = _number(
            raw.get(
                "version_latency_tolerance_ms",
                defaults.get("version_latency_tolerance_ms", 1),
            ),
            "E_BAD_VERSION_TOLERANCE",
            "version_latency_tolerance_ms 必须是非负数",
            {"instance_id": instance_id},
        )

        spec = InstanceSpec(
            instance_id=instance_id,
            service_name=service_name,
            endpoint=endpoint,
            expected_status_codes=statuses,
            lease_generation=lease_generation,
            state_generation=state_generation,
            serialization_baseline=baseline,
            initial_state=str(initial_state),
            suspect_after=suspect_after,
            lost_after=lost_after,
            backoff_base_ms=backoff_base_ms,
            backoff_max_ms=backoff_max_ms,
            lease_decay_per_observation=lease_decay,
            lease_refresh_on_success=lease_refresh,
            clock_rollback_tolerance_ms=clock_tolerance,
            version_time_tolerance_ms=version_time_tolerance,
            version_latency_tolerance_ms=version_latency_tolerance,
        )
        specs[instance_id] = spec

    if not specs:
        raise BusinessError(
            "E_EMPTY_MANIFEST",
            "注册清单不得为空",
            {},
            EXIT_USAGE,
        )
    normalized_manifest = {
        "schema_version": 1,
        "services": [
            {
                "service_name": name,
                "instances": [
                    spec.to_config()
                    for spec in specs.values()
                    if spec.service_name == name
                ],
            }
            for name in service_names
        ],
    }
    return ParsedRegistry(
        manifest=normalized_manifest,
        digest=sha256_digest(normalized_manifest),
        instances=specs,
        services=service_names,
    )


def parse_clock_file(path: str | os.PathLike[str]) -> dict[tuple[str, int], int]:
    document = read_json(path)
    entries: list[dict[str, Any]] = []
    if isinstance(document, list):
        entries = list(document)
    elif isinstance(document, dict):
        if isinstance(document.get("clocks"), list):
            entries = list(document["clocks"])
        elif isinstance(document.get("clock_ms"), list):
            # A bare logical-clock sequence is accepted only as documentation;
            # observations still need an instance/sequence mapping.
            entries = []
        else:
            for instance_id, value in document.items():
                if instance_id in {"schema_version", "description"}:
                    continue
                if isinstance(value, dict):
                    for seq, clock in value.items():
                        entries.append(
                            {
                                "instance_id": instance_id,
                                "obs_seq": seq,
                                "logical_clock_ms": clock,
                            }
                        )
                elif isinstance(value, list):
                    for seq, clock in enumerate(value, start=1):
                        entries.append(
                            {
                                "instance_id": instance_id,
                                "obs_seq": seq,
                                "logical_clock_ms": clock,
                            }
                        )
    else:
        raise BusinessError(
            "E_BAD_CLOCK_FILE",
            "逻辑时钟文件必须是数组或对象",
            {"file": str(path)},
            EXIT_USAGE,
        )

    clocks: dict[tuple[str, int], int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise BusinessError(
                "E_BAD_CLOCK_FILE",
                "逻辑时钟条目必须是对象",
                {"file": str(path), "entry": entry},
                EXIT_USAGE,
            )
        instance_id = _require_string(
            entry.get("instance_id"),
            "E_BAD_CLOCK_FILE",
            "逻辑时钟条目缺少实例 ID",
            details={"file": str(path)},
        )
        obs_seq = _require_int(
            entry.get("obs_seq"),
            "E_BAD_CLOCK_FILE",
            "逻辑时钟条目的观测序号必须是正整数",
            minimum=1,
            details={"file": str(path), "instance_id": instance_id},
        )
        clock = _require_int(
            entry.get("logical_clock_ms"),
            "E_BAD_CLOCK_FILE",
            "逻辑时钟必须是整数毫秒",
            minimum=0,
            details={"file": str(path), "instance_id": instance_id, "obs_seq": obs_seq},
        )
        key = (instance_id, obs_seq)
        if key in clocks and clocks[key] != clock:
            raise BusinessError(
                "E_BAD_CLOCK_FILE",
                "同一实例观测序号存在互斥逻辑时钟",
                {
                    "file": str(path),
                    "instance_id": instance_id,
                    "obs_seq": obs_seq,
                    "first": clocks[key],
                    "second": clock,
                },
                EXIT_USAGE,
            )
        clocks[key] = clock
    return clocks


def _parse_histogram(value: Any, details: dict[str, Any]) -> dict[str, int]:
    if not isinstance(value, dict) or not value:
        raise BusinessError(
            "E_BAD_OBS_SCHEMA",
            "v2 观测必须包含非空延迟直方图",
            details,
            EXIT_USAGE,
        )
    result: dict[str, int] = {}
    for key, count in value.items():
        if not isinstance(key, str) or not key:
            raise BusinessError(
                "E_BAD_OBS_SCHEMA",
                "延迟直方图桶名必须是非空字符串",
                details,
                EXIT_USAGE,
            )
        if not _is_int(count) or int(count) < 0:
            raise BusinessError(
                "E_BAD_OBS_SCHEMA",
                "延迟直方图计数必须是非负整数",
                {**details, "bucket": key},
                EXIT_USAGE,
            )
        result[key] = int(count)
    if not any(result.values()):
        raise BusinessError(
            "E_BAD_OBS_SCHEMA",
            "延迟直方图必须包含至少一个样本",
            details,
            EXIT_USAGE,
        )
    return dict(sorted(result.items()))


def _histogram_average(histogram: dict[str, int], details: dict[str, Any]) -> float:
    weighted = 0.0
    count = 0
    for label, occurrences in histogram.items():
        if occurrences == 0:
            continue
        try:
            if label.startswith("<"):
                point = float(label[1:]) / 2.0
            elif label.startswith(">="):
                point = float(label[2:])
            else:
                left, right = label.split("-", 1)
                point = (float(left) + float(right)) / 2.0
        except (TypeError, ValueError):
            raise BusinessError(
                "E_BAD_OBS_SCHEMA",
                "延迟直方图桶名无法解析为毫秒区间",
                {**details, "bucket": label},
                EXIT_USAGE,
            ) from None
        weighted += point * occurrences
        count += occurrences
    if count == 0:
        raise BusinessError(
            "E_BAD_OBS_SCHEMA",
            "延迟直方图为空",
            details,
            EXIT_USAGE,
        )
    return weighted / count


def _normalize_v1(
    raw: dict[str, Any],
    *,
    instance_id: str,
    obs_seq: int,
    logical_clock_ms: int,
    details: dict[str, Any],
) -> tuple[dict[str, Any], int, tuple[int, ...], float, None, dict[str, Any]]:
    status_code = _require_int(
        raw.get("status_code"),
        "E_BAD_OBS_SCHEMA",
        "v1 观测必须包含三位整数状态码",
        minimum=100,
        maximum=999,
        details=details,
    )
    latency = _number(
        raw.get("latency_ms"),
        "E_BAD_OBS_SCHEMA",
        "v1 观测必须包含非负相对延迟毫秒",
        details,
    )
    observed_at = math.floor(float(logical_clock_ms) - latency)
    if observed_at < 0:
        raise BusinessError(
            "E_BAD_OBS_SCHEMA",
            "v1 相对延迟换算后的绝对时刻不得为负",
            {**details, "observed_at_ms": observed_at},
            EXIT_USAGE,
        )
    time_conversion = {
        "method": "logical_clock_minus_relative_latency",
        "scale": 1,
        "rounding": "floor",
        "absolute_event_time_ms": observed_at,
    }
    canonical = {
        "schema_version": 1,
        "instance_id": instance_id,
        "obs_seq": obs_seq,
        "logical_clock_ms": logical_clock_ms,
        "status_code": status_code,
        "latency_ms": latency,
    }
    return canonical, observed_at, (status_code,), latency, None, time_conversion


def _normalize_v2(
    raw: dict[str, Any],
    *,
    instance_id: str,
    obs_seq: int,
    logical_clock_ms: int,
    details: dict[str, Any],
) -> tuple[dict[str, Any], int, tuple[int, ...], float, dict[str, int], None]:
    observed_at = _require_int(
        raw.get("observed_at_ms", raw.get("event_time_ms", raw.get("observed_at"))),
        "E_BAD_OBS_SCHEMA",
        "v2 观测必须包含非负绝对观测毫秒",
        minimum=0,
        details=details,
    )
    statuses = _validate_status_codes(
        raw.get(
            "status_codes",
            raw.get("status_code_set", raw.get("status_code")),
        ),
        instance_id=instance_id,
        code="E_BAD_OBS_SCHEMA",
    )
    histogram = _parse_histogram(
        raw.get(
            "latency_histogram",
            raw.get("delay_histogram", raw.get("latency_histogram_summary")),
        ),
        details,
    )
    summary_value = raw.get("latency_summary_ms", raw.get("latency_ms"))
    if summary_value is not None:
        latency_summary = _number(
            summary_value,
            "E_BAD_OBS_SCHEMA",
            "v2 latency_summary_ms 必须是非负数",
            details,
        )
    else:
        summary = raw.get("latency_summary")
        if isinstance(summary, dict) and summary.get("mean_ms") is not None:
            latency_summary = _number(
                summary["mean_ms"],
                "E_BAD_OBS_SCHEMA",
                "v2 latency_summary.mean_ms 必须是非负数",
                details,
            )
        elif (
            isinstance(summary, dict)
            and _is_int(summary.get("count"))
            and summary.get("count")
            and summary.get("sum_ms") is not None
        ):
            latency_summary = _number(
                summary["sum_ms"],
                "E_BAD_OBS_SCHEMA",
                "v2 latency_summary.sum_ms 必须是非负数",
                details,
            ) / float(summary["count"])
        else:
            latency_summary = _histogram_average(histogram, details)
    canonical = {
        "schema_version": 2,
        "instance_id": instance_id,
        "obs_seq": obs_seq,
        "logical_clock_ms": logical_clock_ms,
        "observed_at_ms": observed_at,
        "status_codes": list(statuses),
        "latency_histogram": histogram,
        "latency_summary_ms": latency_summary,
    }
    return canonical, observed_at, statuses, latency_summary, histogram, None


def _extract_observation_records(document: Any) -> tuple[dict[str, Any], list[Any]]:
    if isinstance(document, list):
        return {}, list(document)
    if not isinstance(document, dict):
        raise BusinessError(
            "E_BAD_SHARD",
            "分片必须是 JSON 对象或观测数组",
            {},
            EXIT_USAGE,
        )
    observations = document.get("observations")
    if not isinstance(observations, list):
        raise BusinessError(
            "E_BAD_SHARD",
            "分片缺少 observations 数组",
            {},
            EXIT_USAGE,
        )
    return document, observations


def _shard_content_digest(document: Any) -> str:
    if isinstance(document, dict):
        payload = {
            key: value
            for key, value in document.items()
            if key not in {"shard_digest", "content_digest", "content_hash"}
        }
    else:
        payload = document
    return sha256_digest(payload)


def _observation_digest_payload(
    schema_version: int,
    canonical: dict[str, Any],
) -> dict[str, Any]:
    if schema_version == 1:
        return {
            "schema_version": 1,
            "instance_id": canonical["instance_id"],
            "obs_seq": canonical["obs_seq"],
            "logical_clock_ms": canonical["logical_clock_ms"],
            "status_code": canonical["status_code"],
            "latency_ms": canonical["latency_ms"],
        }
    return {
        "schema_version": 2,
        "instance_id": canonical["instance_id"],
        "obs_seq": canonical["obs_seq"],
        "logical_clock_ms": canonical["logical_clock_ms"],
        "observed_at_ms": canonical["observed_at_ms"],
        "status_codes": canonical["status_codes"],
        "latency_histogram": canonical["latency_histogram"],
        "latency_summary_ms": canonical["latency_summary_ms"],
    }


def observation_digest(schema_version: int, canonical: dict[str, Any]) -> str:
    return sha256_digest(_observation_digest_payload(schema_version, canonical))


def parse_shard(
    path: str | os.PathLike[str],
    specs: dict[str, InstanceSpec],
    *,
    clocks: dict[tuple[str, int], int] | None = None,
    arrival_start: int = 0,
) -> ParsedShard:
    document = read_json(path)
    metadata, records = _extract_observation_records(document)
    file_name = Path(path).name
    shard_id = metadata.get("shard_id", Path(path).stem)
    shard_id = _require_string(
        shard_id,
        "E_BAD_SHARD",
        "分片 ID 必须是非空字符串",
        details={"file": str(path)},
    )
    content_digest = _shard_content_digest(document)
    declared_content_digest = metadata.get(
        "shard_digest",
        metadata.get("content_digest", metadata.get("content_hash")),
    )
    if declared_content_digest is not None:
        try:
            normalized_declared = normalize_digest(declared_content_digest)
        except ValueError as exc:
            raise BusinessError(
                "E_SHARD_DIGEST_MISMATCH",
                "分片内容摘要格式非法",
                {
                    "file": str(path),
                    "shard_id": shard_id,
                    "declared": declared_content_digest,
                    "calculated": content_digest,
                },
                EXIT_USAGE,
            ) from exc
        if normalized_declared != content_digest:
            raise BusinessError(
                "E_SHARD_DIGEST_MISMATCH",
                "分片内容摘要与规范化内容不一致",
                {
                    "file": str(path),
                    "shard_id": shard_id,
                    "declared": normalized_declared,
                    "calculated": content_digest,
                },
                EXIT_USAGE,
            )

    stream_id = metadata.get("stream_id", metadata.get("shard_set_id", "default"))
    stream_id = _require_string(
        stream_id,
        "E_BAD_SHARD",
        "stream_id 必须是非空字符串",
        details={"file": str(path), "shard_id": shard_id},
    )
    shard_index = metadata.get("shard_index")
    shard_count = metadata.get("shard_count")
    if shard_index is not None:
        shard_index = _require_int(
            shard_index,
            "E_BAD_SHARD",
            "shard_index 必须是非负整数",
            minimum=0,
            details={"file": str(path), "shard_id": shard_id},
        )
    if shard_count is not None:
        shard_count = _require_int(
            shard_count,
            "E_BAD_SHARD",
            "shard_count 必须是正整数",
            minimum=1,
            details={"file": str(path), "shard_id": shard_id},
        )
    if (shard_index is None) != (shard_count is None):
        raise BusinessError(
            "E_BAD_SHARD",
            "shard_index 与 shard_count 必须同时提供",
            {"file": str(path), "shard_id": shard_id},
            EXIT_USAGE,
        )
    if shard_index is not None and shard_index >= int(shard_count):
        raise BusinessError(
            "E_BAD_SHARD",
            "shard_index 必须小于 shard_count",
            {
                "file": str(path),
                "shard_id": shard_id,
                "shard_index": shard_index,
                "shard_count": shard_count,
            },
            EXIT_USAGE,
        )
    expected_raw = metadata.get("expected_shard_ids", [])
    if expected_raw is None:
        expected_raw = []
    if not isinstance(expected_raw, list) or any(
        not isinstance(item, str) or not item for item in expected_raw
    ):
        raise BusinessError(
            "E_BAD_SHARD",
            "expected_shard_ids 必须是非空字符串数组",
            {"file": str(path), "shard_id": shard_id},
            EXIT_USAGE,
        )
    expected_shard_ids = tuple(sorted(set(expected_raw)))

    default_version = metadata.get("schema_version", metadata.get("format_version", 1))
    observations: list[Observation] = []
    seen_in_file: set[tuple[str, int, str]] = set()
    clock_map = clocks or {}
    for index, raw in enumerate(records):
        arrival_order = arrival_start + index
        details: dict[str, Any] = {
            "file": str(path),
            "shard_id": shard_id,
            "record_index": index,
            "arrival_order": arrival_order,
        }
        if not isinstance(raw, dict):
            raise BusinessError(
                "E_BAD_OBS_SCHEMA",
                "观测记录必须是 JSON 对象",
                details,
                EXIT_USAGE,
            )
        instance_id = raw.get("instance_id", raw.get("instance"))
        if not isinstance(instance_id, str) or not instance_id:
            raise BusinessError(
                "E_BAD_INSTANCE",
                "观测记录必须引用非空实例 ID",
                details,
                EXIT_USAGE,
            )
        details["instance_id"] = instance_id
        if instance_id not in specs:
            raise BusinessError(
                "E_UNKNOWN_INSTANCE",
                "观测引用了注册清单中未声明的实例",
                details,
                EXIT_USAGE,
            )
        obs_seq = _require_int(
            raw.get("obs_seq"),
            "E_BAD_OBS_SEQ",
            "观测序号必须是正整数",
            minimum=1,
            details=details,
        )
        details["obs_seq"] = obs_seq
        clock_value = raw.get(
            "logical_clock_ms",
            raw.get("logical_clock", raw.get("clock_ms")),
        )
        clock_key = (instance_id, obs_seq)
        if clock_value is None and clock_key in clock_map:
            clock_value = clock_map[clock_key]
        if clock_value is None:
            raise BusinessError(
                "E_BAD_LOGICAL_CLOCK",
                "观测记录缺少注入逻辑时钟毫秒",
                details,
                EXIT_USAGE,
            )
        logical_clock_ms = _require_int(
            clock_value,
            "E_BAD_LOGICAL_CLOCK",
            "注入逻辑时钟必须是非负整数毫秒",
            minimum=0,
            details=details,
        )
        if clock_key in clock_map and clock_map[clock_key] != logical_clock_ms:
            raise BusinessError(
                "E_CLOCK_INJECTION_MISMATCH",
                "观测逻辑时钟与注入时钟序列不一致",
                {
                    **details,
                    "expected": clock_map[clock_key],
                    "actual": logical_clock_ms,
                },
                EXIT_USAGE,
            )
        schema_version = raw.get("schema_version", raw.get("version", default_version))
        if isinstance(schema_version, str) and schema_version.lower() in {"v1", "v2"}:
            schema_version = int(schema_version[1:])
        if not _is_int(schema_version) or int(schema_version) not in {1, 2}:
            raise BusinessError(
                "E_BAD_OBS_SCHEMA",
                "观测格式版本必须是 1 或 2",
                {**details, "schema_version": schema_version},
                EXIT_USAGE,
            )
        schema_version = int(schema_version)
        if schema_version == 1:
            (
                canonical,
                observed_at,
                statuses,
                latency_summary,
                histogram,
                time_conversion,
            ) = _normalize_v1(
                raw,
                instance_id=instance_id,
                obs_seq=obs_seq,
                logical_clock_ms=logical_clock_ms,
                details=details,
            )
        else:
            (
                canonical,
                observed_at,
                statuses,
                latency_summary,
                histogram,
                time_conversion,
            ) = _normalize_v2(
                raw,
                instance_id=instance_id,
                obs_seq=obs_seq,
                logical_clock_ms=logical_clock_ms,
                details=details,
            )
        declared_digest = raw.get(
            "digest",
            raw.get("observation_digest", raw.get("sha256")),
        )
        calculated_digest = observation_digest(schema_version, canonical)
        if declared_digest is None:
            raise BusinessError(
                "E_DIGEST_MISMATCH",
                "观测记录缺少摘要哈希",
                {**details, "calculated": calculated_digest},
                EXIT_USAGE,
            )
        try:
            normalized_digest = normalize_digest(declared_digest)
        except ValueError as exc:
            raise BusinessError(
                "E_DIGEST_MISMATCH",
                "观测摘要哈希格式非法",
                {
                    **details,
                    "declared": declared_digest,
                    "calculated": calculated_digest,
                },
                EXIT_USAGE,
            ) from exc
        if normalized_digest != calculated_digest:
            raise BusinessError(
                "E_DIGEST_MISMATCH",
                "观测摘要哈希与观测字段规范化摘要不一致",
                {
                    **details,
                    "declared": normalized_digest,
                    "calculated": calculated_digest,
                },
                EXIT_USAGE,
            )
        expected = set(specs[instance_id].expected_status_codes)
        outcome = "success" if all(code in expected for code in statuses) else "failure"
        semantic = semantic_digest(instance_id, obs_seq, outcome, list(statuses))
        dedupe_key = (instance_id, obs_seq, normalized_digest)
        if dedupe_key in seen_in_file:
            continue
        seen_in_file.add(dedupe_key)
        observations.append(
            Observation(
                instance_id=instance_id,
                obs_seq=obs_seq,
                schema_version=schema_version,
                logical_clock_ms=logical_clock_ms,
                observed_at_ms=observed_at,
                status_codes=statuses,
                latency_summary_ms=latency_summary,
                latency_histogram=histogram,
                time_conversion=time_conversion,
                digest=normalized_digest,
                semantic_digest=semantic,
                outcome=outcome,
                canonical=canonical,
                shard_id=shard_id,
                arrival_order=arrival_order,
            )
        )

    coverage_by_instance: dict[str, dict[str, Any]] = {}
    for obs in observations:
        entry = coverage_by_instance.get(obs.instance_id)
        if entry is None:
            coverage_by_instance[obs.instance_id] = {
                "instance_id": obs.instance_id,
                "min_seq": obs.obs_seq,
                "max_seq": obs.obs_seq,
                "observation_count": 1,
            }
        else:
            entry["min_seq"] = min(entry["min_seq"], obs.obs_seq)
            entry["max_seq"] = max(entry["max_seq"], obs.obs_seq)
            entry["observation_count"] += 1
    coverage = [
        coverage_by_instance[key]
        for key in sorted(coverage_by_instance)
    ]
    return ParsedShard(
        shard_id=shard_id,
        content_digest=content_digest,
        stream_id=stream_id,
        shard_index=shard_index,
        shard_count=shard_count,
        expected_shard_ids=expected_shard_ids,
        file_name=file_name,
        observations=observations,
        coverage=coverage,
    )
