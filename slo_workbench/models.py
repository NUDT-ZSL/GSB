"""Typed, validated domain models for SLO definitions and observed events."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List

from .decimal_math import HUNDRED, ZERO, to_decimal
from .errors import bad_event, bad_slo

VALID_SLI_TYPES = ("availability", "latency")


@dataclass(frozen=True)
class SLO:
    name: str
    service: str
    sli_type: str
    target: Decimal
    window_length_ms: int
    window_step_ms: int
    compliance_period_ms: int
    burn_alert_threshold: Decimal
    burn_consecutive_windows: int
    release_consecutive_windows: int
    latency_threshold_ms: Decimal | None = None
    latency_percentile: Decimal | None = None
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "name": self.name,
            "service": self.service,
            "sli_type": self.sli_type,
            "target": str(self.target),
            "window_length_ms": self.window_length_ms,
            "window_step_ms": self.window_step_ms,
            "compliance_period_ms": self.compliance_period_ms,
            "burn_alert_threshold": str(self.burn_alert_threshold),
            "burn_consecutive_windows": self.burn_consecutive_windows,
            "release_consecutive_windows": self.release_consecutive_windows,
            "version": self.version,
        }
        if self.sli_type == "latency":
            payload["latency_threshold_ms"] = str(self.latency_threshold_ms)
            payload["latency_percentile"] = str(self.latency_percentile)
        return payload


def _require_int(obj: Any, field_name: str, *, slo_name: str | None = None) -> int:
    if isinstance(obj, bool) or not isinstance(obj, int):
        raise bad_slo(
            f"{field_name} must be a positive integer",
            slo_name=slo_name,
            field=field_name,
            value=obj,
        )
    return obj


def parse_slo(raw: Dict[str, Any], version: int = 1) -> SLO:
    """Validate one SLO definition, raising :class:`BusinessError` on problems."""
    if not isinstance(raw, dict):
        raise bad_slo("SLO definition must be an object", value=type(raw).__name__)
    name = raw.get("name")
    service = raw.get("service")
    if not isinstance(name, str) or not name.strip():
        raise bad_slo("SLO name must be a non-empty string", value=name)
    if not isinstance(service, str) or not service.strip():
        raise bad_slo("service name must be a non-empty string", slo_name=name, value=service)

    sli_type = raw.get("sli_type")
    if sli_type not in VALID_SLI_TYPES:
        raise bad_slo(
            f"sli_type must be one of {list(VALID_SLI_TYPES)}",
            slo_name=name,
            value=sli_type,
        )

    try:
        target = to_decimal(raw.get("target"), "target")
    except TypeError as exc:
        raise bad_slo(str(exc), slo_name=name) from exc
    # Target must lie inside the open interval (0, 100).
    if not (ZERO < target < HUNDRED):
        raise bad_slo(
            "target must lie in the open interval (0, 100)",
            slo_name=name,
            value=str(target),
        )

    window_length = _require_int(raw.get("window_length_ms"), "window_length_ms", slo_name=name)
    window_step = _require_int(raw.get("window_step_ms"), "window_step_ms", slo_name=name)
    period = _require_int(raw.get("compliance_period_ms"), "compliance_period_ms", slo_name=name)
    if window_length <= 0:
        raise bad_slo("window_length_ms must be positive", slo_name=name, value=window_length)
    if window_step <= 0:
        raise bad_slo("window_step_ms must be positive", slo_name=name, value=window_step)
    if window_step > window_length:
        raise bad_slo(
            "window_step_ms must not exceed window_length_ms",
            slo_name=name,
            window_step_ms=window_step,
            window_length_ms=window_length,
        )
    if period <= 0 or period < window_length:
        raise bad_slo(
            "compliance_period_ms must be positive and >= window_length_ms",
            slo_name=name,
            value=period,
        )

    try:
        burn_threshold = to_decimal(raw.get("burn_alert_threshold"), "burn_alert_threshold")
    except TypeError as exc:
        raise bad_slo(str(exc), slo_name=name) from exc
    if burn_threshold <= 0:
        raise bad_slo(
            "burn_alert_threshold must be positive", slo_name=name, value=str(burn_threshold)
        )

    burn_windows = _require_int(
        raw.get("burn_consecutive_windows", 1), "burn_consecutive_windows", slo_name=name
    )
    release_windows = _require_int(
        raw.get("release_consecutive_windows", 2), "release_consecutive_windows", slo_name=name
    )
    if burn_windows <= 0 or release_windows <= 0:
        raise bad_slo(
            "consecutive window counts must be positive integers",
            slo_name=name,
            burn_consecutive_windows=burn_windows,
            release_consecutive_windows=release_windows,
        )

    latency_threshold = None
    latency_percentile = None
    if sli_type == "latency":
        try:
            latency_threshold = to_decimal(
                raw.get("latency_threshold_ms"), "latency_threshold_ms"
            )
            latency_percentile = to_decimal(
                raw.get("latency_percentile"), "latency_percentile"
            )
        except TypeError as exc:
            raise bad_slo(str(exc), slo_name=name) from exc
        if latency_threshold < 0:
            raise bad_slo(
                "latency_threshold_ms must be non-negative",
                slo_name=name,
                value=str(latency_threshold),
            )
        if not (ZERO < latency_percentile <= HUNDRED):
            raise bad_slo(
                "latency_percentile must lie in (0, 100]",
                slo_name=name,
                value=str(latency_percentile),
            )

    return SLO(
        name=name,
        service=service,
        sli_type=sli_type,
        target=target,
        window_length_ms=window_length,
        window_step_ms=window_step,
        compliance_period_ms=period,
        burn_alert_threshold=burn_threshold,
        burn_consecutive_windows=burn_windows,
        release_consecutive_windows=release_windows,
        latency_threshold_ms=latency_threshold,
        latency_percentile=latency_percentile,
        version=version,
    )


def parse_slo_list(raw: Any) -> List[SLO]:
    """Validate a whole SLO document including uniqueness constraints."""
    if not isinstance(raw, list) or not raw:
        raise bad_slo("SLO document must be a non-empty list")
    slos: List[SLO] = []
    seen_names: set[str] = set()
    seen_services: set[str] = set()
    for item in raw:
        slo = parse_slo(item)
        if slo.name in seen_names:
            raise bad_slo("duplicate SLO name", slo_name=slo.name)
        if slo.service in seen_services:
            raise bad_slo("duplicate service name", slo_name=slo.name, service=slo.service)
        seen_names.add(slo.name)
        seen_services.add(slo.service)
        slos.append(slo)
    return slos


@dataclass(frozen=True)
class Event:
    seq: int
    service: str
    clock_ms: int
    success: bool
    latency_ms: Decimal
    valid: bool


def parse_event(raw: Dict[str, Any]) -> Event:
    if not isinstance(raw, dict):
        raise bad_event("event must be an object", value=type(raw).__name__)
    seq = raw.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise bad_event("event seq must be an integer", value=seq)
    service = raw.get("service")
    if not isinstance(service, str) or not service.strip():
        raise bad_event("event service must be a non-empty string", seq=seq, value=service)
    clock = raw.get("clock_ms")
    if isinstance(clock, bool) or not isinstance(clock, int):
        raise bad_event("clock_ms must be an integer", seq=seq, service=service, value=clock)
    if clock < 0:
        raise bad_event("clock_ms must be non-negative", seq=seq, service=service, value=clock)

    success = raw.get("success")
    if not isinstance(success, bool):
        raise bad_event(
            "success flag must be boolean", seq=seq, service=service, value=success
        )
    valid = raw.get("valid")
    if not isinstance(valid, bool):
        raise bad_event(
            "valid flag must be boolean", seq=seq, service=service, value=valid
        )
    try:
        latency = to_decimal(raw.get("latency_ms", 0), "latency_ms")
    except TypeError as exc:
        raise bad_event(str(exc), seq=seq, service=service) from exc
    if latency < 0:
        raise bad_event("latency_ms must be non-negative", seq=seq, service=service)
    return Event(
        seq=seq,
        service=service,
        clock_ms=clock,
        success=success,
        latency_ms=latency,
        valid=valid,
    )


def parse_event_list(raw: Any) -> List[Event]:
    """Validate an event stream: strictly increasing sequence numbers."""
    if not isinstance(raw, list):
        raise bad_event("event stream must be a list")
    events: List[Event] = []
    previous_seq: int | None = None
    for item in raw:
        event = parse_event(item)
        if previous_seq is not None and event.seq <= previous_seq:
            raise bad_event(
                "event seq values must be strictly increasing",
                seq=event.seq,
                previous_seq=previous_seq,
            )
        previous_seq = event.seq
        events.append(event)
    return events


@dataclass
class ClockConfig:
    """Deterministic injected-clock configuration (no wall clock ever read)."""

    ticks_ms: List[int] = field(default_factory=list)
    rollback_tolerance_ms: int = 0


def parse_clock_config(raw: Any) -> ClockConfig:
    if not isinstance(raw, dict):
        raise bad_slo("clock config must be an object")
    ticks = raw.get("ticks_ms")
    if not isinstance(ticks, list) or not ticks:
        raise bad_slo("clock config ticks_ms must be a non-empty list")
    normalised: List[int] = []
    for tick in ticks:
        if isinstance(tick, bool) or not isinstance(tick, int) or tick < 0:
            raise bad_slo("each clock tick must be a non-negative integer", value=tick)
        normalised.append(tick)
    tolerance = raw.get("rollback_tolerance_ms", 0)
    if isinstance(tolerance, bool) or not isinstance(tolerance, int) or tolerance < 0:
        raise bad_slo("rollback_tolerance_ms must be a non-negative integer", value=tolerance)
    return ClockConfig(ticks_ms=normalised, rollback_tolerance_ms=tolerance)
