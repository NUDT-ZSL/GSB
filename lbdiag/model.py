"""Sample parsing, strict validation, hashing, and deterministic scheduling."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import (
    E_BAD_DIAGNOSTICS,
    E_BAD_INSTANCE,
    E_BAD_PLAN,
    E_BAD_SAMPLE,
    E_SAMPLE_MISMATCH,
    BusinessError,
    fail,
)
from .fixed import dec, quantize_fixed

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CLASSIFICATIONS = (
    "success",
    "status_mismatch",
    "timeout",
    "connection_refused",
    "malformed_response",
    "sample_mismatch",
)
STATES = ("HEALTHY", "SUSPECT", "UNHEALTHY")


@dataclass(frozen=True)
class Address:
    scheme: str
    host: str
    port: int
    path: str

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.authority}{self.path}"


@dataclass(frozen=True)
class InstanceConfig:
    instance_id: str
    address: Address
    weight: int
    expected_status: int
    expected_body_sha256: str | None
    failure_threshold: int
    success_threshold: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoreWeights:
    success: Decimal
    status_mismatch: Decimal
    timeout: Decimal
    connection_refused: Decimal
    malformed_response: Decimal
    sample_mismatch: Decimal

    def for_class(self, classification: str) -> Decimal:
        return getattr(self, classification)


@dataclass(frozen=True)
class SlowStart:
    initial_ratio: Decimal
    ramp_steps: int
    curve: str
    suspect_ratio: Decimal
    exponent: Decimal


@dataclass(frozen=True)
class Plan:
    rounds: int
    interval_ms: int
    timeout_ms: int
    jitter_ratio: Decimal
    window_length: int
    window_step: int
    healthy_success_rate: Decimal
    suspect_success_rate: Decimal
    recovery_success_rate: Decimal
    score_weights: ScoreWeights
    slow_start: SlowStart
    initial_state: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Diagnostics:
    float_tolerance: Decimal
    fixed_scale: int
    logical_clock_start_ms: int
    logical_clock_step_ms: int
    clock_rollback_tolerance_ms: int
    sample_sha256: str | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkbenchConfig:
    instances: tuple[InstanceConfig, ...]
    plan: Plan
    diagnostics: Diagnostics
    sample_sha256: str
    raw: dict[str, Any]

    def instance(self, instance_id: str) -> InstanceConfig:
        for instance in self.instances:
            if instance.instance_id == instance_id:
                return instance
        fail(
            E_BAD_INSTANCE,
            "instance does not exist in sample",
            fields={"instance_id": instance_id},
        )
        raise AssertionError("unreachable")


@dataclass(frozen=True)
class ScheduledCheck:
    instance_id: str
    check_index: int
    scheduled_at_ms: int
    jitter_offset_ms: int


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_object(value: Any, code: str, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(code, f"{label} must be a JSON object", fields={"field": label})
    return value


def _require_int(
    obj: dict[str, Any],
    key: str,
    code: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if key not in obj or not _is_int(obj[key]):
        fail(code, f"{key} must be an integer", fields={"field": key})
    value = obj[key]
    if minimum is not None and value < minimum:
        fail(
            code,
            f"{key} must be at least {minimum}",
            fields={"field": key, "value": value, "minimum": minimum},
        )
    if maximum is not None and value > maximum:
        fail(
            code,
            f"{key} must be at most {maximum}",
            fields={"field": key, "value": value, "maximum": maximum},
        )
    return value


def _require_decimal(
    obj: dict[str, Any],
    key: str,
    code: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    maximum_exclusive: bool = False,
) -> Decimal:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(code, f"{key} must be numeric", fields={"field": key})
    number = dec(value)
    if not number.is_finite():
        fail(code, f"{key} must be finite", fields={"field": key})
    if minimum is not None and number < minimum:
        fail(
            code,
            f"{key} is below the allowed range",
            fields={"field": key, "value": str(number), "minimum": str(minimum)},
        )
    if maximum is not None:
        invalid = number >= maximum if maximum_exclusive else number > maximum
        if invalid:
            fail(
                code,
                f"{key} is above the allowed range",
                fields={
                    "field": key,
                    "value": str(number),
                    "maximum": str(maximum),
                    "exclusive": maximum_exclusive,
                },
            )
    return number


def _parse_address(value: Any, instance_id: str) -> Address:
    if not isinstance(value, str) or not value:
        fail(
            E_BAD_INSTANCE,
            "instance address must be a non-empty string",
            fields={"instance_id": instance_id, "field": "address"},
        )
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        fail(
            E_BAD_INSTANCE,
            "instance address is not a valid URL",
            fields={"instance_id": instance_id, "address": value, "reason": str(exc)},
        )
    if parsed.scheme != "http":
        fail(
            E_BAD_INSTANCE,
            "only plain HTTP loopback addresses are supported",
            fields={"instance_id": instance_id, "address": value},
        )
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        fail(
            E_BAD_INSTANCE,
            "instance address must not contain credentials, query, or fragment",
            fields={"instance_id": instance_id, "address": value},
        )
    host = parsed.hostname
    if not host:
        fail(
            E_BAD_INSTANCE,
            "instance address has no host",
            fields={"instance_id": instance_id, "address": value},
        )
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        fail(
            E_BAD_INSTANCE,
            "instance host must be a literal loopback IP address",
            fields={"instance_id": instance_id, "host": host},
        )
    if not (ip.is_loopback or (ip.version == 4 and ip in ipaddress.ip_network("127.0.0.0/8"))):
        fail(
            E_BAD_INSTANCE,
            "instance host is not a loopback address",
            fields={"instance_id": instance_id, "host": host},
        )
    try:
        port = parsed.port
    except ValueError as exc:
        fail(
            E_BAD_INSTANCE,
            "instance address has an invalid port",
            fields={"instance_id": instance_id, "address": value, "reason": str(exc)},
        )
    if port is None or not (1 <= port <= 65535):
        fail(
            E_BAD_INSTANCE,
            "instance address must include a port in 1..65535",
            fields={"instance_id": instance_id, "address": value},
        )
    path = parsed.path or "/"
    if not path.startswith("/"):
        fail(
            E_BAD_INSTANCE,
            "instance path must be absolute",
            fields={"instance_id": instance_id, "path": path},
        )
    return Address("http", host, port, path)


def _parse_instances(raw_instances: Any) -> tuple[InstanceConfig, ...]:
    if not isinstance(raw_instances, list) or not raw_instances:
        fail(E_BAD_INSTANCE, "instances must be a non-empty array", fields={"field": "instances"})
    result: list[InstanceConfig] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_instances):
        if not isinstance(raw, dict):
            fail(
                E_BAD_INSTANCE,
                "each instance must be an object",
                fields={"index": index},
            )
        instance_id = raw.get("id")
        if not isinstance(instance_id, str) or not ID_RE.fullmatch(instance_id):
            fail(
                E_BAD_INSTANCE,
                "instance id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}",
                fields={"index": index, "id": instance_id},
            )
        if instance_id in seen:
            fail(
                E_BAD_INSTANCE,
                "instance id must be unique",
                fields={"index": index, "instance_id": instance_id},
            )
        seen.add(instance_id)
        weight = _require_int(raw, "weight", E_BAD_INSTANCE, minimum=1)
        expected_status = _require_int(
            raw, "expected_status", E_BAD_INSTANCE, minimum=100, maximum=599
        )
        failure_threshold = _require_int(
            raw, "failure_threshold", E_BAD_INSTANCE, minimum=1
        )
        success_threshold = _require_int(
            raw, "success_threshold", E_BAD_INSTANCE, minimum=1
        )
        body_hash = raw.get("expected_body_sha256")
        if body_hash is not None:
            if not isinstance(body_hash, str) or not SHA256_RE.fullmatch(body_hash):
                fail(
                    E_BAD_INSTANCE,
                    "expected_body_sha256 must be 64 lowercase hex characters",
                    fields={"instance_id": instance_id},
                )
        result.append(
            InstanceConfig(
                instance_id=instance_id,
                address=_parse_address(raw.get("address"), instance_id),
                weight=weight,
                expected_status=expected_status,
                expected_body_sha256=body_hash,
                failure_threshold=failure_threshold,
                success_threshold=success_threshold,
                raw=dict(raw),
            )
        )
    return tuple(result)


def _parse_score_weights(raw: Any) -> ScoreWeights:
    defaults = {
        "success": 1.0,
        "status_mismatch": 0.35,
        "timeout": 0.0,
        "connection_refused": 0.0,
        "malformed_response": 0.0,
        "sample_mismatch": 0.0,
    }
    obj = defaults | (_require_object(raw, E_BAD_PLAN, "score_weights") if raw is not None else {})
    values: dict[str, Decimal] = {}
    for name in CLASSIFICATIONS:
        values[name] = _require_decimal(
            obj, name, E_BAD_PLAN, minimum=Decimal("0"), maximum=Decimal("1")
        )
    if values["success"] != Decimal("1"):
        fail(
            E_BAD_PLAN,
            "score_weights.success must equal 1",
            fields={"field": "score_weights.success"},
        )
    return ScoreWeights(**values)


def _parse_slow_start(raw: Any) -> SlowStart:
    obj = _require_object(raw, E_BAD_PLAN, "slow_start")
    initial = _require_decimal(
        obj, "initial_ratio", E_BAD_PLAN, minimum=Decimal("0"), maximum=Decimal("1")
    )
    suspect = _require_decimal(
        obj, "suspect_ratio", E_BAD_PLAN, minimum=Decimal("0"), maximum=Decimal("1")
    )
    exponent = _require_decimal(
        obj,
        "exponent",
        E_BAD_PLAN,
        minimum=Decimal("0.000001"),
        maximum=Decimal("100"),
    )
    ramp_steps = _require_int(obj, "ramp_steps", E_BAD_PLAN, minimum=1)
    curve = obj.get("curve", "linear")
    if curve not in {"linear", "power"}:
        fail(
            E_BAD_PLAN,
            "slow_start.curve must be linear or power",
            fields={"value": curve},
        )
    if curve == "linear" and exponent != Decimal("1"):
        fail(
            E_BAD_PLAN,
            "linear slow-start curve requires exponent 1",
            fields={"exponent": str(exponent)},
        )
    return SlowStart(initial, ramp_steps, curve, suspect, exponent)


def _parse_plan(raw_plan: Any) -> Plan:
    obj = _require_object(raw_plan, E_BAD_PLAN, "plan")
    rounds = _require_int(obj, "rounds", E_BAD_PLAN, minimum=1)
    interval_ms = _require_int(obj, "interval_ms", E_BAD_PLAN, minimum=1)
    timeout_ms = _require_int(obj, "timeout_ms", E_BAD_PLAN, minimum=1)
    if interval_ms <= timeout_ms:
        fail(
            E_BAD_PLAN,
            "interval_ms must be greater than timeout_ms",
            fields={"interval_ms": interval_ms, "timeout_ms": timeout_ms},
        )
    jitter = _require_decimal(
        obj,
        "jitter_ratio",
        E_BAD_PLAN,
        minimum=Decimal("0"),
        maximum=Decimal("1"),
        maximum_exclusive=True,
    )
    window_length = _require_int(obj, "window_length", E_BAD_PLAN, minimum=1)
    window_step = _require_int(obj, "window_step", E_BAD_PLAN, minimum=1)
    if window_step > window_length:
        fail(
            E_BAD_PLAN,
            "window_step must not exceed window_length",
            fields={"window_length": window_length, "window_step": window_step},
        )
    if rounds < window_length:
        fail(
            E_BAD_PLAN,
            "rounds must be at least window_length",
            fields={"rounds": rounds, "window_length": window_length},
        )
    healthy_rate = _require_decimal(
        obj,
        "healthy_success_rate",
        E_BAD_PLAN,
        minimum=Decimal("0"),
        maximum=Decimal("1"),
    )
    suspect_rate = _require_decimal(
        obj,
        "suspect_success_rate",
        E_BAD_PLAN,
        minimum=Decimal("0"),
        maximum=Decimal("1"),
    )
    recovery_rate = _require_decimal(
        obj,
        "recovery_success_rate",
        E_BAD_PLAN,
        minimum=Decimal("0"),
        maximum=Decimal("1"),
    )
    if suspect_rate > healthy_rate:
        fail(
            E_BAD_PLAN,
            "suspect_success_rate must not exceed healthy_success_rate",
            fields={
                "suspect_success_rate": str(suspect_rate),
                "healthy_success_rate": str(healthy_rate),
            },
        )
    initial_state = obj.get("initial_state", "HEALTHY")
    if initial_state not in STATES:
        fail(
            E_BAD_PLAN,
            "initial_state must be HEALTHY, SUSPECT, or UNHEALTHY",
            fields={"value": initial_state},
        )
    return Plan(
        rounds=rounds,
        interval_ms=interval_ms,
        timeout_ms=timeout_ms,
        jitter_ratio=jitter,
        window_length=window_length,
        window_step=window_step,
        healthy_success_rate=healthy_rate,
        suspect_success_rate=suspect_rate,
        recovery_success_rate=recovery_rate,
        score_weights=_parse_score_weights(obj.get("score_weights")),
        slow_start=_parse_slow_start(obj.get("slow_start")),
        initial_state=initial_state,
        raw=dict(obj),
    )


def _parse_diagnostics(raw_diagnostics: Any) -> Diagnostics:
    obj = _require_object(raw_diagnostics, E_BAD_DIAGNOSTICS, "diagnostics")
    tolerance = _require_decimal(
        obj,
        "float_tolerance",
        E_BAD_DIAGNOSTICS,
        minimum=Decimal("0"),
        maximum=Decimal("1"),
    )
    scale = _require_int(
        obj, "fixed_scale", E_BAD_DIAGNOSTICS, minimum=1, maximum=1000000000
    )
    clock_start = _require_int(
        obj, "logical_clock_start_ms", E_BAD_DIAGNOSTICS, minimum=0
    )
    clock_step = _require_int(
        obj, "logical_clock_step_ms", E_BAD_DIAGNOSTICS, minimum=1
    )
    rollback = _require_int(
        obj, "clock_rollback_tolerance_ms", E_BAD_DIAGNOSTICS, minimum=0
    )
    digest = obj.get("sample_sha256")
    if digest is not None:
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            fail(
                E_BAD_DIAGNOSTICS,
                "sample_sha256 must be 64 lowercase hex characters",
                fields={"field": "diagnostics.sample_sha256"},
            )
    return Diagnostics(
        float_tolerance=tolerance,
        fixed_scale=scale,
        logical_clock_start_ms=clock_start,
        logical_clock_step_ms=clock_step,
        clock_rollback_tolerance_ms=rollback,
        sample_sha256=digest,
        raw=dict(obj),
    )


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def compute_sample_sha256(raw: dict[str, Any]) -> str:
    body = {
        "instances": raw.get("instances"),
        "plan": raw.get("plan"),
        "diagnostics": {
            key: value
            for key, value in (raw.get("diagnostics") or {}).items()
            if key != "sample_sha256"
        },
        "test_service": raw.get("test_service"),
    }
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def parse_sample_file(path: str | Path) -> WorkbenchConfig:
    sample_path = Path(path)
    try:
        raw_bytes = sample_path.read_bytes()
    except OSError as exc:
        fail(E_BAD_SAMPLE, "cannot read sample file", fields={"path": str(sample_path), "reason": str(exc)})
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(
            E_BAD_SAMPLE,
            "sample is not valid UTF-8 JSON",
            fields={"path": str(sample_path), "reason": str(exc)},
        )
    return parse_sample(raw)


def parse_sample(raw: Any) -> WorkbenchConfig:
    if not isinstance(raw, dict):
        fail(E_BAD_SAMPLE, "sample root must be a JSON object")
    missing = [key for key in ("instances", "plan", "diagnostics") if key not in raw]
    if missing:
        fail(E_BAD_SAMPLE, "sample is missing required sections", fields={"missing": missing})
    instances = _parse_instances(raw["instances"])
    plan = _parse_plan(raw["plan"])
    diagnostics = _parse_diagnostics(raw["diagnostics"])
    fingerprint = compute_sample_sha256(raw)
    if diagnostics.sample_sha256 and diagnostics.sample_sha256 != fingerprint:
        fail(
            E_SAMPLE_MISMATCH,
            "sample digest does not match canonical sample content",
            fields={
                "expected": diagnostics.sample_sha256,
                "actual": fingerprint,
            },
        )
    test_service = raw.get("test_service")
    if test_service is not None:
        obj = _require_object(test_service, E_BAD_SAMPLE, "test_service")
        host = obj.get("host", "127.0.0.1")
        if host not in {"127.0.0.1", "::1"}:
            fail(
                E_BAD_SAMPLE,
                "test_service.host must be 127.0.0.1 or ::1",
                fields={"host": host},
            )
        _require_int(obj, "port", E_BAD_SAMPLE, minimum=1, maximum=65535)
        routes = obj.get("routes", {})
        if not isinstance(routes, dict):
            fail(E_BAD_SAMPLE, "test_service.routes must be an object")
    return WorkbenchConfig(
        instances=instances,
        plan=plan,
        diagnostics=diagnostics,
        sample_sha256=fingerprint,
        raw=raw,
    )


def jitter_offset(instance_id: str, check_index: int, config: WorkbenchConfig) -> int:
    material = f"{config.sample_sha256}:{instance_id}:{check_index}".encode("utf-8")
    number = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    unit = Decimal(number) / Decimal(2**64 - 1)
    signed = unit * 2 - 1
    exact = signed * config.plan.jitter_ratio * config.plan.interval_ms
    fixed, _ = quantize_fixed(exact, 1)
    return fixed


def schedule_checks(
    config: WorkbenchConfig,
    *,
    start_ms: int | None = None,
) -> dict[str, tuple[ScheduledCheck, ...]]:
    base = config.diagnostics.logical_clock_start_ms if start_ms is None else start_ms
    schedules: dict[str, tuple[ScheduledCheck, ...]] = {}
    for instance in config.instances:
        checks: list[ScheduledCheck] = []
        for check_index in range(config.plan.rounds):
            offset = jitter_offset(instance.instance_id, check_index, config)
            checks.append(
                ScheduledCheck(
                    instance_id=instance.instance_id,
                    check_index=check_index,
                    scheduled_at_ms=base + check_index * config.plan.interval_ms + offset,
                    jitter_offset_ms=offset,
                )
            )
        schedules[instance.instance_id] = tuple(checks)
    return schedules


def window_end_indices(plan: Plan, available_checks: int) -> list[int]:
    ends: list[int] = []
    close_count = plan.window_length
    while close_count <= available_checks:
        ends.append(close_count - 1)
        close_count += plan.window_step
    return ends


def fixed_scale(config: WorkbenchConfig) -> int:
    return config.diagnostics.fixed_scale


def business_error_from(exc: BusinessError) -> dict[str, Any]:
    return exc.as_dict()

