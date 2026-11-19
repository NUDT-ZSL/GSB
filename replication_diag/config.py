"""Configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from .canonical import parse_fraction
from .errors import DiagnosticsError
from .util import read_json, resolve_path

ROUNDING_MODES = {"half_up", "half_even", "toward_zero", "away_from_zero", "floor", "ceil"}


@dataclass(frozen=True)
class Config:
    path: Path
    root: Path
    v1_path: Path
    v2_manifest: Path | None
    logs_path: Path
    v1_base_clock_ms: int
    v1_clock_step_ms: int
    v1_scale: Fraction
    v1_rounding: str
    window_size: int
    window_stride: int
    tolerance: Fraction
    observation_error: Fraction
    v1_observation_error: Fraction
    require_contiguous: bool
    strict_clock_order: bool

    @property
    def v1_scale_num(self) -> int:
        return self.v1_scale.numerator

    @property
    def v1_scale_den(self) -> int:
        return self.v1_scale.denominator

    def as_dict(self) -> dict[str, Any]:
        return {
            "v1_base_clock_ms": self.v1_base_clock_ms,
            "v1_clock_step_ms": self.v1_clock_step_ms,
            "v1_scale": f"{self.v1_scale.numerator}/{self.v1_scale.denominator}",
            "v1_rounding": self.v1_rounding,
            "window_size": self.window_size,
            "window_stride": self.window_stride,
            "tolerance": f"{self.tolerance.numerator}/{self.tolerance.denominator}",
            "observation_error": f"{self.observation_error.numerator}/{self.observation_error.denominator}",
            "v1_observation_error": f"{self.v1_observation_error.numerator}/{self.v1_observation_error.denominator}",
            "require_contiguous": self.require_contiguous,
            "strict_clock_order": self.strict_clock_order,
        }


def _int_field(data: dict[str, Any], key: str, default: int, *, minimum: int = 0) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DiagnosticsError(
            "E_CONFIG", location=key, details={"value": repr(value), "minimum": minimum}
        )
    return value


def _bool_field(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise DiagnosticsError("E_CONFIG", location=key, details={"value": repr(value)})
    return value


def load_config(path: str | Path) -> Config:
    config_path = Path(path).resolve()
    data = read_json(config_path)
    if not isinstance(data, dict):
        raise DiagnosticsError("E_CONFIG", location=str(config_path), details={"reason": "object required"})

    base = config_path.parent
    root_value = data.get("root", ".")
    if not isinstance(root_value, str):
        raise DiagnosticsError("E_CONFIG", location="root", details={"value": repr(root_value)})
    root = resolve_path(base, root_value)

    def path_field(key: str, required: bool = True) -> Path | None:
        value = data.get(key)
        if value is None:
            if required:
                raise DiagnosticsError("E_CONFIG", location=key, details={"reason": "required"})
            return None
        if not isinstance(value, str) or not value:
            raise DiagnosticsError("E_CONFIG", location=key, details={"value": repr(value)})
        return resolve_path(root, value)

    v1_path = path_field("v1_path")
    v2_manifest = path_field("v2_manifest", required=False)
    logs_path = path_field("logs_path")
    assert v1_path is not None and logs_path is not None

    scale_value = data.get("v1_scale", "1/1")
    if isinstance(scale_value, dict):
        num = scale_value.get("numerator")
        den = scale_value.get("denominator")
        if (
            isinstance(num, bool)
            or not isinstance(num, int)
            or isinstance(den, bool)
            or not isinstance(den, int)
            or num <= 0
            or den <= 0
        ):
            raise DiagnosticsError("E_CONFIG", location="v1_scale")
        v1_scale = Fraction(num, den)
    else:
        v1_scale = parse_fraction(scale_value, location="v1_scale")
        if v1_scale <= 0:
            raise DiagnosticsError("E_CONFIG", location="v1_scale", details={"reason": "positive required"})

    rounding = data.get("v1_rounding", "half_up")
    if rounding not in ROUNDING_MODES:
        raise DiagnosticsError(
            "E_CONFIG", location="v1_rounding", details={"value": rounding, "allowed": sorted(ROUNDING_MODES)}
        )

    return Config(
        path=config_path,
        root=root,
        v1_path=v1_path,
        v2_manifest=v2_manifest,
        logs_path=logs_path,
        v1_base_clock_ms=_int_field(data, "v1_base_clock_ms", 0),
        v1_clock_step_ms=_int_field(data, "v1_clock_step_ms", 1),
        v1_scale=v1_scale,
        v1_rounding=rounding,
        window_size=_int_field(data, "window_size", 3, minimum=1),
        window_stride=_int_field(data, "window_stride", 1, minimum=1),
        tolerance=parse_fraction(data.get("tolerance_ms", 0), location="tolerance_ms"),
        observation_error=parse_fraction(
            data.get("observation_error_ms", 1), location="observation_error_ms"
        ),
        v1_observation_error=parse_fraction(
            data.get("v1_observation_error_ms", 0), location="v1_observation_error_ms"
        ),
        require_contiguous=_bool_field(data, "require_contiguous", True),
        strict_clock_order=_bool_field(data, "strict_clock_order", True),
    )
