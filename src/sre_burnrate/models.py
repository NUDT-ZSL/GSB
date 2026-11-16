"""Serializable domain models shared by evaluation, snapshots, and replay."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any, Mapping

from .config import EvaluationConfig
from .errors import SnapshotError
from .fixed import FixedPoint


def minute_to_iso(minute_epoch_seconds: int) -> str:
    dt = datetime.fromtimestamp(minute_epoch_seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_minute(value: str) -> int:
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise SnapshotError(f"invalid ISO minute in snapshot: {value!r}") from exc
    return calendar.timegm(dt.utctimetuple())


@dataclass(frozen=True, slots=True)
class Event:
    minute_start: int
    total_requests: int
    failed_requests: int
    line_no: int = 0


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    data_sha256: str
    row_count: int
    first_minute: int | None
    last_minute: int | None
    input_format: str


@dataclass(frozen=True, slots=True)
class WindowResult:
    window_id: str
    kind: str
    start_minute: int
    end_minute_exclusive: int
    start_iso: str
    end_iso_exclusive: str
    length_minutes: int
    step_minutes: int
    partial: bool
    coverage_minutes: int
    observed_minutes: int
    total_requests: int
    failed_requests: int
    sli: FixedPoint
    error_rate: FixedPoint
    budget_fraction: FixedPoint
    budget_total: FixedPoint
    budget_consumed: FixedPoint
    budget_remaining: FixedPoint
    burn_rate: FixedPoint
    slo_version: str
    exact_sli: Fraction | None = field(default=None, compare=False, repr=False)
    exact_burn_rate: Fraction | None = field(default=None, compare=False, repr=False)

    @property
    def start_minute_iso(self) -> str:
        return self.start_iso

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "kind": self.kind,
            "start_minute": self.start_minute,
            "end_minute_exclusive": self.end_minute_exclusive,
            "start_iso": self.start_iso,
            "end_iso_exclusive": self.end_iso_exclusive,
            "length_minutes": self.length_minutes,
            "step_minutes": self.step_minutes,
            "partial": self.partial,
            "coverage_minutes": self.coverage_minutes,
            "observed_minutes": self.observed_minutes,
            "total_requests": self.total_requests,
            "failed_requests": self.failed_requests,
            "sli": self.sli.to_dict(),
            "error_rate": self.error_rate.to_dict(),
            "budget_fraction": self.budget_fraction.to_dict(),
            "budget_total": self.budget_total.to_dict(),
            "budget_consumed": self.budget_consumed.to_dict(),
            "budget_remaining": self.budget_remaining.to_dict(),
            "burn_rate": self.burn_rate.to_dict(),
            "slo_version": self.slo_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], config: EvaluationConfig) -> "WindowResult":
        data = _mapping(payload, "window")
        total = _int(data.get("total_requests"), "window.total_requests", minimum=0)
        failed = _int(data.get("failed_requests"), "window.failed_requests", minimum=0)
        if failed > total:
            raise SnapshotError("snapshot window has failed_requests > total_requests")
        if total > 0:
            exact_sli: Fraction | None = Fraction(total - failed, total)
            exact_burn: Fraction | None = exact_sli / config.slo.budget_fraction
        else:
            exact_sli = None
            exact_burn = None
        start = _int(data.get("start_minute"), "window.start_minute")
        end = _int(data.get("end_minute_exclusive"), "window.end_minute_exclusive")
        partial = data.get("partial")
        if not isinstance(partial, bool):
            raise SnapshotError("snapshot window.partial must be boolean")
        return cls(
            window_id=_string(data.get("window_id"), "window.window_id"),
            kind=_string(data.get("kind"), "window.kind"),
            start_minute=start,
            end_minute_exclusive=end,
            start_iso=_string(data.get("start_iso"), "window.start_iso"),
            end_iso_exclusive=_string(data.get("end_iso_exclusive"), "window.end_iso_exclusive"),
            length_minutes=_int(data.get("length_minutes"), "window.length_minutes", minimum=1),
            step_minutes=_int(data.get("step_minutes"), "window.step_minutes", minimum=1),
            partial=partial,
            coverage_minutes=_int(data.get("coverage_minutes"), "window.coverage_minutes", minimum=0),
            observed_minutes=_int(data.get("observed_minutes"), "window.observed_minutes", minimum=0),
            total_requests=total,
            failed_requests=failed,
            sli=FixedPoint.from_dict(_mapping(data.get("sli"), "window.sli"), context="window.sli"),
            error_rate=FixedPoint.from_dict(
                _mapping(data.get("error_rate"), "window.error_rate"), context="window.error_rate"
            ),
            budget_fraction=FixedPoint.from_dict(
                _mapping(data.get("budget_fraction"), "window.budget_fraction"),
                context="window.budget_fraction",
            ),
            budget_total=FixedPoint.from_dict(
                _mapping(data.get("budget_total"), "window.budget_total"), context="window.budget_total"
            ),
            budget_consumed=FixedPoint.from_dict(
                _mapping(data.get("budget_consumed"), "window.budget_consumed"),
                context="window.budget_consumed",
            ),
            budget_remaining=FixedPoint.from_dict(
                _mapping(data.get("budget_remaining"), "window.budget_remaining"),
                context="window.budget_remaining",
            ),
            burn_rate=FixedPoint.from_dict(
                _mapping(data.get("burn_rate"), "window.burn_rate"), context="window.burn_rate"
            ),
            slo_version=_string(data.get("slo_version"), "window.slo_version"),
            exact_sli=exact_sli,
            exact_burn_rate=exact_burn,
        )


@dataclass(frozen=True, slots=True)
class AlertRecord:
    step_index: int
    step_id: str
    state: str
    reason: str
    evaluated_start_minute: int
    evaluated_start_iso: str
    long_window_id: str
    short_window_id: str
    long_burn_rate: FixedPoint
    short_burn_rate: FixedPoint
    triggered_window_ids: tuple[str, ...]
    slo_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "step_id": self.step_id,
            "state": self.state,
            "reason": self.reason,
            "evaluated_start_minute": self.evaluated_start_minute,
            "evaluated_start_iso": self.evaluated_start_iso,
            "long_window_id": self.long_window_id,
            "short_window_id": self.short_window_id,
            "long_burn_rate": self.long_burn_rate.to_dict(),
            "short_burn_rate": self.short_burn_rate.to_dict(),
            "triggered_window_ids": list(self.triggered_window_ids),
            "slo_version": self.slo_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AlertRecord":
        data = _mapping(payload, "alert record")
        triggered = data.get("triggered_window_ids")
        if not isinstance(triggered, list) or not all(isinstance(item, str) for item in triggered):
            raise SnapshotError("alert record.triggered_window_ids must be a string list")
        return cls(
            step_index=_int(data.get("step_index"), "alert record.step_index", minimum=0),
            step_id=_string(data.get("step_id"), "alert record.step_id"),
            state=_string(data.get("state"), "alert record.state"),
            reason=_string(data.get("reason"), "alert record.reason"),
            evaluated_start_minute=_int(
                data.get("evaluated_start_minute"), "alert record.evaluated_start_minute"
            ),
            evaluated_start_iso=_string(
                data.get("evaluated_start_iso"), "alert record.evaluated_start_iso"
            ),
            long_window_id=_string(data.get("long_window_id"), "alert record.long_window_id"),
            short_window_id=_string(data.get("short_window_id"), "alert record.short_window_id"),
            long_burn_rate=FixedPoint.from_dict(
                _mapping(data.get("long_burn_rate"), "alert record.long_burn_rate"),
                context="alert record.long_burn_rate",
            ),
            short_burn_rate=FixedPoint.from_dict(
                _mapping(data.get("short_burn_rate"), "alert record.short_burn_rate"),
                context="alert record.short_burn_rate",
            ),
            triggered_window_ids=tuple(triggered),
            slo_version=_string(data.get("slo_version"), "alert record.slo_version"),
        )


@dataclass(frozen=True, slots=True)
class AlertTransition:
    step_index: int
    step_id: str
    from_state: str
    to_state: str
    reason: str
    trigger_window_ids: tuple[str, ...]
    long_burn_rate: FixedPoint
    short_burn_rate: FixedPoint
    slo_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "step_id": self.step_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
            "trigger_window_ids": list(self.trigger_window_ids),
            "long_burn_rate": self.long_burn_rate.to_dict(),
            "short_burn_rate": self.short_burn_rate.to_dict(),
            "slo_version": self.slo_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AlertTransition":
        data = _mapping(payload, "alert transition")
        triggered = data.get("trigger_window_ids")
        if not isinstance(triggered, list) or not all(isinstance(item, str) for item in triggered):
            raise SnapshotError("alert transition.trigger_window_ids must be a string list")
        return cls(
            step_index=_int(data.get("step_index"), "alert transition.step_index", minimum=0),
            step_id=_string(data.get("step_id"), "alert transition.step_id"),
            from_state=_string(data.get("from_state"), "alert transition.from_state"),
            to_state=_string(data.get("to_state"), "alert transition.to_state"),
            reason=_string(data.get("reason"), "alert transition.reason"),
            trigger_window_ids=tuple(triggered),
            long_burn_rate=FixedPoint.from_dict(
                _mapping(data.get("long_burn_rate"), "alert transition.long_burn_rate"),
                context="alert transition.long_burn_rate",
            ),
            short_burn_rate=FixedPoint.from_dict(
                _mapping(data.get("short_burn_rate"), "alert transition.short_burn_rate"),
                context="alert transition.short_burn_rate",
            ),
            slo_version=_string(data.get("slo_version"), "alert transition.slo_version"),
        )


@dataclass(frozen=True, slots=True)
class EvaluationStep:
    step_index: int
    step_id: str
    start_minute: int
    start_iso: str
    long_window_id: str
    short_window_id: str
    state: str
    long_burn_rate: FixedPoint
    short_burn_rate: FixedPoint
    slo_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "step_id": self.step_id,
            "start_minute": self.start_minute,
            "start_iso": self.start_iso,
            "long_window_id": self.long_window_id,
            "short_window_id": self.short_window_id,
            "state": self.state,
            "long_burn_rate": self.long_burn_rate.to_dict(),
            "short_burn_rate": self.short_burn_rate.to_dict(),
            "slo_version": self.slo_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluationStep":
        data = _mapping(payload, "step")
        return cls(
            step_index=_int(data.get("step_index"), "step.step_index", minimum=0),
            step_id=_string(data.get("step_id"), "step.step_id"),
            start_minute=_int(data.get("start_minute"), "step.start_minute"),
            start_iso=_string(data.get("start_iso"), "step.start_iso"),
            long_window_id=_string(data.get("long_window_id"), "step.long_window_id"),
            short_window_id=_string(data.get("short_window_id"), "step.short_window_id"),
            state=_string(data.get("state"), "step.state"),
            long_burn_rate=FixedPoint.from_dict(
                _mapping(data.get("long_burn_rate"), "step.long_burn_rate"), context="step.long_burn_rate"
            ),
            short_burn_rate=FixedPoint.from_dict(
                _mapping(data.get("short_burn_rate"), "step.short_burn_rate"),
                context="step.short_burn_rate",
            ),
            slo_version=_string(data.get("slo_version"), "step.slo_version"),
        )


@dataclass(frozen=True, slots=True)
class ResourceReport:
    adaptive_path: str
    auto_row_threshold: int
    row_count: int
    peak_memory_estimate_bytes: int
    peak_memory_observed_bytes: int
    elapsed_ms: int
    elapsed_us: int
    memory_accounting: str
    estimator: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "adaptive_path": self.adaptive_path,
            "auto_row_threshold": self.auto_row_threshold,
            "row_count": self.row_count,
            "peak_memory_estimate_bytes": self.peak_memory_estimate_bytes,
            "peak_memory_observed_bytes": self.peak_memory_observed_bytes,
            "elapsed_ms": self.elapsed_ms,
            "elapsed_us": self.elapsed_us,
            "memory_accounting": self.memory_accounting,
            "estimator": self.estimator,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResourceReport":
        data = _mapping(payload, "resource report")
        return cls(
            adaptive_path=_string(data.get("adaptive_path"), "resource.adaptive_path"),
            auto_row_threshold=_int(
                data.get("auto_row_threshold"), "resource.auto_row_threshold", minimum=0
            ),
            row_count=_int(data.get("row_count"), "resource.row_count", minimum=0),
            peak_memory_estimate_bytes=_int(
                data.get("peak_memory_estimate_bytes"), "resource.peak_memory_estimate_bytes", minimum=0
            ),
            peak_memory_observed_bytes=_int(
                data.get("peak_memory_observed_bytes"), "resource.peak_memory_observed_bytes", minimum=0
            ),
            elapsed_ms=_int(data.get("elapsed_ms"), "resource.elapsed_ms", minimum=0),
            elapsed_us=_int(data.get("elapsed_us"), "resource.elapsed_us", minimum=0),
            memory_accounting=_string(data.get("memory_accounting"), "resource.memory_accounting"),
            estimator=_string(data.get("estimator"), "resource.estimator"),
        )


@dataclass(frozen=True, slots=True)
class EvaluationSession:
    config: EvaluationConfig
    data_version: str
    data_sha256: str
    source_path: str
    row_count: int
    first_minute: int | None
    last_minute: int | None
    long_windows: tuple[WindowResult, ...]
    short_windows: tuple[WindowResult, ...]
    steps: tuple[EvaluationStep, ...]
    alert_records: tuple[AlertRecord, ...]
    alert_transitions: tuple[AlertTransition, ...]
    final_alert_state: str
    resource: ResourceReport

    @property
    def window_count(self) -> int:
        return len(self.long_windows) + len(self.short_windows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_version": self.data_version,
            "data_sha256": self.data_sha256,
            "source_path": self.source_path,
            "row_count": self.row_count,
            "first_minute": self.first_minute,
            "last_minute": self.last_minute,
            "windows": {
                "long": [window.to_dict() for window in self.long_windows],
                "short": [window.to_dict() for window in self.short_windows],
            },
            "steps": [step.to_dict() for step in self.steps],
            "alerts": {
                "history": [record.to_dict() for record in self.alert_records],
                "transitions": [transition.to_dict() for transition in self.alert_transitions],
                "final_state": self.final_alert_state,
            },
            "resource": self.resource.to_dict(),
        }


def _mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotError(f"{context} must be an object")
    return dict(value)


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotError(f"{context} must be a non-empty string")
    return value


def _int(value: Any, context: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotError(f"{context} must be an integer")
    if minimum is not None and value < minimum:
        raise SnapshotError(f"{context} must be >= {minimum}")
    return value

