"""Stable calibration profiles referenced by v1/v2 calibration versions."""

from __future__ import annotations

from typing import Iterable

from .errors import BusinessError, ErrorCode


_PROFILES: dict[str, dict[int, tuple[tuple[float, float, float], tuple[float, float, float]]]] = {
    "gyro": {
        1: ((0.0100, -0.0200, 0.0150), (1.0020, 0.9980, 1.0010)),
        2: ((0.0070, -0.0150, 0.0100), (1.0012, 0.9988, 1.0006)),
        3: ((0.0040, -0.0100, 0.0060), (1.0005, 0.9994, 1.0002)),
        4: ((0.0020, -0.0050, 0.0030), (1.0002, 0.9997, 1.0001)),
    },
    "accel": {
        1: ((0.0300, -0.0200, 0.0400), (1.0020, 0.9980, 1.0015)),
        2: ((0.0180, -0.0110, 0.0250), (1.0010, 0.9990, 1.0008)),
        3: ((0.0090, -0.0050, 0.0120), (1.0004, 0.9996, 1.0003)),
    },
    "mag": {
        1: ((1.2000, -0.8000, 0.6000), (1.0200, 0.9800, 1.0100)),
        2: ((0.9000, -0.6000, 0.4500), (1.0120, 0.9880, 1.0060)),
        3: ((0.6000, -0.4000, 0.3000), (1.0080, 0.9920, 1.0040)),
        4: ((0.3500, -0.2200, 0.1800), (1.0040, 0.9960, 1.0020)),
    },
}


def resolve_calibration(
    sensor_type: str,
    version: int,
    *,
    stage: str = "parse",
    sensor_id: str | None = None,
    field: str = "calibration_version",
    line: int | None = None,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Resolve a calibration version or raise a stable business error."""

    if not isinstance(version, int) or version < 1:
        raise BusinessError(
            code=ErrorCode.FORMAT_CORRUPT,
            message=f"calibration_version must be a positive integer, got {version!r}",
            stage=stage,
            sensor_id=sensor_id,
            field=field,
            line=line,
            value=version,
        )
    profiles = _PROFILES.get(sensor_type)
    if profiles and version in profiles:
        bias, scale = profiles[version]
        return tuple(bias), tuple(scale)

    # Unknown but valid future versions remain deterministic and replayable.
    # This avoids hard-coding the registry as a permanent upper bound.
    base_bias = (0.003, -0.002, 0.0015)
    offset = (version - 1) * 0.0001
    bias = tuple(base_bias[i] / max(version, 1) + offset * (i + 1) for i in range(3))
    scale = tuple(1.0 + ((version * (i + 3)) % 7 - 3) * 0.00005 for i in range(3))
    return bias, scale


def infer_sensor_type(sensor_id: str) -> str:
    normalized = sensor_id.strip().lower().replace("_", "-")
    if normalized.startswith(("gyro", "gyr", "g")):
        return "gyro"
    if normalized.startswith(("accel", "acc", "a")):
        return "accel"
    if normalized.startswith(("mag", "m")):
        return "mag"
    if normalized in {"0", "1", "2"}:
        return {"0": "gyro", "1": "accel", "2": "mag"}[normalized]
    raise BusinessError(
        code=ErrorCode.FORMAT_CORRUPT,
        message=f"cannot infer sensor type from sensor_id {sensor_id!r}",
        stage="parse",
        sensor_id=sensor_id,
        field="sensor_id",
    )


def validate_axis_order(axis_order: Iterable[str]) -> tuple[str, str, str]:
    values = tuple(str(item).strip().lower() for item in axis_order)
    if len(values) != 3:
        raise ValueError("axis_order must contain exactly three axes")
    if set(values) != {"x", "y", "z"} or len(set(values)) != 3:
        raise ValueError("axis_order must be a permutation of x, y, z")
    return values  # type: ignore[return-value]


def axis_mapping(axis_order: Iterable[str]) -> dict[str, int]:
    order = validate_axis_order(axis_order)
    return {axis: order.index(axis) for axis in ("x", "y", "z")}

