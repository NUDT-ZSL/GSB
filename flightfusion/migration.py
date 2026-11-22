"""Adjacent version migrations with explicit completion and loss provenance."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, replace
from typing import Any

from .calibration import resolve_calibration
from .errors import ErrorCode
from .formats import V2_COLUMNS
from .models import (
    DEFAULT_NOISE,
    DEFAULT_TEMPERATURE,
    IDENTITY3,
    SensorDescription,
)
from .serialization import content_hash


@dataclass(frozen=True)
class MigrationResult:
    source_version: int
    target_version: int
    descriptions: dict[str, SensorDescription]
    serialized: str
    completions: tuple[dict[str, Any], ...]
    losses: tuple[dict[str, Any], ...]
    transformations: tuple[dict[str, Any], ...]
    behavior_checksums: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "completions": list(self.completions),
            "losses": list(self.losses),
            "transformations": list(self.transformations),
            "behavior_checksums": dict(self.behavior_checksums),
            "serialized": self.serialized,
        }


def behavior_fingerprint(description: SensorDescription) -> dict[str, Any]:
    """Fields that drive reorder -> scale/bias -> time-alignment behavior."""

    return {
        "sensor_id": description.sensor_id,
        "sensor_type": description.sensor_type,
        "axis_order": list(description.axis_order),
        "axis_mapping": description.axis_mapping,
        "bias": list(description.bias),
        "scale": list(description.scale),
        "calibration_version": description.calibration_version,
        "sample_rate": description.sample_rate,
    }


def _serialize_v1(descriptions: dict[str, SensorDescription]) -> str:
    lines = ["# FORMAT=v1", "# sensor_id|range|sample_rate|axis_order|calibration_version"]
    for sensor_id in sorted(descriptions):
        item = descriptions[sensor_id]
        lines.append(
            "|".join(
                (
                    item.sensor_id,
                    f"{item.range_limit:g}",
                    f"{item.sample_rate:g}",
                    ",".join(item.axis_order),
                    str(item.calibration_version),
                )
            )
        )
    return "\n".join(lines) + "\n"


def _serialize_v2(descriptions: dict[str, SensorDescription]) -> str:
    output = io.StringIO()
    output.write("# FORMAT=v2\n")
    writer = csv.DictWriter(output, fieldnames=list(V2_COLUMNS))
    writer.writeheader()
    for sensor_id in sorted(descriptions):
        item = descriptions[sensor_id]
        row: dict[str, Any] = {
            "sensor_id": item.sensor_id,
            "range": f"{item.range_limit:g}",
            "sample_rate": f"{item.sample_rate:g}",
            "axis_order": ",".join(item.axis_order),
            "calibration_version": item.calibration_version,
            "bias_x": item.bias[0],
            "bias_y": item.bias[1],
            "bias_z": item.bias[2],
            "scale_x": item.scale[0],
            "scale_y": item.scale[1],
            "scale_z": item.scale[2],
            "noise_white": item.noise_model.get("white_noise_std", 0.0),
            "noise_walk": item.noise_model.get("bias_random_walk_std", 0.0),
            "temp_reference_c": item.temperature_compensation.get(
                "reference_c", DEFAULT_TEMPERATURE["reference_c"]
            ),
        }
        for row_index, prefix in enumerate(("x", "y", "z")):
            for column_index, suffix in enumerate(("x", "y", "z")):
                row[f"coupling_{prefix}{suffix}"] = item.axis_coupling[row_index][column_index]
                row[f"temp_{prefix}{column_index}"] = item.temperature_compensation[
                    "coefficients"
                ][row_index][column_index]
        writer.writerow(row)
    return output.getvalue()


def _serialize_v3(descriptions: dict[str, SensorDescription]) -> str:
    sensors = []
    for sensor_id in sorted(descriptions):
        item = descriptions[sensor_id]
        sensors.append(
            {
                "sensor_id": item.sensor_id,
                "sensor_type": item.sensor_type,
                "range": item.range_limit,
                "sample_rate": item.sample_rate,
                "axis_order": list(item.axis_order),
                "calibration": {
                    "version": item.calibration_version,
                    "bias": list(item.bias),
                    "scale": list(item.scale),
                },
                "axis_coupling": [list(row) for row in item.axis_coupling],
                "noise_model": dict(item.noise_model),
                "temperature_compensation": {
                    "reference_c": item.temperature_compensation.get("reference_c"),
                    "coefficients": [
                        list(row)
                        for row in item.temperature_compensation.get("coefficients", ())
                    ],
                },
            }
        )
    return (
        json.dumps(
            {
                "format": "flightfusion.sensor-descriptions",
                "version": 3,
                "sensors": sensors,
            },
            indent=2,
            sort_keys=False,
        )
        + "\n"
    )


def _completion(
    sensor_id: str,
    field: str,
    source: str,
    value: Any,
) -> dict[str, Any]:
    return {
        "sensor_id": sensor_id,
        "field": field,
        "source": source,
        "value": value,
    }


def _loss(
    sensor_id: str,
    field: str,
    action: str,
    reason: str,
    value: Any,
) -> dict[str, Any]:
    return {
        "code": ErrorCode.MIGRATION_FIELD_LOST,
        "sensor_id": sensor_id,
        "field": field,
        "action": action,
        "reason": reason,
        "value": value,
    }


def _as_format(descriptions: dict[str, SensorDescription], version: int) -> dict[str, SensorDescription]:
    return {
        sensor_id: replace(description, format_version=version)
        for sensor_id, description in descriptions.items()
    }


def upgrade_v1_to_v2(
    descriptions: dict[str, SensorDescription],
) -> MigrationResult:
    migrated = _as_format(descriptions, 2)
    completions: list[dict[str, Any]] = []
    for sensor_id in sorted(descriptions):
        item = descriptions[sensor_id]
        for field in ("sensor_id", "range", "sample_rate", "axis_order", "calibration_version"):
            completions.append(
                _completion(sensor_id, f"v2.{field}", "v1.positional_field", getattr(item, field, None))
            )
        completions.extend(
            [
                _completion(sensor_id, "v2.bias", "calibration_registry", list(item.bias)),
                _completion(sensor_id, "v2.scale", "calibration_registry", list(item.scale)),
            ]
        )
    return MigrationResult(
        source_version=1,
        target_version=2,
        descriptions=migrated,
        serialized=_serialize_v2(migrated),
        completions=tuple(completions),
        losses=(),
        transformations=(),
        behavior_checksums={
            sensor_id: content_hash(behavior_fingerprint(item))
            for sensor_id, item in descriptions.items()
        },
    )


def upgrade_v2_to_v3(
    descriptions: dict[str, SensorDescription],
) -> MigrationResult:
    migrated: dict[str, SensorDescription] = {}
    completions: list[dict[str, Any]] = []
    for sensor_id in sorted(descriptions):
        item = descriptions[sensor_id]
        provenance = dict(item.provenance)
        present = set(item.present_fields)
        if not any(field.startswith("coupling_") for field in present):
            for row_index, prefix in enumerate(("x", "y", "z")):
                for column_index, suffix in enumerate(("x", "y", "z")):
                    field = f"axis_coupling.{prefix}{suffix}"
                    value = 1.0 if row_index == column_index else 0.0
                    completions.append(
                        _completion(
                            sensor_id,
                            field,
                            "defaults.v3.identity_coupling",
                            value,
                        )
                    )
            provenance["axis_coupling"] = "defaults.v3.identity_coupling"
        else:
            provenance["axis_coupling"] = "v2.flattened_axis_coupling"

        if "noise_white" not in present and "noise_walk" not in present:
            for field, value in DEFAULT_NOISE.items():
                completions.append(
                    _completion(
                        sensor_id,
                        f"noise_model.{field}",
                        "defaults.v3.noise_model",
                        value,
                    )
                )
            provenance["noise_model"] = "defaults.v3.noise_model"
        else:
            provenance["noise_model"] = "v2.flattened_noise_model"

        if "temp_reference_c" not in present and not any(
            field.startswith("temp_") for field in present
        ):
            completions.append(
                _completion(
                    sensor_id,
                    "temperature_compensation.reference_c",
                    "defaults.v3.zero_temperature_coefficients",
                    DEFAULT_TEMPERATURE["reference_c"],
                )
            )
            for row_index, prefix in enumerate(("x", "y", "z")):
                for column_index in range(3):
                    completions.append(
                        _completion(
                            sensor_id,
                            f"temperature_compensation.coefficients.{prefix}{column_index}",
                            "defaults.v3.zero_temperature_coefficients",
                            0.0,
                        )
                    )
            provenance["temperature_compensation"] = (
                "defaults.v3.zero_temperature_coefficients"
            )
        else:
            provenance["temperature_compensation"] = "v2.flattened_temperature"

        migrated[sensor_id] = replace(
            item,
            format_version=3,
            provenance=provenance,
        )
    return MigrationResult(
        source_version=2,
        target_version=3,
        descriptions=migrated,
        serialized=_serialize_v3(migrated),
        completions=tuple(completions),
        losses=(),
        transformations=(),
        behavior_checksums={
            sensor_id: content_hash(behavior_fingerprint(item))
            for sensor_id, item in descriptions.items()
        },
    )


def downgrade_v3_to_v2(
    descriptions: dict[str, SensorDescription],
) -> MigrationResult:
    migrated = _as_format(descriptions, 2)
    transformations: list[dict[str, Any]] = []
    for sensor_id in sorted(descriptions):
        item = descriptions[sensor_id]
        for row_index, prefix in enumerate(("x", "y", "z")):
            for column_index, suffix in enumerate(("x", "y", "z")):
                transformations.append(
                    {
                        "sensor_id": sensor_id,
                        "field": f"axis_coupling.{prefix}{suffix}",
                        "action": "flatten",
                        "target_field": f"coupling_{prefix}{suffix}",
                        "value": item.axis_coupling[row_index][column_index],
                    }
                )
        for field, value in item.noise_model.items():
            transformations.append(
                {
                    "sensor_id": sensor_id,
                    "field": f"noise_model.{field}",
                    "action": "flatten",
                    "target_field": "noise_white"
                    if field == "white_noise_std"
                    else "noise_walk",
                    "value": value,
                }
            )
        transformations.append(
            {
                "sensor_id": sensor_id,
                "field": "temperature_compensation",
                "action": "flatten",
                "target_field": "temp_*",
                "value": {
                    "reference_c": item.temperature_compensation.get("reference_c"),
                    "coefficients": [
                        list(row)
                        for row in item.temperature_compensation.get("coefficients", ())
                    ],
                },
            }
        )
    return MigrationResult(
        source_version=3,
        target_version=2,
        descriptions=migrated,
        serialized=_serialize_v2(migrated),
        completions=(),
        losses=(),
        transformations=tuple(transformations),
        behavior_checksums={
            sensor_id: content_hash(behavior_fingerprint(item))
            for sensor_id, item in descriptions.items()
        },
    )


def downgrade_v2_to_v1(
    descriptions: dict[str, SensorDescription],
    *,
    policy: dict[str, str] | None = None,
) -> MigrationResult:
    rules = {
        "axis_coupling": "diag",
        "noise_model": "drop",
        "temperature_compensation": "drop",
    }
    rules.update(policy or {})
    losses: list[dict[str, Any]] = []
    for sensor_id in sorted(descriptions):
        item = descriptions[sensor_id]
        non_identity = item.axis_coupling != IDENTITY3
        if non_identity:
            action = rules.get("axis_coupling", "diag")
            if action not in {"drop", "diag"}:
                action = "drop"
            losses.append(
                _loss(
                    sensor_id,
                    "axis_coupling",
                    "diagonal_approximation" if action == "diag" else "drop",
                    "v1 has no field for a 3x3 axis coupling matrix",
                    {
                        "matrix": [list(row) for row in item.axis_coupling],
                        "approximated_diagonal": (
                            [item.axis_coupling[index][index] for index in range(3)]
                            if action == "diag"
                            else None
                        ),
                    },
                )
            )
        if item.noise_model != DEFAULT_NOISE:
            action = rules.get("noise_model", "drop")
            losses.append(
                _loss(
                    sensor_id,
                    "noise_model",
                    "drop",
                    "v1 has no field for a structured noise model",
                    {"policy": action, "value": dict(item.noise_model)},
                )
            )
        if item.temperature_compensation != {
            "reference_c": DEFAULT_TEMPERATURE["reference_c"],
            "coefficients": DEFAULT_TEMPERATURE["coefficients"],
        }:
            losses.append(
                _loss(
                    sensor_id,
                    "temperature_compensation",
                    "drop",
                    "v1 has no field for temperature compensation",
                    dict(item.temperature_compensation),
                )
            )
        expected_bias, expected_scale = resolve_calibration(
            item.sensor_type, item.calibration_version
        )
        if tuple(item.bias) != tuple(expected_bias) or tuple(item.scale) != tuple(expected_scale):
            losses.append(
                _loss(
                    sensor_id,
                    "calibration_override",
                    "drop",
                    "v1 can only reference a calibration registry version",
                    {"bias": list(item.bias), "scale": list(item.scale)},
                )
            )
    migrated = _as_format(descriptions, 1)
    return MigrationResult(
        source_version=2,
        target_version=1,
        descriptions=migrated,
        serialized=_serialize_v1(migrated),
        completions=(),
        losses=tuple(losses),
        transformations=(),
        behavior_checksums={
            sensor_id: content_hash(behavior_fingerprint(item))
            for sensor_id, item in descriptions.items()
        },
    )


def migrate_descriptions(
    descriptions: dict[str, SensorDescription],
    source_version: int,
    target_version: int,
    *,
    policy: dict[str, str] | None = None,
) -> MigrationResult:
    if source_version == target_version:
        return MigrationResult(
            source_version=source_version,
            target_version=target_version,
            descriptions=dict(descriptions),
            serialized="",
            completions=(),
            losses=(),
            transformations=(),
            behavior_checksums={
                sensor_id: content_hash(behavior_fingerprint(item))
                for sensor_id, item in descriptions.items()
            },
        )
    if source_version == 1 and target_version == 2:
        return upgrade_v1_to_v2(descriptions)
    if source_version == 2 and target_version == 3:
        return upgrade_v2_to_v3(descriptions)
    if source_version == 3 and target_version == 2:
        return downgrade_v3_to_v2(descriptions)
    if source_version == 2 and target_version == 1:
        return downgrade_v2_to_v1(descriptions, policy=policy)
    if source_version == 1 and target_version == 3:
        first = upgrade_v1_to_v2(descriptions)
        second = upgrade_v2_to_v3(first.descriptions)
        return MigrationResult(
            source_version=1,
            target_version=3,
            descriptions=second.descriptions,
            serialized=second.serialized,
            completions=first.completions + second.completions,
            losses=(),
            transformations=(),
            behavior_checksums=first.behavior_checksums,
        )
    if source_version == 3 and target_version == 1:
        first = downgrade_v3_to_v2(descriptions)
        second = downgrade_v2_to_v1(first.descriptions, policy=policy)
        return MigrationResult(
            source_version=3,
            target_version=1,
            descriptions=second.descriptions,
            serialized=second.serialized,
            completions=(),
            losses=second.losses,
            transformations=first.transformations,
            behavior_checksums=first.behavior_checksums,
        )
    raise ValueError(f"unsupported migration v{source_version} -> v{target_version}")


def verify_behavior_preserved(
    source: dict[str, SensorDescription],
    migrated: dict[str, SensorDescription],
) -> bool:
    if set(source) != set(migrated):
        return False
    return all(
        behavior_fingerprint(source[sensor_id]) == behavior_fingerprint(migrated[sensor_id])
        for sensor_id in source
    )

