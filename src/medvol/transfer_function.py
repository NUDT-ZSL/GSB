"""Validated piecewise-linear transfer functions and pre-built LUTs."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .errors import BusinessCode, VolumeRenderError


def _tf_error(message: str, **details: Any) -> VolumeRenderError:
    return VolumeRenderError(BusinessCode.E_TF_RANGE, message, details=details)


def _finite_float(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise _tf_error(f"{field} must be numeric", field=field, value=repr(value)) from exc
    if not math.isfinite(result):
        raise _tf_error(f"{field} must be finite", field=field, value=repr(value))
    return result


def _rgba(value: Iterable[Any], field: str) -> tuple[float, float, float, float]:
    try:
        components = tuple(_finite_float(v, field) for v in value)
    except TypeError as exc:
        raise _tf_error(f"{field} must be an array of four numbers", field=field) from exc
    if len(components) != 4:
        raise _tf_error(f"{field} must have four components", field=field)
    if any(component < 0.0 or component > 1.0 for component in components):
        raise _tf_error(f"{field} components must be in [0, 1]", field=field, value=list(components))
    return components


@dataclass(frozen=True)
class TransferControlPoint:
    value: float
    color: tuple[float, float, float]
    opacity: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "color": list(self.color),
            "opacity": self.opacity,
        }


@dataclass(frozen=True)
class TransferFunctionLUT:
    rgba: np.ndarray
    gradient_l1: np.ndarray
    gradient_norm: np.ndarray
    values: np.ndarray
    max_gradient_norm: float
    value_range: tuple[float, float]

    def __post_init__(self) -> None:
        for array in (self.rgba, self.gradient_l1, self.gradient_norm, self.values):
            array.setflags(write=False)

    def lookup_indices(self, scalar_values: np.ndarray | float) -> np.ndarray:
        values = np.asarray(scalar_values, dtype=np.float64)
        low, high = self.value_range
        scale = (self.rgba.shape[0] - 1) / (high - low)
        indices = np.floor((values - low) * scale + 0.5).astype(np.int64)
        return np.clip(indices, 0, self.rgba.shape[0] - 1)

    def lookup(self, scalar_values: np.ndarray | float) -> np.ndarray:
        return self.rgba[self.lookup_indices(scalar_values)]


@dataclass(frozen=True)
class TransferFunction:
    value_range: tuple[float, float]
    control_points: tuple[TransferControlPoint, ...]
    opacity_curve: tuple[tuple[float, float], ...] = ((0.0, 0.0), (1.0, 1.0))
    opacity_gamma: float = 1.0
    lut_size: int = 4096

    def __post_init__(self) -> None:
        try:
            low, high = (_finite_float(v, "value_range") for v in self.value_range)
        except (TypeError, ValueError) as exc:
            raise _tf_error("value_range must contain exactly two numbers") from exc
        if not low < high:
            raise _tf_error("value_range minimum must be smaller than maximum", value_range=[low, high])
        if len(self.control_points) < 2:
            raise _tf_error("Transfer function requires at least two control points")

        previous_value = -math.inf
        for index, point in enumerate(self.control_points):
            value = _finite_float(point.value, f"control_points[{index}].value")
            if value <= previous_value:
                raise _tf_error(
                    "Transfer function control values must be strictly increasing",
                    control_point_index=index,
                    previous_value=previous_value,
                    value=value,
                )
            if value < low or value > high:
                raise _tf_error(
                    "Transfer function control value is outside the configured input range",
                    control_point_index=index,
                    value=value,
                    value_range=[low, high],
                )
            _rgba((*point.color, point.opacity), f"control_points[{index}]")
            previous_value = value

        try:
            curve = tuple(
                (_finite_float(a, "opacity_curve"), _finite_float(b, "opacity_curve"))
                for a, b in self.opacity_curve
            )
        except (TypeError, ValueError) as exc:
            raise _tf_error("opacity_curve entries must be [alpha, corrected] pairs") from exc
        if len(curve) < 2:
            raise _tf_error("opacity_curve requires at least two control points")
        previous_alpha = -math.inf
        for index, (alpha, corrected) in enumerate(curve):
            if alpha <= previous_alpha or alpha < 0.0 or alpha > 1.0:
                raise _tf_error(
                    "opacity_curve alpha values must be strictly increasing in [0, 1]",
                    control_point_index=index,
                    alpha=alpha,
                )
            if corrected < 0.0 or corrected > 1.0:
                raise _tf_error(
                    "opacity_curve corrected values must be in [0, 1]",
                    control_point_index=index,
                    corrected=corrected,
                )
            previous_alpha = alpha
        if curve[0][0] != 0.0 or curve[-1][0] != 1.0:
            raise _tf_error("opacity_curve must include alpha endpoints 0 and 1", opacity_curve=[list(p) for p in curve])

        gamma = _finite_float(self.opacity_gamma, "opacity_gamma")
        if gamma <= 0.0:
            raise _tf_error("opacity_gamma must be positive", opacity_gamma=gamma)
        try:
            lut_size = int(self.lut_size)
        except (TypeError, ValueError) as exc:
            raise _tf_error("lut_size must be an integer", lut_size=repr(self.lut_size)) from exc
        if lut_size < 16 or lut_size > 1 << 20:
            raise _tf_error("lut_size must be between 16 and 1048576", lut_size=self.lut_size)

        object.__setattr__(self, "value_range", (low, high))
        object.__setattr__(self, "opacity_gamma", gamma)
        object.__setattr__(self, "lut_size", lut_size)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TransferFunction":
        if not isinstance(payload, dict):
            raise _tf_error("Transfer function JSON root must be an object")
        try:
            value_range = payload["value_range"]
            points_payload = payload["control_points"]
        except KeyError as exc:
            raise _tf_error("Transfer function is missing a required field", missing=exc.args[0]) from exc
        if not isinstance(value_range, (list, tuple)) or len(value_range) != 2:
            raise _tf_error("value_range must be an array of two numbers", value_range=repr(value_range))
        if not isinstance(points_payload, list):
            raise _tf_error("control_points must be an array")

        points: list[TransferControlPoint] = []
        for index, item in enumerate(points_payload):
            if not isinstance(item, dict):
                raise _tf_error("Each control point must be an object", control_point_index=index)
            try:
                value = _finite_float(item["value"], f"control_points[{index}].value")
                raw_color = item["color"]
                if not isinstance(raw_color, (list, tuple)) or len(raw_color) != 3:
                    raise _tf_error(
                        "Control point color must have three components",
                        control_point_index=index,
                    )
                color_values = tuple(_finite_float(v, f"control_points[{index}].color") for v in raw_color)
                opacity = _finite_float(item["opacity"], f"control_points[{index}].opacity")
            except KeyError as exc:
                raise _tf_error(
                    "Control point is missing a required field",
                    control_point_index=index,
                    missing=exc.args[0],
                ) from exc
            if len(color_values) != 3:
                raise _tf_error("Control point color must have three components", control_point_index=index)
            if any(component < 0.0 or component > 1.0 for component in color_values):
                raise _tf_error("Control point colors must be in [0, 1]", control_point_index=index)
            if opacity < 0.0 or opacity > 1.0:
                raise _tf_error("Control point opacity must be in [0, 1]", control_point_index=index)
            points.append(TransferControlPoint(value, color_values, opacity))  # type: ignore[arg-type]

        opacity_curve_payload = payload.get("opacity_curve", [[0.0, 0.0], [1.0, 1.0]])
        if not isinstance(opacity_curve_payload, list):
            raise _tf_error("opacity_curve must be an array")
        opacity_curve: list[tuple[float, float]] = []
        for index, item in enumerate(opacity_curve_payload):
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise _tf_error("Each opacity_curve entry must be [alpha, corrected]", control_point_index=index)
            opacity_curve.append((_finite_float(item[0], "opacity_curve.alpha"), _finite_float(item[1], "opacity_curve.corrected")))

        try:
            lut_size = int(payload.get("lut_size", 4096))
        except (TypeError, ValueError) as exc:
            raise _tf_error("lut_size must be an integer", value=repr(payload.get("lut_size"))) from exc
        return cls(
            value_range=(_finite_float(value_range[0], "value_range"), _finite_float(value_range[1], "value_range")),
            control_points=tuple(points),
            opacity_curve=tuple(opacity_curve),
            opacity_gamma=_finite_float(payload.get("opacity_gamma", 1.0), "opacity_gamma"),
            lut_size=lut_size,
        )

    @classmethod
    def from_json(cls, text: str) -> "TransferFunction":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _tf_error("Transfer function JSON is invalid", error=str(exc)) from exc
        return cls.from_dict(payload)

    @classmethod
    def from_file(cls, path: str | Path) -> "TransferFunction":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "value_range": [float(self.value_range[0]), float(self.value_range[1])],
            "control_points": [point.to_dict() for point in self.control_points],
            "opacity_curve": [[float(a), float(b)] for a, b in self.opacity_curve],
            "opacity_gamma": float(self.opacity_gamma),
            "lut_size": int(self.lut_size),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)

    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def build_lut(self) -> TransferFunctionLUT:
        low, high = self.value_range
        values = np.linspace(low, high, self.lut_size, dtype=np.float64)
        point_values = np.asarray([point.value for point in self.control_points], dtype=np.float64)

        rgb = np.empty((self.lut_size, 3), dtype=np.float64)
        alpha = np.empty(self.lut_size, dtype=np.float64)
        for channel in range(3):
            rgb[:, channel] = np.interp(
                values,
                point_values,
                np.asarray([point.color[channel] for point in self.control_points], dtype=np.float64),
            )
        alpha[:] = np.interp(
            values,
            point_values,
            np.asarray([point.opacity for point in self.control_points], dtype=np.float64),
        )

        curve_alpha = np.asarray([point[0] for point in self.opacity_curve], dtype=np.float64)
        curve_corrected = np.asarray([point[1] for point in self.opacity_curve], dtype=np.float64)
        alpha = np.interp(alpha, curve_alpha, curve_corrected)
        if self.opacity_gamma != 1.0:
            alpha = 1.0 - np.power(1.0 - alpha, self.opacity_gamma)
        alpha = np.clip(alpha, 0.0, 1.0)

        rgba = np.concatenate((rgb, alpha[:, None]), axis=1).astype(np.float32)
        step = (high - low) / max(self.lut_size - 1, 1)
        gradient = np.gradient(rgba.astype(np.float64), step, axis=0)
        gradient_l1 = np.sum(np.abs(gradient), axis=1).astype(np.float32)
        gradient_norm = np.linalg.norm(gradient, axis=1).astype(np.float32)
        max_gradient_norm = float(np.max(gradient_norm)) if self.lut_size else 0.0
        return TransferFunctionLUT(
            rgba=rgba,
            gradient_l1=gradient_l1,
            gradient_norm=gradient_norm,
            values=values,
            max_gradient_norm=max_gradient_norm,
            value_range=self.value_range,
        )

    def lookup(self, scalar_values: np.ndarray | float) -> np.ndarray:
        return self.build_lut().lookup(scalar_values)

    def metadata(self) -> dict[str, Any]:
        return {
            "value_range": list(self.value_range),
            "control_point_count": len(self.control_points),
            "lut_size": self.lut_size,
            "opacity_gamma": self.opacity_gamma,
            "sha256": self.sha256(),
        }


def demo_transfer_function(value_range: tuple[float, float] = (0.0, 4095.0)) -> TransferFunction:
    """A CT-like transfer function with air, soft tissue, and bone ramps."""

    low, high = value_range
    span = high - low

    def at(fraction: float) -> float:
        return low + fraction * span

    return TransferFunction(
        value_range=value_range,
        control_points=(
            TransferControlPoint(at(0.00), (0.0, 0.0, 0.0), 0.0),
            TransferControlPoint(at(0.10), (0.0, 0.0, 0.0), 0.0),
            TransferControlPoint(at(0.28), (0.45, 0.08, 0.03), 0.015),
            TransferControlPoint(at(0.48), (0.92, 0.34, 0.12), 0.075),
            TransferControlPoint(at(0.63), (0.95, 0.72, 0.42), 0.16),
            TransferControlPoint(at(0.82), (0.98, 0.94, 0.82), 0.42),
            TransferControlPoint(at(1.00), (1.0, 1.0, 1.0), 0.70),
        ),
        opacity_curve=((0.0, 0.0), (0.25, 0.18), (0.5, 0.46), (0.75, 0.75), (1.0, 1.0)),
        opacity_gamma=1.0,
        lut_size=4096,
    )
