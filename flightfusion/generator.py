"""Fixed-seed flight data and description sample generator."""

from __future__ import annotations

import csv
import io
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .calibration import resolve_calibration
from .mathutils import (
    quat_conjugate,
    quat_from_euler_zyx,
    quat_multiply,
    rotate_world_to_body,
)


DEFAULT_SEED = 20261123
DEFAULT_DURATION = 4.0
DEFAULT_CONFIG = {
    "config_version": "fusion-1.0.0",
    "complementary_gain": 0.075,
    "accel_weight": 0.55,
    "mag_weight": 0.22,
    "gravity_constant": 9.80665,
    "gyro_bias_window": 24,
    "output_rate_hz": 100.0,
    "mag_reference": [24.0, 0.0, 42.0],
    "downgrade_policy": {
        "axis_coupling": "diag",
        "noise_model": "drop",
        "temperature_compensation": "drop",
    },
}


AUTO_DESCRIPTIONS = {
    "gyro": {
        "sensor_id": "gyro",
        "sensor_type": "gyro",
        "range": 2000.0,
        "sample_rate": 200.0,
        "axis_order": ["x", "y", "z"],
        "calibration_version": 3,
    },
    "accel": {
        "sensor_id": "accel",
        "sensor_type": "accel",
        "range": 16.0,
        "sample_rate": 100.0,
        "axis_order": ["y", "x", "z"],
        "calibration_version": 2,
    },
    "mag": {
        "sensor_id": "mag",
        "sensor_type": "mag",
        "range": 4900.0,
        "sample_rate": 50.0,
        "axis_order": ["x", "z", "y"],
        "calibration_version": 4,
    },
}

AUTO_V3_EXTRAS = {
    "gyro": {
        "axis_coupling": [
            [1.0, 0.0012, -0.0007],
            [-0.0005, 1.0, 0.0009],
            [0.0004, -0.0011, 1.0],
        ],
        "noise_model": {"white_noise_std": 0.015, "bias_random_walk_std": 0.00012},
        "temperature_compensation": {
            "reference_c": 25.0,
            "coefficients": [
                [0.00002, -0.00001, 0.0],
                [0.00001, 0.00003, 0.0],
                [-0.00002, 0.00001, 0.0],
            ],
        },
    },
    "accel": {
        "axis_coupling": [
            [1.0, 0.0021, 0.0003],
            [0.0002, 1.0, -0.0014],
            [-0.0006, 0.0008, 1.0],
        ],
        "noise_model": {"white_noise_std": 0.0008, "bias_random_walk_std": 0.000015},
        "temperature_compensation": {
            "reference_c": 25.0,
            "coefficients": [
                [0.00003, 0.0, -0.00001],
                [0.0, 0.00002, 0.00001],
                [0.00001, -0.00001, 0.00004],
            ],
        },
    },
    "mag": {
        "axis_coupling": [
            [1.0, 0.0030, -0.0018],
            [-0.0012, 1.0, 0.0022],
            [0.0016, -0.0020, 1.0],
        ],
        "noise_model": {"white_noise_std": 0.05, "bias_random_walk_std": 0.002},
        "temperature_compensation": {
            "reference_c": 25.0,
            "coefficients": [
                [-0.00004, 0.00002, 0.0],
                [0.00003, 0.00001, 0.0],
                [0.0, -0.00003, 0.00005],
            ],
        },
    },
}


def _truth_quaternion(t: float) -> tuple[float, float, float, float]:
    roll = math.radians(6.5 * math.sin(0.82 * t))
    pitch = math.radians(4.5 * math.sin(0.57 * t + 0.35) - 4.5 * math.sin(0.35))
    yaw = math.radians(58.0 * (1.0 - math.cos(0.31 * t)))
    return quat_from_euler_zyx(yaw, pitch, roll)


def _body_rate(t: float, epsilon: float = 1e-5) -> tuple[float, float, float]:
    q0 = _truth_quaternion(t)
    q1 = _truth_quaternion(t + epsilon)
    relative = quat_multiply(quat_conjugate(q0), q1)
    if relative[0] < 0:
        relative = tuple(-item for item in relative)  # type: ignore[assignment]
    angle = 2.0 * math.acos(max(-1.0, min(1.0, relative[0])))
    sine = math.sqrt(max(0.0, 1.0 - relative[0] * relative[0]))
    if sine < 1e-12:
        return (0.0, 0.0, 0.0)
    axis = (relative[1] / sine, relative[2] / sine, relative[3] / sine)
    return tuple(item * angle / epsilon for item in axis)  # type: ignore[return-value]


def _times(rate: float, duration: float) -> np.ndarray:
    return np.arange(0.0, duration + 0.5 / rate, 1.0 / rate, dtype=np.float64)


def _axis_inverse(values: tuple[float, float, float], axis_order: list[str]) -> list[float]:
    raw = [0.0, 0.0, 0.0]
    for canonical_index, axis in enumerate(("x", "y", "z")):
        raw_index = axis_order.index(axis)
        raw[raw_index] = values[canonical_index]
    return raw


def _noise(rng: np.random.Generator, count: int, sigma: tuple[float, float, float]) -> np.ndarray:
    return rng.normal(0.0, np.asarray(sigma, dtype=np.float64), size=(count, 3))


def generate_dataset(seed: int = DEFAULT_SEED, duration: float = DEFAULT_DURATION) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    gyro_times = _times(200.0, duration)
    accel_times = _times(100.0, duration)
    mag_times = _times(50.0, duration)
    truth_times = _times(400.0, duration)

    truth_q = [_truth_quaternion(float(t)) for t in truth_times]
    truth = {
        "times": [round(float(t), 12) for t in truth_times],
        "quaternions": [[round(item, 12) for item in q] for q in truth_q],
    }

    world_up = (0.0, 0.0, 1.0)
    world_mag = (24.0, 0.0, 42.0)

    gyro_bias, gyro_scale = resolve_calibration("gyro", 3)
    accel_bias, accel_scale = resolve_calibration("accel", 2)
    mag_bias, mag_scale = resolve_calibration("mag", 4)

    gyro_noise = _noise(rng, len(gyro_times), (0.015, 0.015, 0.015))
    accel_noise = _noise(rng, len(accel_times), (0.0007, 0.0007, 0.0007))
    mag_noise = _noise(rng, len(mag_times), (0.045, 0.045, 0.045))

    gyro_values: list[list[float]] = []
    for index, t in enumerate(gyro_times):
        rate_rad = _body_rate(float(t))
        rate_deg = tuple(math.degrees(item) for item in rate_rad)
        clean = tuple(
            rate_deg[axis] + math.degrees(gyro_bias[axis]) + gyro_noise[index, axis]
            for axis in range(3)
        )
        # ``clean`` above is already the post-calibration target. Construct raw
        # values so that reorder -> scale -> bias recovers it.
        canonical = tuple(
            (clean[axis] - gyro_bias[axis]) / gyro_scale[axis] for axis in range(3)
        )
        raw = _axis_inverse(canonical, AUTO_DESCRIPTIONS["gyro"]["axis_order"])
        gyro_values.append([round(item, 12) for item in raw])

    accel_values: list[list[float]] = []
    for index, t in enumerate(accel_times):
        q = _truth_quaternion(float(t))
        up_body = rotate_world_to_body(world_up, q)
        vibration = 0.0015 * math.sin(2.0 * math.pi * 2.3 * float(t))
        clean = tuple(
            up_body[axis] + (vibration if axis < 2 else 0.0) + accel_noise[index, axis]
            for axis in range(3)
        )
        canonical = tuple(
            (clean[axis] - accel_bias[axis]) / accel_scale[axis] for axis in range(3)
        )
        raw = _axis_inverse(canonical, AUTO_DESCRIPTIONS["accel"]["axis_order"])
        accel_values.append([round(item, 12) for item in raw])

    mag_values: list[list[float]] = []
    for index, t in enumerate(mag_times):
        q = _truth_quaternion(float(t))
        mag_body = rotate_world_to_body(world_mag, q)
        clean = tuple(
            mag_body[axis] + mag_noise[index, axis] for axis in range(3)
        )
        canonical = tuple(
            (clean[axis] - mag_bias[axis]) / mag_scale[axis] for axis in range(3)
        )
        raw = _axis_inverse(canonical, AUTO_DESCRIPTIONS["mag"]["axis_order"])
        mag_values.append([round(item, 12) for item in raw])

    return {
        "seed": seed,
        "duration_s": duration,
        "sequences": {
            "gyro": {
                "sensor_id": "gyro",
                "sensor_type": "gyro",
                "sample_rate": 200.0,
                "times": [round(float(t), 12) for t in gyro_times],
                "values": gyro_values,
            },
            "accel": {
                "sensor_id": "accel",
                "sensor_type": "accel",
                "sample_rate": 100.0,
                "times": [round(float(t), 12) for t in accel_times],
                "values": accel_values,
            },
            "mag": {
                "sensor_id": "mag",
                "sensor_type": "mag",
                "sample_rate": 50.0,
                "times": [round(float(t), 12) for t in mag_times],
                "values": mag_values,
            },
        },
        "truth": truth,
    }


def render_v1(descriptions: dict[str, dict[str, Any]]) -> str:
    lines = ["# FORMAT=v1", "# sensor_id|range|sample_rate|axis_order|calibration_version"]
    for sensor_id in sorted(descriptions):
        item = descriptions[sensor_id]
        lines.append(
            "|".join(
                (
                    sensor_id,
                    f"{float(item['range']):g}",
                    f"{float(item['sample_rate']):g}",
                    ",".join(item["axis_order"]),
                    str(item["calibration_version"]),
                )
            )
        )
    return "\n".join(lines) + "\n"


def render_v2(descriptions: dict[str, dict[str, Any]]) -> str:
    output = io.StringIO()
    output.write("# FORMAT=v2\n")
    writer = csv.DictWriter(output, fieldnames=["sensor_id", "range", "sample_rate", "axis_order", "calibration_version"])
    writer.writeheader()
    for sensor_id in sorted(descriptions):
        item = descriptions[sensor_id]
        writer.writerow(
            {
                "sensor_id": sensor_id,
                "range": f"{float(item['range']):g}",
                "sample_rate": f"{float(item['sample_rate']):g}",
                "axis_order": ",".join(item["axis_order"]),
                "calibration_version": item["calibration_version"],
            }
        )
    return output.getvalue()


def render_v3(descriptions: dict[str, dict[str, Any]]) -> str:
    sensors = []
    for sensor_id in sorted(descriptions):
        item = descriptions[sensor_id]
        sensor_type = item["sensor_type"]
        bias, scale = resolve_calibration(sensor_type, int(item["calibration_version"]))
        extras = AUTO_V3_EXTRAS[sensor_type]
        sensors.append(
            {
                "sensor_id": sensor_id,
                "sensor_type": sensor_type,
                "range": float(item["range"]),
                "sample_rate": float(item["sample_rate"]),
                "axis_order": list(item["axis_order"]),
                "calibration": {
                    "version": int(item["calibration_version"]),
                    "bias": list(bias),
                    "scale": list(scale),
                },
                "axis_coupling": extras["axis_coupling"],
                "noise_model": extras["noise_model"],
                "temperature_compensation": extras["temperature_compensation"],
            }
        )
    return json.dumps(
        {
            "format": "flightfusion.sensor-descriptions",
            "version": 3,
            "sensors": sensors,
        },
        indent=2,
        sort_keys=False,
    ) + "\n"


def write_samples(root: str | Path, *, force: bool = False) -> list[Path]:
    base = Path(root)
    data_dir = base / "data"
    formats_dir = data_dir / "formats"
    invalid_dir = data_dir / "invalid"
    formats_dir.mkdir(parents=True, exist_ok=True)
    invalid_dir.mkdir(parents=True, exist_ok=True)

    dataset_path = data_dir / "flight_imu.json"
    if force or not dataset_path.exists():
        dataset_path.write_text(
            json.dumps(generate_dataset(), indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )

    descriptions = {
        key: {
            **value,
            "axis_order": list(value["axis_order"]),
        }
        for key, value in AUTO_DESCRIPTIONS.items()
    }
    files: list[Path] = [dataset_path]
    for name, renderer in (("sensors.v1.txt", render_v1), ("sensors.v2.csv", render_v2), ("sensors.v3.json", render_v3)):
        path = formats_dir / name
        if force or not path.exists():
            path.write_text(renderer(descriptions), encoding="utf-8")
        files.append(path)

    config_path = data_dir / "fusion_config.json"
    if force or not config_path.exists():
        config_path.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
    files.append(config_path)

    invalid_samples = {
        "unknown_version.v1.txt": "# FORMAT=v9\ngyro|2000|200|x,y,z|3\n",
        "field_count.v1.txt": "# FORMAT=v1\ngyro|2000|200|x,y,z\n",
        "missing_sensor_id.v2.csv": (
            "# FORMAT=v2\n"
            "sensor_id,range,sample_rate,axis_order,calibration_version\n"
            ",16,100,x,y,z,2\n"
        ),
        "bad_number.v2.csv": (
            "# FORMAT=v2\n"
            "sensor_id,range,sample_rate,axis_order,calibration_version\n"
            "accel,16,not-a-rate,x,y,z,2\n"
        ),
        "axis_missing.v1.txt": (
            "# FORMAT=v1\ngyro|2000|200|x,y,q|3\n"
        ),
        "range_out_of_bounds.v1.txt": (
            "# FORMAT=v1\ngyro|-1|200|x,y,z|3\n"
        ),
        "corrupt.v3.json": '{"version":3,"sensors":[{"sensor_id":"mag",',
    }
    for name, text in invalid_samples.items():
        path = invalid_dir / name
        if force or not path.exists():
            path.write_text(text, encoding="utf-8")
        files.append(path)
    return files

