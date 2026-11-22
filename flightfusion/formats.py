"""Readers and dispatch logic for the three coexisting description formats."""

from __future__ import annotations

import csv
import io
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable

from .calibration import infer_sensor_type, resolve_calibration
from .errors import BusinessError, ErrorCode
from .models import (
    DEFAULT_NOISE,
    DEFAULT_TEMPERATURE,
    IDENTITY3,
    MATRIX3,
    TRIPLE,
    SensorDescription,
    _matrix3,
    _triple,
)


V1_COLUMNS = ("sensor_id", "range", "sample_rate", "axis_order", "calibration_version")
V2_BASE_COLUMNS = V1_COLUMNS
V2_OPTIONAL_COLUMNS = (
    "bias_x",
    "bias_y",
    "bias_z",
    "scale_x",
    "scale_y",
    "scale_z",
    "coupling_xx",
    "coupling_xy",
    "coupling_xz",
    "coupling_yx",
    "coupling_yy",
    "coupling_yz",
    "coupling_zx",
    "coupling_zy",
    "coupling_zz",
    "noise_white",
    "noise_walk",
    "temp_reference_c",
    "temp_x0",
    "temp_x1",
    "temp_x2",
    "temp_y0",
    "temp_y1",
    "temp_y2",
    "temp_z0",
    "temp_z1",
    "temp_z2",
)
V2_COLUMNS = V2_BASE_COLUMNS + V2_OPTIONAL_COLUMNS

_FORMAT_HEADER_RE = re.compile(r"^\s*#\s*FORMAT\s*=\s*v?(\d+)\s*$", re.IGNORECASE)


def _business(
    code: str,
    message: str,
    *,
    stage: str = "parse",
    sensor_id: str | None = None,
    field: str | None = None,
    line: int | None = None,
    value: Any = None,
    degradation: str | None = None,
) -> BusinessError:
    return BusinessError(
        code=code,
        message=message,
        stage=stage,
        sensor_id=sensor_id,
        field=field,
        line=line,
        value=value,
        degradation=degradation,
    )


def _nonempty_lines(text: str) -> list[tuple[int, str]]:
    return [
        (line_number, line.rstrip("\r\n"))
        for line_number, line in enumerate(text.splitlines(), start=1)
        if line.strip()
    ]


def detect_format(text: str) -> tuple[int, bool]:
    """Return ``(format_version, explicit_header)``."""

    lines = _nonempty_lines(text)
    if not lines:
        raise _business(ErrorCode.FORMAT_CORRUPT, "description document is empty", line=1)

    for line_number, line in lines:
        match = _FORMAT_HEADER_RE.match(line)
        if not match:
            continue
        version = int(match.group(1))
        if version not in {1, 2, 3}:
            raise _business(
                ErrorCode.VERSION_UNSUPPORTED,
                f"unsupported description format version v{version}",
                field="format_version",
                line=line_number,
                value=version,
            )
        return version, True

    content_lines = [
        (line_number, line)
        for line_number, line in lines
        if not line.lstrip().startswith("#")
    ]
    if not content_lines:
        raise _business(
            ErrorCode.FORMAT_CORRUPT,
            "description document contains only comments",
            field="document",
            line=lines[-1][0],
        )
    first_line_number, first = content_lines[0]
    stripped = first.strip()
    if stripped.startswith(("{", "[")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _business(
                ErrorCode.FORMAT_CORRUPT,
                f"invalid JSON description: {exc.msg}",
                field="$",
                line=exc.lineno,
            ) from exc
        version = data.get("version", data.get("format_version"))
        try:
            version_int = int(version)
        except (TypeError, ValueError) as exc:
            raise _business(
                ErrorCode.VERSION_UNSUPPORTED,
                "structured description has no supported version field",
                field="version",
                line=1,
                value=version,
            ) from exc
        if version_int not in {1, 2, 3}:
            raise _business(
                ErrorCode.VERSION_UNSUPPORTED,
                f"unsupported description format version v{version_int}",
                field="version",
                line=1,
                value=version_int,
            )
        return version_int, False

    if "|" in stripped and not _looks_like_v2_header(stripped):
        return 1, False
    if _looks_like_v2_header(stripped):
        return 2, False
    raise _business(
        ErrorCode.VERSION_UNSUPPORTED,
        "cannot dispatch description format from header or field set",
        field="format_version",
        line=first_line_number,
    )


def _looks_like_v2_header(line: str) -> bool:
    lowered = line.lower()
    return "sensor_id" in lowered and "sample_rate" in lowered and (
        "," in line or "\t" in line
    )


def _csv_rows(text: str):
    reader = csv.reader(io.StringIO(text))
    try:
        for row in reader:
            yield reader.line_num, row
    except csv.Error as exc:
        raise _business(
            ErrorCode.FORMAT_CORRUPT,
            f"invalid v2 CSV syntax: {exc}",
            field="csv",
            line=reader.line_num,
        ) from exc


def _parse_float(
    raw: Any,
    *,
    field: str,
    line: int,
    sensor_id: str | None,
) -> float:
    if raw is None or str(raw).strip() == "":
        raise _business(
            ErrorCode.FORMAT_CORRUPT,
            f"required field {field} is missing",
            sensor_id=sensor_id,
            field=field,
            line=line,
            value=raw,
        )
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise _business(
            ErrorCode.FORMAT_CORRUPT,
            f"field {field} is not numeric: {raw!r}",
            sensor_id=sensor_id,
            field=field,
            line=line,
            value=raw,
        ) from exc
    if not math.isfinite(value):
        raise _business(
            ErrorCode.FORMAT_CORRUPT,
            f"field {field} must be finite, got {raw!r}",
            sensor_id=sensor_id,
            field=field,
            line=line,
            value=raw,
        )
    return value


def _parse_int(
    raw: Any,
    *,
    field: str,
    line: int,
    sensor_id: str | None,
) -> int:
    value = _parse_float(raw, field=field, line=line, sensor_id=sensor_id)
    if not value.is_integer():
        raise _business(
            ErrorCode.FORMAT_CORRUPT,
            f"field {field} must be an integer, got {raw!r}",
            sensor_id=sensor_id,
            field=field,
            line=line,
            value=raw,
        )
    return int(value)


def _split_axis_order(raw: Any, *, line: int, sensor_id: str | None) -> TRIPLE:
    if raw is None or str(raw).strip() == "":
        raise _business(
            ErrorCode.FORMAT_CORRUPT,
            "required field axis_order is missing",
            sensor_id=sensor_id,
            field="axis_order",
            line=line,
        )
    if isinstance(raw, (list, tuple)):
        parts = [str(item).strip().lower() for item in raw]
    else:
        text = str(raw).strip().lower()
        if "," in text:
            parts = [item.strip() for item in text.split(",")]
        elif len(text) == 3 and set(text) == {"x", "y", "z"}:
            parts = list(text)
        else:
            parts = [item.strip() for item in re.split(r"[;/\s]+", text) if item.strip()]
    if len(parts) != 3 or set(parts) != {"x", "y", "z"}:
        raise _business(
            ErrorCode.AXIS_MAPPING_INVALID,
            f"axis_order must be a permutation of x,y,z, got {parts!r}",
            sensor_id=sensor_id,
            field="axis_order",
            line=line,
            value=parts,
            degradation="fallback_identity_axis_mapping",
        )
    return tuple(parts)  # type: ignore[return-value]


def _finish_description(
    *,
    sensor_id: str,
    range_limit: float,
    sample_rate: float,
    axis_order: TRIPLE,
    calibration_version: int,
    format_version: int,
    line: int,
    bias: TRIPLE | None = None,
    scale: TRIPLE | None = None,
    coupling: MATRIX3 = IDENTITY3,
    noise: dict[str, float] | None = None,
    temperature: dict[str, Any] | None = None,
    provenance: dict[str, str] | None = None,
    present_fields: Iterable[str] = (),
) -> SensorDescription:
    try:
        sensor_type = infer_sensor_type(sensor_id)
    except BusinessError as exc:
        exc.line = line
        raise
    if range_limit <= 0:
        raise _business(
            ErrorCode.FORMAT_CORRUPT,
            "range must be positive",
            sensor_id=sensor_id,
            field="range",
            line=line,
            value=range_limit,
        )
    if sample_rate <= 0:
        # Sample-rate validity is a preprocessing concern. Keeping the parsed
        # value allows the configured degradation policy to act on it.
        pass
    if bias is None or scale is None:
        resolved_bias, resolved_scale = resolve_calibration(
            sensor_type,
            calibration_version,
            sensor_id=sensor_id,
            line=line,
        )
        bias = bias or resolved_bias
        scale = scale or resolved_scale
    return SensorDescription(
        sensor_id=sensor_id,
        sensor_type=sensor_type,
        range_limit=range_limit,
        sample_rate=sample_rate,
        axis_order=axis_order,
        calibration_version=calibration_version,
        format_version=format_version,
        bias=bias,
        scale=scale,
        axis_coupling=coupling,
        noise_model=dict(noise or DEFAULT_NOISE),
        temperature_compensation=dict(
            temperature
            or {
                "reference_c": DEFAULT_TEMPERATURE["reference_c"],
                "coefficients": DEFAULT_TEMPERATURE["coefficients"],
            }
        ),
        provenance=dict(provenance or {}),
        present_fields=tuple(sorted(set(present_fields))),
    )


def parse_v1(text: str) -> dict[str, SensorDescription]:
    descriptions: dict[str, SensorDescription] = {}
    for line_number, line in _nonempty_lines(text):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != len(V1_COLUMNS):
            raise _business(
                ErrorCode.FORMAT_CORRUPT,
                f"v1 row must contain exactly {len(V1_COLUMNS)} pipe-delimited fields, "
                f"got {len(parts)}",
                field="row",
                line=line_number,
                value=len(parts),
            )
        values = [part.strip() for part in parts]
        sensor_id = values[0]
        if not sensor_id:
            raise _business(
                ErrorCode.FORMAT_CORRUPT,
                "required field sensor_id is missing",
                field="sensor_id",
                line=line_number,
                value="",
            )
        if sensor_id in descriptions:
            raise _business(
                ErrorCode.FORMAT_CORRUPT,
                f"duplicate sensor_id {sensor_id!r}",
                sensor_id=sensor_id,
                field="sensor_id",
                line=line_number,
            )
        range_limit = _parse_float(
            values[1], field="range", line=line_number, sensor_id=sensor_id
        )
        sample_rate = _parse_float(
            values[2], field="sample_rate", line=line_number, sensor_id=sensor_id
        )
        axis_order = _split_axis_order(
            values[3], line=line_number, sensor_id=sensor_id
        )
        calibration_version = _parse_int(
            values[4],
            field="calibration_version",
            line=line_number,
            sensor_id=sensor_id,
        )
        descriptions[sensor_id] = _finish_description(
            sensor_id=sensor_id,
            range_limit=range_limit,
            sample_rate=sample_rate,
            axis_order=axis_order,
            calibration_version=calibration_version,
            format_version=1,
            line=line_number,
            provenance={column: "v1.positional_field" for column in V1_COLUMNS},
            present_fields=V1_COLUMNS,
        )
    if not descriptions:
        raise _business(ErrorCode.FORMAT_CORRUPT, "v1 document contains no sensor rows", line=1)
    return descriptions


def parse_v2(text: str) -> dict[str, SensorDescription]:
    descriptions: dict[str, SensorDescription] = {}
    header: list[str] | None = None
    header_line = 0
    for line_number, row in _csv_rows(text):
        if not row or all(not str(cell).strip() for cell in row):
            continue
        if str(row[0]).lstrip().startswith("#"):
            continue
        if header is None:
            header = [str(cell).strip() for cell in row]
            header_line = line_number
            missing = [column for column in V2_BASE_COLUMNS if column not in header]
            if missing:
                raise _business(
                    ErrorCode.FORMAT_CORRUPT,
                    f"v2 header is missing required fields: {', '.join(missing)}",
                    field=missing[0],
                    line=header_line,
                    value=header,
                )
            if len(set(header)) != len(header):
                raise _business(
                    ErrorCode.FORMAT_CORRUPT,
                    "v2 header contains duplicate field names",
                    field="header",
                    line=header_line,
                    value=header,
                )
            continue

        row_values: dict[str, str] = {}
        for index, column in enumerate(header):
            row_values[column] = str(row[index]).strip() if index < len(row) else ""
        sensor_id = row_values.get("sensor_id", "")
        if not sensor_id:
            raise _business(
                ErrorCode.FORMAT_CORRUPT,
                "required field sensor_id is missing",
                field="sensor_id",
                line=line_number,
                value="",
            )
        if sensor_id in descriptions:
            raise _business(
                ErrorCode.FORMAT_CORRUPT,
                f"duplicate sensor_id {sensor_id!r}",
                sensor_id=sensor_id,
                field="sensor_id",
                line=line_number,
            )
        range_limit = _parse_float(
            row_values.get("range"),
            field="range",
            line=line_number,
            sensor_id=sensor_id,
        )
        sample_rate = _parse_float(
            row_values.get("sample_rate"),
            field="sample_rate",
            line=line_number,
            sensor_id=sensor_id,
        )
        axis_order = _split_axis_order(
            row_values.get("axis_order"), line=line_number, sensor_id=sensor_id
        )
        calibration_version = _parse_int(
            row_values.get("calibration_version"),
            field="calibration_version",
            line=line_number,
            sensor_id=sensor_id,
        )

        present = set(V2_BASE_COLUMNS)
        provenance: dict[str, str] = {
            column: "v2.named_field" for column in V2_BASE_COLUMNS
        }
        bias: TRIPLE | None = None
        scale: TRIPLE | None = None
        bias_fields = ("bias_x", "bias_y", "bias_z")
        scale_fields = ("scale_x", "scale_y", "scale_z")
        if any(row_values.get(field, "") != "" for field in bias_fields + scale_fields):
            bias = _triple(
                [
                    _parse_float(
                        row_values.get(field),
                        field=field,
                        line=line_number,
                        sensor_id=sensor_id,
                    )
                    for field in bias_fields
                ],
                "bias",
                sensor_id,
            )
            scale = _triple(
                [
                    _parse_float(
                        row_values.get(field),
                        field=field,
                        line=line_number,
                        sensor_id=sensor_id,
                    )
                    for field in scale_fields
                ],
                "scale",
                sensor_id,
            )
            present.update(bias_fields + scale_fields)
            provenance.update(
                {field: "v2.explicit_calibration" for field in bias_fields + scale_fields}
            )

        coupling = IDENTITY3
        coupling_fields = (
            "coupling_xx",
            "coupling_xy",
            "coupling_xz",
            "coupling_yx",
            "coupling_yy",
            "coupling_yz",
            "coupling_zx",
            "coupling_zy",
            "coupling_zz",
        )
        if any(row_values.get(field, "") != "" for field in coupling_fields):
            coupling_values = [
                _parse_float(
                    row_values.get(field),
                    field=field,
                    line=line_number,
                    sensor_id=sensor_id,
                )
                for field in coupling_fields
            ]
            coupling = (
                tuple(coupling_values[0:3]),
                tuple(coupling_values[3:6]),
                tuple(coupling_values[6:9]),
            )  # type: ignore[assignment]
            present.update(coupling_fields)
            provenance.update(
                {field: "v2.explicit_axis_coupling" for field in coupling_fields}
            )

        noise = dict(DEFAULT_NOISE)
        if row_values.get("noise_white", "") != "" or row_values.get("noise_walk", "") != "":
            noise["white_noise_std"] = _parse_float(
                row_values.get("noise_white"),
                field="noise_white",
                line=line_number,
                sensor_id=sensor_id,
            )
            noise["bias_random_walk_std"] = _parse_float(
                row_values.get("noise_walk"),
                field="noise_walk",
                line=line_number,
                sensor_id=sensor_id,
            )
            present.update(("noise_white", "noise_walk"))
            provenance["noise_model"] = "v2.explicit_noise_model"

        temperature = {
            "reference_c": DEFAULT_TEMPERATURE["reference_c"],
            "coefficients": DEFAULT_TEMPERATURE["coefficients"],
        }
        temp_fields = tuple(
            item
            for item in V2_OPTIONAL_COLUMNS
            if item.startswith("temp_") and item != "temp_reference_c"
        )
        if row_values.get("temp_reference_c", "") != "" or any(
            row_values.get(field, "") != "" for field in temp_fields
        ):
            temperature = {
                "reference_c": _parse_float(
                    row_values.get("temp_reference_c"),
                    field="temp_reference_c",
                    line=line_number,
                    sensor_id=sensor_id,
                ),
                "coefficients": _matrix3(
                    [
                        [
                            _parse_float(
                                row_values.get(field),
                                field=field,
                                line=line_number,
                                sensor_id=sensor_id,
                            )
                            for field in temp_fields[row * 3 : row * 3 + 3]
                        ]
                        for row in range(3)
                    ],
                    "temperature_compensation.coefficients",
                    sensor_id,
                ),
            }
            present.add("temp_reference_c")
            present.update(temp_fields)
            provenance["temperature_compensation"] = "v2.explicit_temperature"

        descriptions[sensor_id] = _finish_description(
            sensor_id=sensor_id,
            range_limit=range_limit,
            sample_rate=sample_rate,
            axis_order=axis_order,
            calibration_version=calibration_version,
            format_version=2,
            line=line_number,
            bias=bias,
            scale=scale,
            coupling=coupling,
            noise=noise,
            temperature=temperature,
            provenance=provenance,
            present_fields=present,
        )
    if header is None:
        raise _business(ErrorCode.FORMAT_CORRUPT, "v2 document has no header", line=1)
    if not descriptions:
        raise _business(ErrorCode.FORMAT_CORRUPT, "v2 document contains no sensor rows", line=header_line)
    return descriptions


def parse_v3(text: str) -> dict[str, SensorDescription]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _business(
            ErrorCode.FORMAT_CORRUPT,
            f"invalid JSON description: {exc.msg}",
            field="$",
            line=exc.lineno,
        ) from exc
    version_raw = data.get("version", data.get("format_version"))
    try:
        version = int(version_raw)
    except (TypeError, ValueError) as exc:
        raise _business(
            ErrorCode.VERSION_UNSUPPORTED,
            "v3 structured description has no version",
            field="version",
            line=1,
            value=version_raw,
        ) from exc
    if version != 3:
        raise _business(
            ErrorCode.VERSION_UNSUPPORTED,
            f"structured description version v{version} is not handled by the v3 reader",
            field="version",
            line=1,
            value=version,
        )
    sensors = data.get("sensors")
    if not isinstance(sensors, list):
        raise _business(
            ErrorCode.FORMAT_CORRUPT,
            "v3 document requires a sensors array",
            field="sensors",
            line=1,
        )
    descriptions: dict[str, SensorDescription] = {}
    for index, raw in enumerate(sensors):
        if not isinstance(raw, dict):
            raise _business(
                ErrorCode.FORMAT_CORRUPT,
                "each v3 sensor entry must be an object",
                field=f"sensors[{index}]",
                line=1,
            )
        sensor_id = str(raw.get("sensor_id", "")).strip()
        if not sensor_id:
            raise _business(
                ErrorCode.FORMAT_CORRUPT,
                "required field sensor_id is missing",
                field=f"sensors[{index}].sensor_id",
                line=1,
            )
        if sensor_id in descriptions:
            raise _business(
                ErrorCode.FORMAT_CORRUPT,
                f"duplicate sensor_id {sensor_id!r}",
                sensor_id=sensor_id,
                field=f"sensors[{index}].sensor_id",
                line=1,
            )
        for required in ("range", "sample_rate", "axis_order"):
            if required not in raw:
                raise _business(
                    ErrorCode.FORMAT_CORRUPT,
                    f"required field {required} is missing",
                    sensor_id=sensor_id,
                    field=f"sensors[{index}].{required}",
                    line=1,
                )
        range_limit = _parse_float(
            raw.get("range"), field="range", line=1, sensor_id=sensor_id
        )
        sample_rate = _parse_float(
            raw.get("sample_rate"), field="sample_rate", line=1, sensor_id=sensor_id
        )
        axis_order = _split_axis_order(
            raw.get("axis_order"), line=1, sensor_id=sensor_id
        )
        calibration = raw.get("calibration") or {}
        if not isinstance(calibration, dict):
            raise _business(
                ErrorCode.FORMAT_CORRUPT,
                "calibration must be an object",
                sensor_id=sensor_id,
                field=f"sensors[{index}].calibration",
                line=1,
            )
        calibration_raw = calibration.get(
            "version", raw.get("calibration_version")
        )
        if calibration_raw is None:
            raise _business(
                ErrorCode.FORMAT_CORRUPT,
                "required field calibration.version is missing",
                sensor_id=sensor_id,
                field=f"sensors[{index}].calibration.version",
                line=1,
            )
        calibration_version = _parse_int(
            calibration_raw,
            field="calibration.version",
            line=1,
            sensor_id=sensor_id,
        )
        bias: TRIPLE | None = None
        scale: TRIPLE | None = None
        if "bias" in calibration or "scale" in calibration:
            bias = _triple(
                calibration.get("bias"),
                "calibration.bias",
                sensor_id,
            )
            scale = _triple(
                calibration.get("scale"),
                "calibration.scale",
                sensor_id,
            )
        coupling = _matrix3(
            raw.get("axis_coupling", IDENTITY3),
            "axis_coupling",
            sensor_id,
        )
        noise_raw = raw.get("noise_model", DEFAULT_NOISE)
        if not isinstance(noise_raw, dict):
            raise _business(
                ErrorCode.FORMAT_CORRUPT,
                "noise_model must be an object",
                sensor_id=sensor_id,
                field=f"sensors[{index}].noise_model",
                line=1,
            )
        try:
            noise = {str(key): float(value) for key, value in noise_raw.items()}
        except (TypeError, ValueError) as exc:
            raise _business(
                ErrorCode.FORMAT_CORRUPT,
                "noise_model values must be numeric",
                sensor_id=sensor_id,
                field=f"sensors[{index}].noise_model",
                line=1,
            ) from exc
        temp_raw = raw.get("temperature_compensation", DEFAULT_TEMPERATURE)
        if not isinstance(temp_raw, dict):
            raise _business(
                ErrorCode.FORMAT_CORRUPT,
                "temperature_compensation must be an object",
                sensor_id=sensor_id,
                field=f"sensors[{index}].temperature_compensation",
                line=1,
            )
        temperature = {
            "reference_c": float(
                temp_raw.get("reference_c", DEFAULT_TEMPERATURE["reference_c"])
            ),
            "coefficients": _matrix3(
                temp_raw.get("coefficients", DEFAULT_TEMPERATURE["coefficients"]),
                "temperature_compensation.coefficients",
                sensor_id,
            ),
        }
        present = set(raw.keys())
        present.add("calibration_version")
        if calibration:
            present.update(f"calibration.{key}" for key in calibration)
        descriptions[sensor_id] = _finish_description(
            sensor_id=sensor_id,
            range_limit=range_limit,
            sample_rate=sample_rate,
            axis_order=axis_order,
            calibration_version=calibration_version,
            format_version=3,
            line=1,
            bias=bias,
            scale=scale,
            coupling=coupling,
            noise=noise,
            temperature=temperature,
            provenance={
                "axis_coupling": "v3.structured_field",
                "noise_model": "v3.structured_field",
                "temperature_compensation": "v3.structured_field",
            },
            present_fields=present,
        )
    if not descriptions:
        raise _business(ErrorCode.FORMAT_CORRUPT, "v3 document contains no sensors", line=1)
    return descriptions


def parse_document(text: str) -> tuple[int, dict[str, SensorDescription]]:
    """Parse a description document and return ``(version, by_sensor_id)``."""

    version, _ = detect_format(text)
    if version == 1:
        return 1, parse_v1(text)
    if version == 2:
        return 2, parse_v2(text)
    if version == 3:
        return 3, parse_v3(text)
    raise _business(
        ErrorCode.VERSION_UNSUPPORTED,
        f"unsupported description format version v{version}",
        field="format_version",
        line=1,
        value=version,
    )


def parse_description_file(path: str | Path) -> tuple[int, dict[str, SensorDescription]]:
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _business(
            ErrorCode.FORMAT_CORRUPT,
            f"cannot read description file {file_path}: {exc}",
            field="file",
            line=1,
        ) from exc
    return parse_document(text)

