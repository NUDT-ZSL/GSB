"""Configuration loading and validation for the evaluation engine."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError, InputError


def _as_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a JSON object")
    return dict(value)


def _as_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _as_int(value: Any, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer, not boolean")
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise ConfigError(f"{name} must be an integer")
        value = int(value)
    if not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _ratio_to_ppm(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be an exact ratio, not boolean")
    if isinstance(value, int):
        fraction = Fraction(value, 1)
    elif isinstance(value, Decimal):
        fraction = Fraction(value)
    elif isinstance(value, str):
        try:
            fraction = Fraction(Decimal(value))
        except Exception as exc:  # Decimal raises several concrete subclasses
            raise ConfigError(f"{name} must be a decimal ratio") from exc
    else:
        raise ConfigError(f"{name} must be a string/Decimal ratio or target_ppm integer")
    scaled = fraction * 1_000_000
    if scaled.denominator != 1:
        raise ConfigError(f"{name} must be representable exactly as integer ppm")
    return int(scaled)


@dataclass(frozen=True, slots=True)
class SLISpec:
    id: str
    service: str
    timestamp_field: str = "minute"
    total_requests_field: str = "requests"
    failed_requests_field: str = "errors"
    timestamp_unit: str = "minute"

    def __post_init__(self) -> None:
        for name in ("id", "service", "timestamp_field", "total_requests_field", "failed_requests_field"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ConfigError(f"sli.{name} must be a non-empty string")
        if self.timestamp_unit != "minute":
            raise ConfigError("sli.timestamp_unit must be 'minute'")
        fields = {self.timestamp_field, self.total_requests_field, self.failed_requests_field}
        if len(fields) != 3:
            raise ConfigError("sli field names must be distinct")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "service": self.service,
            "timestamp_field": self.timestamp_field,
            "total_requests_field": self.total_requests_field,
            "failed_requests_field": self.failed_requests_field,
            "timestamp_unit": self.timestamp_unit,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SLISpec":
        data = _as_mapping(payload, "sli")
        return cls(
            id=_as_string(data.get("id"), "sli.id"),
            service=_as_string(data.get("service"), "sli.service"),
            timestamp_field=_as_string(data.get("timestamp_field", "minute"), "sli.timestamp_field"),
            total_requests_field=_as_string(
                data.get("total_requests_field", "requests"), "sli.total_requests_field"
            ),
            failed_requests_field=_as_string(
                data.get("failed_requests_field", "errors"), "sli.failed_requests_field"
            ),
            timestamp_unit=_as_string(data.get("timestamp_unit", "minute"), "sli.timestamp_unit"),
        )


@dataclass(frozen=True, slots=True)
class SLOSpec:
    id: str
    version: str
    target_ppm: int

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ConfigError("slo.id must be a non-empty string")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ConfigError("slo.version must be a non-empty string")
        if isinstance(self.target_ppm, bool) or not isinstance(self.target_ppm, int):
            raise ConfigError("slo.target_ppm must be an integer")
        if not 0 < self.target_ppm < 1_000_000:
            raise ConfigError("slo.target_ppm must satisfy 0 < target_ppm < 1000000")

    @property
    def budget_ppm(self) -> int:
        return 1_000_000 - self.target_ppm

    @property
    def target_fraction(self) -> Fraction:
        return Fraction(self.target_ppm, 1_000_000)

    @property
    def budget_fraction(self) -> Fraction:
        return Fraction(self.budget_ppm, 1_000_000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "target_ppm": self.target_ppm,
            "target_scale": 1_000_000,
            "target_error_upper_ppm": 0,
            "budget_ppm": self.budget_ppm,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SLOSpec":
        data = _as_mapping(payload, "slo")
        if "target_ppm" in data:
            target_ppm = _as_int(data["target_ppm"], "slo.target_ppm")
        else:
            ratio_key = "target_ratio" if "target_ratio" in data else "target_availability"
            if ratio_key not in data:
                raise ConfigError("slo must contain target_ppm or target_ratio")
            target_ppm = _ratio_to_ppm(data[ratio_key], f"slo.{ratio_key}")
        return cls(
            id=_as_string(data.get("id"), "slo.id"),
            version=_as_string(data.get("version"), "slo.version"),
            target_ppm=target_ppm,
        )


@dataclass(frozen=True, slots=True)
class WindowSpec:
    long_minutes: int
    short_minutes: int
    step_minutes: int

    def __post_init__(self) -> None:
        for name in ("long_minutes", "short_minutes", "step_minutes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ConfigError(f"windows.{name} must be a positive integer")
        if self.long_minutes < self.short_minutes:
            raise ConfigError("windows.long_minutes must be >= windows.short_minutes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "long_minutes": self.long_minutes,
            "short_minutes": self.short_minutes,
            "step_minutes": self.step_minutes,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WindowSpec":
        data = _as_mapping(payload, "windows")
        return cls(
            long_minutes=_as_int(data.get("long_minutes"), "windows.long_minutes", minimum=1),
            short_minutes=_as_int(data.get("short_minutes"), "windows.short_minutes", minimum=1),
            step_minutes=_as_int(data.get("step_minutes"), "windows.step_minutes", minimum=1),
        )


@dataclass(frozen=True, slots=True)
class AlertPolicy:
    warning_long_ppm: int
    warning_short_ppm: int
    critical_long_ppm: int
    critical_short_ppm: int

    def __post_init__(self) -> None:
        for name in (
            "warning_long_ppm",
            "warning_short_ppm",
            "critical_long_ppm",
            "critical_short_ppm",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ConfigError(f"alert_policy.{name} must be a non-negative integer")
        if self.warning_long_ppm > self.critical_long_ppm:
            raise ConfigError("warning long threshold cannot exceed critical long threshold")
        if self.warning_short_ppm > self.critical_short_ppm:
            raise ConfigError("warning short threshold cannot exceed critical short threshold")

    @property
    def warning_long(self) -> Fraction:
        return Fraction(self.warning_long_ppm, 1_000_000)

    @property
    def warning_short(self) -> Fraction:
        return Fraction(self.warning_short_ppm, 1_000_000)

    @property
    def critical_long(self) -> Fraction:
        return Fraction(self.critical_long_ppm, 1_000_000)

    @property
    def critical_short(self) -> Fraction:
        return Fraction(self.critical_short_ppm, 1_000_000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "warning_long_ppm": self.warning_long_ppm,
            "warning_short_ppm": self.warning_short_ppm,
            "critical_long_ppm": self.critical_long_ppm,
            "critical_short_ppm": self.critical_short_ppm,
            "scale": 1_000_000,
            "error_upper_ppm": 0,
            "combination": "long_and_short",
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AlertPolicy":
        data = _as_mapping(payload, "alert_policy")
        if all(key in data for key in (
            "warning_long_ppm",
            "warning_short_ppm",
            "critical_long_ppm",
            "critical_short_ppm",
        )):
            values = data
        else:
            warning = _as_mapping(data.get("warning", {}), "alert_policy.warning")
            critical = _as_mapping(data.get("critical", {}), "alert_policy.critical")
            values = {
                "warning_long_ppm": warning.get("long_ppm"),
                "warning_short_ppm": warning.get("short_ppm"),
                "critical_long_ppm": critical.get("long_ppm"),
                "critical_short_ppm": critical.get("short_ppm"),
            }
        return cls(
            warning_long_ppm=_as_int(values["warning_long_ppm"], "alert_policy.warning_long_ppm"),
            warning_short_ppm=_as_int(values["warning_short_ppm"], "alert_policy.warning_short_ppm"),
            critical_long_ppm=_as_int(values["critical_long_ppm"], "alert_policy.critical_long_ppm"),
            critical_short_ppm=_as_int(values["critical_short_ppm"], "alert_policy.critical_short_ppm"),
        )


@dataclass(frozen=True, slots=True)
class PrecisionPolicy:
    ratio_scale: int = 1_000_000_000
    burn_scale: int = 1_000_000
    request_scale: int = 1_000_000

    def __post_init__(self) -> None:
        from .fixed import _require_power_of_ten

        _require_power_of_ten(self.ratio_scale, "precision.ratio_scale")
        _require_power_of_ten(self.burn_scale, "precision.burn_scale")
        _require_power_of_ten(self.request_scale, "precision.request_scale")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ratio_scale": self.ratio_scale,
            "burn_scale": self.burn_scale,
            "request_scale": self.request_scale,
            "rounding": "half_even",
            "error_bound": "ceil_abs_ticks",
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PrecisionPolicy":
        data = _as_mapping(payload, "precision")
        return cls(
            ratio_scale=_as_int(data.get("ratio_scale", 1_000_000_000), "precision.ratio_scale", minimum=1),
            burn_scale=_as_int(data.get("burn_scale", 1_000_000), "precision.burn_scale", minimum=1),
            request_scale=_as_int(
                data.get("request_scale", 1_000_000), "precision.request_scale", minimum=1
            ),
        )


@dataclass(frozen=True, slots=True)
class AdaptivePolicy:
    row_threshold: int = 50_000

    def __post_init__(self) -> None:
        if isinstance(self.row_threshold, bool) or not isinstance(self.row_threshold, int):
            raise ConfigError("adaptive.row_threshold must be an integer")
        if self.row_threshold < 0:
            raise ConfigError("adaptive.row_threshold must be >= 0")

    def to_dict(self) -> dict[str, Any]:
        return {"row_threshold": self.row_threshold}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AdaptivePolicy":
        data = _as_mapping(payload, "adaptive")
        return cls(row_threshold=_as_int(data.get("row_threshold", 50_000), "adaptive.row_threshold", minimum=0))


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    sli: SLISpec
    slo: SLOSpec
    windows: WindowSpec
    alert_policy: AlertPolicy
    precision: PrecisionPolicy = PrecisionPolicy()
    adaptive: AdaptivePolicy = AdaptivePolicy()
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sli": self.sli.to_dict(),
            "slo": self.slo.to_dict(),
            "windows": self.windows.to_dict(),
            "alert_policy": self.alert_policy.to_dict(),
            "precision": self.precision.to_dict(),
            "adaptive": self.adaptive.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluationConfig":
        data = _as_mapping(payload, "config")
        schema_version = _as_int(data.get("schema_version", 1), "schema_version", minimum=1)
        if schema_version != 1:
            raise ConfigError(f"unsupported config schema_version: {schema_version}")
        if "alert_policy" not in data and "thresholds" in data:
            data = dict(data)
            data["alert_policy"] = data["thresholds"]
        return cls(
            sli=SLISpec.from_dict(data.get("sli", {})),
            slo=SLOSpec.from_dict(data.get("slo", {})),
            windows=WindowSpec.from_dict(data.get("windows", {})),
            alert_policy=AlertPolicy.from_dict(data.get("alert_policy", {})),
            precision=PrecisionPolicy.from_dict(data.get("precision", {})),
            adaptive=AdaptivePolicy.from_dict(data.get("adaptive", {})),
            schema_version=schema_version,
        )


def load_config(path: str | Path) -> EvaluationConfig:
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read config file: {config_path}", details={"path": str(config_path)}) from exc
    try:
        payload = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"invalid JSON config: {exc.msg}",
            details={"line": exc.lineno, "column": exc.colno, "path": str(config_path)},
        ) from exc
    return EvaluationConfig.from_dict(payload)

