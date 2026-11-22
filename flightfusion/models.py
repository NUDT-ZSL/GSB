"""Domain data model for descriptions, datasets and fusion configuration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any

from .calibration import axis_mapping, infer_sensor_type, resolve_calibration
from .errors import BusinessError, ErrorCode
from .serialization import content_hash


TRIPLE = tuple[float, float, float]
MATRIX3 = tuple[TRIPLE, TRIPLE, TRIPLE]


def _triple(value: Any, field_name: str, sensor_id: str | None = None) -> TRIPLE:
    try:
        values = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise BusinessError(
            code=ErrorCode.FORMAT_CORRUPT,
            message=f"{field_name} must contain three numeric values",
            stage="parse",
            sensor_id=sensor_id,
            field=field_name,
            value=value,
        ) from exc
    if len(values) != 3:
        raise BusinessError(
            code=ErrorCode.FORMAT_CORRUPT,
            message=f"{field_name} must contain exactly three values",
            stage="parse",
            sensor_id=sensor_id,
            field=field_name,
            value=value,
        )
    if not all(math.isfinite(item) for item in values):
        raise BusinessError(
            code=ErrorCode.FORMAT_CORRUPT,
            message=f"{field_name} must contain finite values",
            stage="parse",
            sensor_id=sensor_id,
            field=field_name,
            value=value,
        )
    return values  # type: ignore[return-value]


def _matrix3(value: Any, field_name: str, sensor_id: str | None = None) -> MATRIX3:
    try:
        rows = tuple(_triple(row, field_name, sensor_id) for row in value)
    except TypeError as exc:
        raise BusinessError(
            code=ErrorCode.FORMAT_CORRUPT,
            message=f"{field_name} must be a 3x3 numeric matrix",
            stage="parse",
            sensor_id=sensor_id,
            field=field_name,
            value=value,
        ) from exc
    if len(rows) != 3:
        raise BusinessError(
            code=ErrorCode.FORMAT_CORRUPT,
            message=f"{field_name} must contain three rows",
            stage="parse",
            sensor_id=sensor_id,
            field=field_name,
            value=value,
        )
    return rows  # type: ignore[return-value]


IDENTITY3: MATRIX3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
DEFAULT_NOISE = {
    "white_noise_std": 0.001,
    "bias_random_walk_std": 0.00001,
}
DEFAULT_TEMPERATURE = {
    "reference_c": 25.0,
    "coefficients": ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
}


@dataclass(frozen=True)
class SensorDescription:
    sensor_id: str
    sensor_type: str
    range_limit: float
    sample_rate: float
    axis_order: TRIPLE
    calibration_version: int
    format_version: int
    bias: TRIPLE
    scale: TRIPLE
    axis_coupling: MATRIX3 = IDENTITY3
    noise_model: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_NOISE))
    temperature_compensation: dict[str, Any] = field(
        default_factory=lambda: {
            "reference_c": DEFAULT_TEMPERATURE["reference_c"],
            "coefficients": DEFAULT_TEMPERATURE["coefficients"],
        }
    )
    provenance: dict[str, str] = field(default_factory=dict)
    present_fields: tuple[str, ...] = ()

    @property
    def axis_mapping(self) -> dict[str, int]:
        return axis_mapping(self.axis_order)

    @property
    def description_version(self) -> str:
        return f"v{self.format_version}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "sensor_id": self.sensor_id,
            "sensor_type": self.sensor_type,
            "range": self.range_limit,
            "sample_rate": self.sample_rate,
            "axis_order": list(self.axis_order),
            "axis_mapping": self.axis_mapping,
            "calibration_version": self.calibration_version,
            "calibration": {
                "bias": list(self.bias),
                "scale": list(self.scale),
            },
            "format_version": self.format_version,
            "description_version": self.description_version,
            "axis_coupling": [list(row) for row in self.axis_coupling],
            "noise_model": dict(self.noise_model),
            "temperature_compensation": {
                "reference_c": self.temperature_compensation.get("reference_c"),
                "coefficients": [
                    list(row)
                    for row in self.temperature_compensation.get("coefficients", ())
                ],
            },
            "provenance": dict(self.provenance),
            "present_fields": list(self.present_fields),
        }

    def content_hash(self) -> str:
        return content_hash(self.as_dict())

    def with_calibration_version(self, version: int) -> "SensorDescription":
        bias, scale = resolve_calibration(
            self.sensor_type,
            version,
            stage="preprocess",
            sensor_id=self.sensor_id,
        )
        return replace(
            self,
            calibration_version=version,
            bias=bias,
            scale=scale,
        )


def description_from_dict(
    data: dict[str, Any],
    *,
    format_version: int | None = None,
) -> SensorDescription:
    """Build a normalized descriptor from a permissive v3-shaped mapping."""

    sensor_id = str(data.get("sensor_id", "")).strip()
    if not sensor_id:
        raise BusinessError(
            code=ErrorCode.FORMAT_CORRUPT,
            message="sensor_id is required",
            stage="parse",
            field="sensor_id",
            value=data.get("sensor_id"),
        )
    sensor_type = str(data.get("sensor_type") or data.get("type") or "").strip()
    if not sensor_type:
        sensor_type = infer_sensor_type(sensor_id)
    if sensor_type not in {"gyro", "accel", "mag"}:
        raise BusinessError(
            code=ErrorCode.FORMAT_CORRUPT,
            message=f"unsupported sensor_type {sensor_type!r}",
            stage="parse",
            sensor_id=sensor_id,
            field="sensor_type",
            value=sensor_type,
        )

    try:
        range_limit = float(data["range"])
        sample_rate = float(data["sample_rate"])
        calibration_version = int(data.get("calibration_version", data.get("calibration", {}).get("version")))
    except (KeyError, TypeError, ValueError) as exc:
        field_name = "range/sample_rate/calibration_version"
        raise BusinessError(
            code=ErrorCode.FORMAT_CORRUPT,
            message=f"required numeric field is missing or invalid: {exc}",
            stage="parse",
            sensor_id=sensor_id,
            field=field_name,
        ) from exc
    if range_limit <= 0:
        raise BusinessError(
            code=ErrorCode.FORMAT_CORRUPT,
            message="range must be positive",
            stage="parse",
            sensor_id=sensor_id,
            field="range",
            value=range_limit,
        )
    try:
        axis_order = tuple(str(item).strip().lower() for item in data["axis_order"])
    except (KeyError, TypeError) as exc:
        raise BusinessError(
            code=ErrorCode.FORMAT_CORRUPT,
            message="axis_order is required",
            stage="parse",
            sensor_id=sensor_id,
            field="axis_order",
        ) from exc
    if len(axis_order) != 3 or set(axis_order) != {"x", "y", "z"}:
        raise BusinessError(
            code=ErrorCode.AXIS_MAPPING_INVALID,
            message=f"axis_order must be a permutation of x,y,z, got {axis_order!r}",
            stage="parse",
            sensor_id=sensor_id,
            field="axis_order",
            value=list(axis_order),
            degradation="fallback_identity_axis_mapping",
        )

    calibration = data.get("calibration") or {}
    if "bias" in calibration or "scale" in calibration:
        bias = _triple(calibration.get("bias"), "calibration.bias", sensor_id)
        scale = _triple(calibration.get("scale"), "calibration.scale", sensor_id)
    else:
        bias, scale = resolve_calibration(
            sensor_type,
            calibration_version,
            sensor_id=sensor_id,
            line=data.get("_line"),
        )

    coupling = data.get("axis_coupling", IDENTITY3)
    noise_raw = data.get("noise_model", DEFAULT_NOISE)
    if not isinstance(noise_raw, dict):
        raise BusinessError(
            code=ErrorCode.FORMAT_CORRUPT,
            message="noise_model must be an object",
            stage="parse",
            sensor_id=sensor_id,
            field="noise_model",
        )
    noise = {str(key): float(value) for key, value in noise_raw.items()}
    temp_raw = data.get("temperature_compensation", DEFAULT_TEMPERATURE)
    if not isinstance(temp_raw, dict):
        raise BusinessError(
            code=ErrorCode.FORMAT_CORRUPT,
            message="temperature_compensation must be an object",
            stage="parse",
            sensor_id=sensor_id,
            field="temperature_compensation",
        )
    coefficients_raw = temp_raw.get("coefficients", DEFAULT_TEMPERATURE["coefficients"])
    coefficients = _matrix3(coefficients_raw, "temperature_compensation.coefficients", sensor_id)
    temp = {
        "reference_c": float(temp_raw.get("reference_c", DEFAULT_TEMPERATURE["reference_c"])),
        "coefficients": coefficients,
    }
    present = data.get("present_fields")
    if present is None:
        present = tuple(sorted(str(key) for key in data.keys() if not key.startswith("_")))
    return SensorDescription(
        sensor_id=sensor_id,
        sensor_type=sensor_type,
        range_limit=range_limit,
        sample_rate=sample_rate,
        axis_order=axis_order,  # type: ignore[arg-type]
        calibration_version=calibration_version,
        format_version=int(format_version or data.get("format_version") or 3),
        bias=bias,
        scale=scale,
        axis_coupling=_matrix3(coupling, "axis_coupling", sensor_id),
        noise_model=noise,
        temperature_compensation=temp,
        provenance=dict(data.get("provenance") or {}),
        present_fields=tuple(present),
    )


@dataclass(frozen=True)
class FusionConfig:
    config_version: str
    complementary_gain: float
    accel_weight: float
    mag_weight: float
    gravity_constant: float
    gyro_bias_window: int
    output_rate_hz: float
    mag_reference: TRIPLE
    downgrade_policy: dict[str, str] = field(
        default_factory=lambda: {
            "axis_coupling": "diag",
            "noise_model": "drop",
            "temperature_compensation": "drop",
        }
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FusionConfig":
        try:
            return cls(
                config_version=str(data["config_version"]),
                complementary_gain=float(data["complementary_gain"]),
                accel_weight=float(data["accel_weight"]),
                mag_weight=float(data["mag_weight"]),
                gravity_constant=float(data["gravity_constant"]),
                gyro_bias_window=int(data["gyro_bias_window"]),
                output_rate_hz=float(data.get("output_rate_hz", 100.0)),
                mag_reference=_triple(data.get("mag_reference", (24.0, 0.0, 42.0)), "mag_reference"),
                downgrade_policy=dict(data.get("downgrade_policy", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BusinessError(
                code=ErrorCode.FORMAT_CORRUPT,
                message=f"fusion config is missing or invalid: {exc}",
                stage="parse",
                field="fusion_config",
            ) from exc

    def as_dict(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "complementary_gain": self.complementary_gain,
            "accel_weight": self.accel_weight,
            "mag_weight": self.mag_weight,
            "gravity_constant": self.gravity_constant,
            "gyro_bias_window": self.gyro_bias_window,
            "output_rate_hz": self.output_rate_hz,
            "mag_reference": list(self.mag_reference),
            "downgrade_policy": dict(self.downgrade_policy),
        }

    def content_hash(self) -> str:
        return content_hash(self.as_dict())


@dataclass(frozen=True)
class SensorSequence:
    sensor_id: str
    sensor_type: str
    sample_rate: float
    times: tuple[float, ...]
    values: tuple[TRIPLE, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sensor_id": self.sensor_id,
            "sensor_type": self.sensor_type,
            "sample_rate": self.sample_rate,
            "times": list(self.times),
            "values": [list(row) for row in self.values],
        }

    def content_hash(self) -> str:
        return content_hash(self.as_dict())


@dataclass(frozen=True)
class GroundTruth:
    times: tuple[float, ...]
    quaternions: tuple[tuple[float, float, float, float], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "times": list(self.times),
            "quaternions": [list(row) for row in self.quaternions],
        }

    def content_hash(self) -> str:
        return content_hash(self.as_dict())


@dataclass(frozen=True)
class FlightDataset:
    seed: int
    sequences: dict[str, SensorSequence]
    truth: GroundTruth

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FlightDataset":
        sequences: dict[str, SensorSequence] = {}
        for sensor_id, raw in data["sequences"].items():
            values = tuple(
                (float(row[0]), float(row[1]), float(row[2])) for row in raw["values"]
            )
            sequences[sensor_id] = SensorSequence(
                sensor_id=sensor_id,
                sensor_type=str(raw["sensor_type"]),
                sample_rate=float(raw["sample_rate"]),
                times=tuple(float(item) for item in raw["times"]),
                values=values,
            )
        truth_raw = data["truth"]
        truth = GroundTruth(
            times=tuple(float(item) for item in truth_raw["times"]),
            quaternions=tuple(
                (float(row[0]), float(row[1]), float(row[2]), float(row[3]))
                for row in truth_raw["quaternions"]
            ),
        )
        return cls(seed=int(data.get("seed", 0)), sequences=sequences, truth=truth)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "sequences": {
                key: sequence.as_dict() for key, sequence in sorted(self.sequences.items())
            },
            "truth": self.truth.as_dict(),
        }

    def content_hash(self) -> str:
        return content_hash(self.as_dict())


@dataclass(frozen=True)
class FlightState:
    dataset: FlightDataset
    descriptions: dict[str, SensorDescription]
    fusion_config: FusionConfig
    description_views: dict[str, str]
    active_formats: dict[str, int]
    snapshot_version: int = 0
    snapshot_checksum: str = ""

    def snapshot_payload(self) -> dict[str, Any]:
        return {
            "dataset_digest": self.dataset.content_hash(),
            "descriptions": {
                sensor_id: description.as_dict()
                for sensor_id, description in sorted(self.descriptions.items())
            },
            "active_formats": dict(sorted(self.active_formats.items())),
            "fusion_config": self.fusion_config.as_dict(),
        }

    def replace_description(
        self,
        sensor_id: str,
        description: SensorDescription,
    ) -> "FlightState":
        descriptions = dict(self.descriptions)
        descriptions[sensor_id] = description
        return replace(self, descriptions=descriptions)

    def replace_config(self, config: FusionConfig) -> "FlightState":
        return replace(self, fusion_config=config)

