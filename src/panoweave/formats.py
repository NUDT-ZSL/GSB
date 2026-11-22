"""Readers and writers for the v1, v2, and v3 parameter formats."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .config import ParseDefaults
from .errors import BusinessError, ErrorCode, format_error
from .geometry import derive_homography
from .model import (
    DistortionStage,
    ExposureCurve,
    MigrationItem,
    ParsedScene,
    SceneParameters,
    ViewParameters,
    format_number,
    qfloat,
    qtuple,
    validate_scene_structure,
)

SUPPORTED_VERSIONS = ("v1", "v2", "v3")


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for match in re.finditer("\n", text):
        offsets.append(match.end())
    return offsets


def _field_starts(fields: list[str]) -> list[int]:
    starts: list[int] = []
    position = 0
    for field in fields:
        starts.append(position)
        position += len(field) + 1
    return starts


def _parse_float(
    raw: str,
    *,
    field_path: str,
    line: int,
    column: int,
    char_offset: int,
) -> float:
    try:
        value = float(raw.strip())
    except (TypeError, ValueError) as exc:
        raise format_error(
            f"cannot parse number from {raw!r}",
            field_path=field_path,
            line=line,
            column=column,
            char_offset=char_offset,
        ) from exc
    if not math.isfinite(value):
        raise format_error(
            "number must be finite",
            field_path=field_path,
            line=line,
            column=column,
            char_offset=char_offset,
        )
    return qfloat(value)


def _parse_int(
    raw: str,
    *,
    field_path: str,
    line: int,
    column: int,
    char_offset: int,
) -> int:
    try:
        value = int(raw.strip(), 10)
    except (TypeError, ValueError) as exc:
        raise format_error(
            f"cannot parse integer from {raw!r}",
            field_path=field_path,
            line=line,
            column=column,
            char_offset=char_offset,
        ) from exc
    return value


def _split_numbers(
    raw: str,
    *,
    field_path: str,
    expected: int,
    line: int,
    column: int,
    char_offset: int,
) -> tuple[float, ...]:
    parts = [part for part in raw.strip().split(",")] if raw.strip() else []
    if len(parts) != expected:
        raise format_error(
            f"expected {expected} comma-separated numbers, got {len(parts)}",
            field_path=field_path,
            line=line,
            column=column,
            char_offset=char_offset,
            details={"expected": expected, "actual": len(parts)},
        )
    if len(parts) != expected:
        raise format_error(
            "field length mismatch",
            field_path=field_path,
            line=line,
            column=column,
            char_offset=char_offset,
        )
    start_offsets = _field_starts(parts)
    return tuple(
        _parse_float(
            part,
            field_path=f"{field_path}[{index}]",
            line=line,
            column=column + start_offsets[index],
            char_offset=char_offset + start_offsets[index],
        )
        for index, part in enumerate(parts)
    )


def _version_from_header(line: str) -> str | None:
    normalized = line.strip().lower()
    normalized = normalized.lstrip("#").strip()
    match = re.fullmatch(
        r"(?:panoweave[_-]?)?(?:psm[_-]?)?(v[0-9]+)", normalized
    )
    if match:
        return match.group(1)
    match = re.fullmatch(r"version\s*=\s*['\"]?([^'\"\s]+)['\"]?", normalized)
    if match:
        return match.group(1).lower()
    return None


def _detect_version(text: str) -> tuple[str, int]:
    stripped = text.lstrip()
    if not stripped:
        raise BusinessError(
            ErrorCode.VERSION_UNSUPPORTED,
            "empty parameter document",
            stage="parse",
            field_path="version",
        )
    if stripped.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return "v3", 0
        if not isinstance(data, dict) or "version" not in data:
            raise BusinessError(
                ErrorCode.VERSION_UNSUPPORTED,
                "structured document has no version field",
                stage="parse",
                field_path="version",
            )
        version = str(data.get("version", "")).lower()
        if version not in SUPPORTED_VERSIONS:
            raise BusinessError(
                ErrorCode.VERSION_UNSUPPORTED,
                f"unsupported parameter version {version!r}",
                stage="parse",
                field_path="version",
                details={"supported": list(SUPPORTED_VERSIONS)},
            )
        return version, 0
    lines = text.splitlines()
    first_data_line = 0
    offset = 0
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            offset += len(raw_line) + 1
            continue
        if line.startswith("#") or line.lower().startswith("psm-"):
            header_version = _version_from_header(line)
            if header_version is not None:
                if header_version not in SUPPORTED_VERSIONS:
                    raise BusinessError(
                        ErrorCode.VERSION_UNSUPPORTED,
                        f"unsupported parameter version {header_version!r}",
                        stage="parse",
                        field_path="version",
                        line=index + 1,
                        column=1,
                        char_offset=offset,
                        details={"supported": list(SUPPORTED_VERSIONS)},
                    )
                return header_version, index + 1
            continue
        if line.startswith("version=") or line.startswith("version ="):
            header_version = _version_from_header(line)
            if header_version is None or header_version not in SUPPORTED_VERSIONS:
                raise BusinessError(
                    ErrorCode.VERSION_UNSUPPORTED,
                    f"unsupported parameter version in {line!r}",
                    stage="parse",
                    field_path="version",
                    line=index + 1,
                    column=1,
                    char_offset=offset,
                )
            return header_version, index + 1
        first_data_line = index
        break
    else:
        raise BusinessError(
            ErrorCode.VERSION_UNSUPPORTED,
            "no parameter rows found",
            stage="parse",
            field_path="version",
        )
    candidate = lines[first_data_line]
    if "|" in candidate:
        fields = candidate.split("|")
        if len(fields) == 7:
            return "v1", 0
        raise format_error(
            f"v1 row must contain 7 fields, got {len(fields)}",
            field_path="views[0].fields",
            line=first_data_line + 1,
            column=1,
            char_offset=sum(len(line) + 1 for line in lines[:first_data_line]),
            details={"expected": 7, "actual": len(fields), "field_index": 7},
        )
    if "=" in candidate or candidate.startswith("["):
        return "v2", 0
    raise BusinessError(
        ErrorCode.VERSION_UNSUPPORTED,
        "could not infer a supported parameter version",
        stage="parse",
        field_path="version",
        line=first_data_line + 1,
        column=1,
    )


def _make_view(
    *,
    camera_id: int,
    focal: float,
    principal_x: float,
    principal_y: float,
    extrinsic: tuple[float, ...],
    distortion_k1: float,
    distortion_k2: float,
    exposure_gain: float,
    homography: tuple[float, ...],
    overlap_weight: float = 1.0,
    exposure_mode: str = "linear",
    color_gain: tuple[float, float, float] = (1.0, 1.0, 1.0),
    distortion_chain: tuple[DistortionStage, ...] | None = None,
    exposure_curve: ExposureCurve | None = None,
) -> ViewParameters:
    chain = distortion_chain or (DistortionStage.radial(distortion_k1, distortion_k2),)
    curve = exposure_curve or ExposureCurve("linear_gain", exposure_gain, 1.0)
    return ViewParameters(
        camera_id=camera_id,
        focal=qfloat(focal),
        principal_x=qfloat(principal_x),
        principal_y=qfloat(principal_y),
        extrinsic=qtuple(extrinsic),
        distortion_k1=qfloat(distortion_k1),
        distortion_k2=qfloat(distortion_k2),
        exposure_gain=qfloat(exposure_gain),
        homography=qtuple(homography),
        overlap_weight=qfloat(overlap_weight),
        exposure_mode=exposure_mode,
        color_gain=qtuple(color_gain),  # type: ignore[arg-type]
        distortion_chain=chain,
        exposure_curve=curve,
    )


def _parse_v1(text: str, defaults: ParseDefaults, header_line: int) -> ParsedScene:
    line_offsets = _line_offsets(text)
    views: list[ViewParameters] = []
    items: list[MigrationItem] = []
    data_index = 0
    for line_index, raw_line in enumerate(text.splitlines()):
        if line_index < header_line:
            continue
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        line_start = line_offsets[line_index]
        fields = raw_line.split("|")
        starts = _field_starts(fields)
        if len(fields) != 7:
            extra_offset = (
                line_start + starts[7]
                if len(fields) > 7
                else line_start + len(raw_line)
            )
            raise format_error(
                f"v1 row must contain 7 fields, got {len(fields)}",
                field_path=f"views[{data_index}].fields",
                line=line_index + 1,
                column=(starts[7] + 1) if len(fields) > 7 else (len(raw_line) + 1),
                char_offset=extra_offset,
                details={
                    "expected": 7,
                    "actual": len(fields),
                    "field_index": 7 if len(fields) > 7 else len(fields),
                },
            )
        camera_id = _parse_int(
            fields[0],
            field_path=f"views[{data_index}].camera_id",
            line=line_index + 1,
            column=starts[0] + 1,
            char_offset=line_start + starts[0],
        )
        focal = _parse_float(
            fields[1],
            field_path=f"views[{data_index}].focal",
            line=line_index + 1,
            column=starts[1] + 1,
            char_offset=line_start + starts[1],
        )
        principal_x = _parse_float(
            fields[2],
            field_path=f"views[{data_index}].principal_x",
            line=line_index + 1,
            column=starts[2] + 1,
            char_offset=line_start + starts[2],
        )
        principal_y = _parse_float(
            fields[3],
            field_path=f"views[{data_index}].principal_y",
            line=line_index + 1,
            column=starts[3] + 1,
            char_offset=line_start + starts[3],
        )
        extrinsic = _split_numbers(
            fields[4],
            field_path=f"views[{data_index}].extrinsic",
            expected=12,
            line=line_index + 1,
            column=starts[4] + 1,
            char_offset=line_start + starts[4],
        )
        distortion = _split_numbers(
            fields[5],
            field_path=f"views[{data_index}].distortion",
            expected=2,
            line=line_index + 1,
            column=starts[5] + 1,
            char_offset=line_start + starts[5],
        )
        exposure_gain = _parse_float(
            fields[6],
            field_path=f"views[{data_index}].exposure_gain",
            line=line_index + 1,
            column=starts[6] + 1,
            char_offset=line_start + starts[6],
        )
        homography = derive_homography(
            focal,
            principal_x,
            principal_y,
            extrinsic,
            defaults.canvas_width,
            defaults.canvas_height,
        )
        view = _make_view(
            camera_id=camera_id,
            focal=focal,
            principal_x=principal_x,
            principal_y=principal_y,
            extrinsic=extrinsic,
            distortion_k1=distortion[0],
            distortion_k2=distortion[1],
            exposure_gain=exposure_gain,
            homography=homography,
            overlap_weight=defaults.overlap_weight,
            exposure_mode=defaults.exposure_mode,
            color_gain=(1.0, 1.0, 1.0),
        )
        prefix = f"views[{data_index}]"
        items.extend(
            [
                MigrationItem(
                    "filled",
                    f"{prefix}.homography",
                    "derived:v1-extrinsic+intrinsics+canvas",
                    "v1 does not carry a homography",
                    list(homography),
                ),
                MigrationItem(
                    "filled",
                    f"{prefix}.overlap_weight",
                    "default:v1-unspecified",
                    "v1 has no overlap declaration",
                    defaults.overlap_weight,
                ),
                MigrationItem(
                    "filled",
                    f"{prefix}.exposure_mode",
                    "default:v1-scalar-gain",
                    "v1 has only a scalar exposure gain",
                    defaults.exposure_mode,
                ),
                MigrationItem(
                    "filled",
                    f"{prefix}.color_gain",
                    "default:v1-unspecified",
                    "v1 has no per-channel gain",
                    (1.0, 1.0, 1.0),
                ),
                MigrationItem(
                    "filled",
                    f"{prefix}.distortion_chain",
                    "derived:v1-k1-k2",
                    "v1 radial coefficients become one radial stage",
                    [stage.to_dict() for stage in view.distortion_chain],
                ),
                MigrationItem(
                    "filled",
                    f"{prefix}.exposure_curve",
                    "derived:v1-scalar-gain",
                    "v1 scalar gain becomes a linear curve",
                    view.exposure_curve.to_dict(),
                ),
            ]
        )
        views.append(view)
        data_index += 1
    if not views:
        raise format_error(
            "v1 document contains no view rows",
            field_path="views",
            line=max(header_line, 1),
            column=1,
            char_offset=0,
        )
    items.extend(
        [
            MigrationItem(
                "filled",
                "feather_radius",
                "default:parse-context",
                "v1 has no fusion radius",
                defaults.feather_radius,
            ),
            MigrationItem(
                "filled",
                "color_space",
                "default:parse-context",
                "v1 has no color-space marker",
                defaults.color_space,
            ),
        ]
    )
    scene = SceneParameters(
        canvas_width=defaults.canvas_width,
        canvas_height=defaults.canvas_height,
        feather_radius=qfloat(defaults.feather_radius),
        color_space=defaults.color_space,
        views=tuple(views),
        interpolation=defaults.interpolation,
        weight_tolerance=defaults.weight_tolerance,
    )
    validate_scene_structure(scene)
    return ParsedScene(scene, "v1", tuple(items))


def _v2_required_scene() -> set[str]:
    return {"canvas_width", "canvas_height", "feather_radius", "color_space"}


def _v2_required_view() -> set[str]:
    return {
        "camera_id",
        "focal",
        "principal_x",
        "principal_y",
        "extrinsic",
        "homography",
        "distortion_k1",
        "distortion_k2",
        "exposure_gain",
        "exposure_mode",
        "color_gain",
        "overlap_weight",
    }


def _parse_v2(text: str, defaults: ParseDefaults, header_line: int) -> ParsedScene:
    line_offsets = _line_offsets(text)
    scene_values: dict[str, tuple[str, int, int, int]] = {}
    view_values: list[dict[str, tuple[str, int, int, int]]] = []
    current: dict[str, tuple[str, int, int, int]] | None = None
    in_scene = True
    for line_index, raw_line in enumerate(text.splitlines()):
        if line_index < header_line:
            continue
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        line_start = line_offsets[line_index]
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().lower()
            if section == "view":
                current = {}
                view_values.append(current)
                in_scene = False
                continue
            if section == "scene":
                current = None
                in_scene = True
                continue
            raise format_error(
                f"unknown section [{section}]",
                field_path="section",
                line=line_index + 1,
                column=1,
                char_offset=line_start,
            )
        if "=" not in raw_line:
            raise format_error(
                "v2 field must use key=value syntax",
                field_path="field",
                line=line_index + 1,
                column=1,
                char_offset=line_start,
            )
        key, value = raw_line.split("=", 1)
        key = key.strip()
        value_column = raw_line.find("=") + 2
        entry = (value.strip(), line_index + 1, value_column, line_start + value_column - 1)
        target = scene_values if in_scene or current is None else current
        if key in target:
            raise format_error(
                f"duplicate v2 field {key!r}",
                field_path=key,
                line=line_index + 1,
                column=1,
                char_offset=line_start,
            )
        target[key] = entry
    missing_scene = sorted(_v2_required_scene() - set(scene_values))
    if missing_scene:
        raise format_error(
            f"missing required scene field {missing_scene[0]!r}",
            field_path=missing_scene[0],
            line=max(header_line, 1),
            column=1,
            char_offset=0,
            details={"missing_fields": missing_scene},
        )
    if not view_values:
        raise format_error(
            "v2 document contains no [view] blocks",
            field_path="views",
            line=max(header_line, 1),
            column=1,
            char_offset=0,
        )
    width = _parse_int(
        scene_values["canvas_width"][0],
        field_path="canvas_width",
        line=scene_values["canvas_width"][1],
        column=scene_values["canvas_width"][2],
        char_offset=scene_values["canvas_width"][3],
    )
    height = _parse_int(
        scene_values["canvas_height"][0],
        field_path="canvas_height",
        line=scene_values["canvas_height"][1],
        column=scene_values["canvas_height"][2],
        char_offset=scene_values["canvas_height"][3],
    )
    feather = _parse_float(
        scene_values["feather_radius"][0],
        field_path="feather_radius",
        line=scene_values["feather_radius"][1],
        column=scene_values["feather_radius"][2],
        char_offset=scene_values["feather_radius"][3],
    )
    color_space = scene_values["color_space"][0]
    views: list[ViewParameters] = []
    items: list[MigrationItem] = []
    for index, values in enumerate(view_values):
        missing = sorted(_v2_required_view() - set(values))
        if missing:
            raise format_error(
                f"missing required view field {missing[0]!r}",
                field_path=f"views[{index}].{missing[0]}",
                line=max(header_line, 1),
                column=1,
                char_offset=0,
                details={"missing_fields": missing},
            )

        def number(key: str, path: str | None = None) -> float:
            raw, line, column, offset = values[key]
            return _parse_float(
                raw,
                field_path=path or f"views[{index}].{key}",
                line=line,
                column=column,
                char_offset=offset,
            )

        camera_id = _parse_int(
            values["camera_id"][0],
            field_path=f"views[{index}].camera_id",
            line=values["camera_id"][1],
            column=values["camera_id"][2],
            char_offset=values["camera_id"][3],
        )
        extrinsic = _split_numbers(
            values["extrinsic"][0],
            field_path=f"views[{index}].extrinsic",
            expected=12,
            line=values["extrinsic"][1],
            column=values["extrinsic"][2],
            char_offset=values["extrinsic"][3],
        )
        homography = _split_numbers(
            values["homography"][0],
            field_path=f"views[{index}].homography",
            expected=9,
            line=values["homography"][1],
            column=values["homography"][2],
            char_offset=values["homography"][3],
        )
        color_gain_raw = values["color_gain"][0]
        color_gain = _split_numbers(
            color_gain_raw,
            field_path=f"views[{index}].color_gain",
            expected=3,
            line=values["color_gain"][1],
            column=values["color_gain"][2],
            char_offset=values["color_gain"][3],
        )
        mode = values["exposure_mode"][0].strip().lower()
        if mode not in {"linear", "linear_gain", "unit"}:
            raise format_error(
                f"unsupported exposure mode {mode!r}",
                field_path=f"views[{index}].exposure_mode",
                line=values["exposure_mode"][1],
                column=values["exposure_mode"][2],
                char_offset=values["exposure_mode"][3],
            )
        canonical_mode = "unit" if mode == "unit" else "linear"
        k1 = number("distortion_k1")
        k2 = number("distortion_k2")
        gain = number("exposure_gain")
        curve = ExposureCurve("unit", 1.0, 1.0) if canonical_mode == "unit" else ExposureCurve(
            "linear_gain", gain, 1.0
        )
        view = _make_view(
            camera_id=camera_id,
            focal=number("focal"),
            principal_x=number("principal_x"),
            principal_y=number("principal_y"),
            extrinsic=extrinsic,
            distortion_k1=k1,
            distortion_k2=k2,
            exposure_gain=1.0 if canonical_mode == "unit" else gain,
            homography=homography,
            overlap_weight=number("overlap_weight"),
            exposure_mode=canonical_mode,
            color_gain=color_gain,  # type: ignore[arg-type]
            exposure_curve=curve,
        )
        items.extend(
            [
                MigrationItem(
                    "filled",
                    f"views[{index}].distortion_chain",
                    "derived:v2-k1-k2",
                    "v2 radial coefficients become one radial stage",
                    [stage.to_dict() for stage in view.distortion_chain],
                ),
                MigrationItem(
                    "filled",
                    f"views[{index}].exposure_curve",
                    "derived:v2-gain-mode",
                    "v2 scalar gain becomes a linear curve",
                    view.exposure_curve.to_dict(),
                ),
            ]
        )
        views.append(view)
    scene = SceneParameters(
        canvas_width=width,
        canvas_height=height,
        feather_radius=feather,
        color_space=color_space,
        views=tuple(views),
        interpolation=defaults.interpolation,
        weight_tolerance=defaults.weight_tolerance,
    )
    validate_scene_structure(scene)
    return ParsedScene(scene, "v2", tuple(items))


def _json_offset(text: str, key: str, fallback: int = 0) -> int:
    matches = list(re.finditer(rf'"{re.escape(key)}"\s*:', text))
    return matches[-1].start() if matches else fallback


def _require_json(
    data: dict[str, Any],
    key: str,
    *,
    path: str,
    text: str,
    parent_key: str | None = None,
) -> Any:
    if key not in data:
        raise format_error(
            f"missing required field {path!r}",
            field_path=path,
            char_offset=_json_offset(text, key if parent_key is None else parent_key),
        )
    return data[key]


def _json_float(value: Any, *, path: str, text: str, key: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise format_error(
            f"expected number at {path}",
            field_path=path,
            char_offset=_json_offset(text, key),
        ) from exc
    if not math.isfinite(number):
        raise format_error(
            f"number must be finite at {path}",
            field_path=path,
            char_offset=_json_offset(text, key),
        )
    return qfloat(number)


def _json_int(value: Any, *, path: str, text: str, key: str) -> int:
    if isinstance(value, bool):
        raise format_error(
            f"expected integer at {path}",
            field_path=path,
            char_offset=_json_offset(text, key),
        )
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise format_error(
            f"expected integer at {path}",
            field_path=path,
            char_offset=_json_offset(text, key),
        ) from exc


def _json_numbers(
    value: Any,
    *,
    path: str,
    expected: int,
    text: str,
    key: str,
) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != expected:
        actual = len(value) if isinstance(value, list) else None
        raise format_error(
            f"expected {expected} numbers at {path}, got {actual}",
            field_path=path,
            char_offset=_json_offset(text, key),
            details={"expected": expected, "actual": actual},
        )
    return tuple(
        _json_float(item, path=f"{path}[{index}]", text=text, key=key)
        for index, item in enumerate(value)
    )


def _parse_v3(text: str, defaults: ParseDefaults) -> ParsedScene:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise format_error(
            f"invalid JSON: {exc.msg}",
            field_path="json",
            line=exc.lineno,
            column=exc.colno,
            char_offset=exc.pos,
        ) from exc
    if not isinstance(data, dict):
        raise format_error(
            "v3 root must be an object",
            field_path="$",
            char_offset=0,
        )
    version = str(data.get("version", "")).lower()
    if version != "v3":
        raise BusinessError(
            ErrorCode.VERSION_UNSUPPORTED,
            f"unsupported parameter version {version!r}",
            stage="parse",
            field_path="version",
            char_offset=_json_offset(text, "version"),
            details={"supported": list(SUPPORTED_VERSIONS)},
        )
    canvas = _require_json(data, "canvas", path="canvas", text=text)
    if not isinstance(canvas, dict):
        raise format_error(
            "canvas must be an object",
            field_path="canvas",
            char_offset=_json_offset(text, "canvas"),
        )
    width = _json_int(
        _require_json(canvas, "width", path="canvas.width", text=text, parent_key="canvas"),
        path="canvas.width",
        text=text,
        key="width",
    )
    height = _json_int(
        _require_json(canvas, "height", path="canvas.height", text=text, parent_key="canvas"),
        path="canvas.height",
        text=text,
        key="height",
    )
    color_space = str(
        _require_json(canvas, "color_space", path="canvas.color_space", text=text, parent_key="canvas")
    )
    feather = _json_float(
        _require_json(data, "feather_radius", path="feather_radius", text=text),
        path="feather_radius",
        text=text,
        key="feather_radius",
    )
    raw_views = _require_json(data, "views", path="views", text=text)
    if not isinstance(raw_views, list) or not raw_views:
        raise format_error(
            "views must be a non-empty array",
            field_path="views",
            char_offset=_json_offset(text, "views"),
        )
    views: list[ViewParameters] = []
    for index, raw_view in enumerate(raw_views):
        path = f"views[{index}]"
        if not isinstance(raw_view, dict):
            raise format_error(
                "view must be an object",
                field_path=path,
                char_offset=_json_offset(text, "views"),
            )
        intrinsics = _require_json(raw_view, "intrinsics", path=f"{path}.intrinsics", text=text)
        if not isinstance(intrinsics, dict):
            raise format_error(
                "intrinsics must be an object",
                field_path=f"{path}.intrinsics",
                char_offset=_json_offset(text, "intrinsics"),
            )
        focal = _json_float(
            _require_json(
                intrinsics, "focal", path=f"{path}.intrinsics.focal", text=text, parent_key="focal"
            ),
            path=f"{path}.intrinsics.focal",
            text=text,
            key="focal",
        )
        principal = _json_numbers(
            _require_json(
                intrinsics,
                "principal",
                path=f"{path}.intrinsics.principal",
                text=text,
                parent_key="principal",
            ),
            path=f"{path}.intrinsics.principal",
            expected=2,
            text=text,
            key="principal",
        )
        extrinsic = _json_numbers(
            _require_json(raw_view, "extrinsic", path=f"{path}.extrinsic", text=text),
            path=f"{path}.extrinsic",
            expected=12,
            text=text,
            key="extrinsic",
        )
        homography = _json_numbers(
            _require_json(raw_view, "homography", path=f"{path}.homography", text=text),
            path=f"{path}.homography",
            expected=9,
            text=text,
            key="homography",
        )
        raw_chain = _require_json(
            raw_view, "distortion_chain", path=f"{path}.distortion_chain", text=text
        )
        if not isinstance(raw_chain, list) or not raw_chain:
            raise format_error(
                "distortion_chain must be a non-empty array",
                field_path=f"{path}.distortion_chain",
                char_offset=_json_offset(text, "distortion_chain"),
            )
        stages: list[DistortionStage] = []
        for stage_index, raw_stage in enumerate(raw_chain):
            stage_path = f"{path}.distortion_chain[{stage_index}]"
            if not isinstance(raw_stage, dict):
                raise format_error(
                    "distortion stage must be an object",
                    field_path=stage_path,
                    char_offset=_json_offset(text, "distortion_chain"),
                )
            model = str(_require_json(raw_stage, "model", path=f"{stage_path}.model", text=text))
            allowed_params = {
                "radial": {"k1", "k2"},
                "scale": {"sx", "sy"},
                "tangential": {"p1", "p2"},
            }
            if model not in allowed_params:
                raise format_error(
                    f"unsupported distortion model {model!r}",
                    field_path=f"{stage_path}.model",
                    char_offset=_json_offset(text, "model"),
                )
            raw_params = _require_json(
                raw_stage, "params", path=f"{stage_path}.params", text=text
            )
            if not isinstance(raw_params, dict):
                raise format_error(
                    "distortion params must be an object",
                    field_path=f"{stage_path}.params",
                    char_offset=_json_offset(text, "params"),
                )
            params = tuple(
                (
                    str(key),
                    _json_float(value, path=f"{stage_path}.params.{key}", text=text, key=str(key)),
                )
                for key, value in raw_params.items()
                if str(key) in allowed_params[model]
            )
            if {key for key, _ in params} != allowed_params[model]:
                raise format_error(
                    f"distortion model {model!r} requires params {sorted(allowed_params[model])}",
                    field_path=f"{stage_path}.params",
                    char_offset=_json_offset(text, "params"),
                )
            stages.append(DistortionStage(model, params))
        raw_exposure = _require_json(
            raw_view, "exposure", path=f"{path}.exposure", text=text
        )
        if not isinstance(raw_exposure, dict):
            raise format_error(
                "exposure must be an object",
                field_path=f"{path}.exposure",
                char_offset=_json_offset(text, "exposure"),
            )
        curve_data = _require_json(
            raw_exposure, "curve", path=f"{path}.exposure.curve", text=text, parent_key="exposure"
        )
        if not isinstance(curve_data, dict):
            raise format_error(
                "exposure.curve must be an object",
                field_path=f"{path}.exposure.curve",
                char_offset=_json_offset(text, "curve"),
            )
        curve_mode = str(
            _require_json(
                curve_data, "mode", path=f"{path}.exposure.curve.mode", text=text, parent_key="curve"
            )
        )
        if curve_mode not in {"linear_gain", "unit"}:
            raise format_error(
                f"unsupported exposure curve mode {curve_mode!r}",
                field_path=f"{path}.exposure.curve.mode",
                char_offset=_json_offset(text, "mode"),
            )
        curve_gain = _json_float(
            _require_json(
                curve_data, "gain", path=f"{path}.exposure.curve.gain", text=text, parent_key="curve"
            ),
            path=f"{path}.exposure.curve.gain",
            text=text,
            key="gain",
        )
        gamma = _json_float(
            curve_data.get("gamma", 1.0),
            path=f"{path}.exposure.curve.gamma",
            text=text,
            key="gamma",
        )
        extra_params: tuple[tuple[str, float], ...] = ()
        if "params" in curve_data:
            raw_extra = curve_data["params"]
            if not isinstance(raw_extra, dict):
                raise format_error(
                    "exposure.curve.params must be an object",
                    field_path=f"{path}.exposure.curve.params",
                    char_offset=_json_offset(text, "params"),
                )
            extra_params = tuple(
                (
                    str(key),
                    _json_float(value, path=f"{path}.exposure.curve.params.{key}", text=text, key=str(key)),
                )
                for key, value in raw_extra.items()
            )
        color_gain = _json_numbers(
            _require_json(raw_exposure, "color_gain", path=f"{path}.exposure.color_gain", text=text),
            path=f"{path}.exposure.color_gain",
            expected=3,
            text=text,
            key="color_gain",
        )
        overlap_weight = _json_float(
            _require_json(
                raw_view, "overlap_weight", path=f"{path}.overlap_weight", text=text
            ),
            path=f"{path}.overlap_weight",
            text=text,
            key="overlap_weight",
        )
        camera_id = _json_int(
            _require_json(raw_view, "camera_id", path=f"{path}.camera_id", text=text),
            path=f"{path}.camera_id",
            text=text,
            key="camera_id",
        )
        radial = next((stage for stage in stages if stage.model == "radial"), None)
        k1 = radial.param("k1") if radial else 0.0
        k2 = radial.param("k2") if radial else 0.0
        exposure_curve = ExposureCurve(curve_mode, curve_gain, gamma, extra_params)
        view = _make_view(
            camera_id=camera_id,
            focal=focal,
            principal_x=principal[0],
            principal_y=principal[1],
            extrinsic=extrinsic,
            distortion_k1=k1,
            distortion_k2=k2,
            exposure_gain=curve_gain,
            homography=homography,
            overlap_weight=overlap_weight,
            exposure_mode="linear" if curve_mode == "linear_gain" else "unit",
            color_gain=color_gain,  # type: ignore[arg-type]
            distortion_chain=tuple(stages),
            exposure_curve=exposure_curve,
        )
        views.append(view)
    scene = SceneParameters(
        canvas_width=width,
        canvas_height=height,
        feather_radius=feather,
        color_space=color_space,
        views=tuple(views),
        interpolation=defaults.interpolation,
        weight_tolerance=defaults.weight_tolerance,
    )
    validate_scene_structure(scene)
    return ParsedScene(scene, "v3", ())


def parse_text(
    text: str,
    defaults: ParseDefaults | None = None,
    *,
    source_name: str = "<memory>",
) -> ParsedScene:
    defaults = defaults or ParseDefaults()
    version, header_line = _detect_version(text)
    try:
        if version == "v1":
            return _parse_v1(text, defaults, header_line)
        if version == "v2":
            return _parse_v2(text, defaults, header_line)
        if version == "v3":
            return _parse_v3(text, defaults)
    except BusinessError:
        raise
    except Exception as exc:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            f"unexpected parse failure: {exc}",
            stage="parse",
            field_path="document",
            details={"source": source_name},
        ) from exc
    raise BusinessError(
        ErrorCode.VERSION_UNSUPPORTED,
        f"unsupported parameter version {version!r}",
        stage="parse",
        field_path="version",
    )


def parse_file(
    path: str | Path,
    defaults: ParseDefaults | None = None,
) -> ParsedScene:
    source = Path(path)
    return parse_text(source.read_text(encoding="utf-8"), defaults, source_name=str(source))


def _v1_line(view: ViewParameters) -> str:
    return "|".join(
        [
            str(view.camera_id),
            format_number(view.focal),
            format_number(view.principal_x),
            format_number(view.principal_y),
            ",".join(format_number(value) for value in view.extrinsic),
            ",".join(
                [format_number(view.distortion_k1), format_number(view.distortion_k2)]
            ),
            format_number(view.exposure_gain),
        ]
    )


def serialize_v1(scene: SceneParameters) -> str:
    lines = ["# PSM-V1"]
    lines.extend(_v1_line(view) for view in scene.sorted_views)
    return "\n".join(lines) + "\n"


def serialize_v2(scene: SceneParameters) -> str:
    lines = [
        "# PSM-V2",
        f"canvas_width={scene.canvas_width}",
        f"canvas_height={scene.canvas_height}",
        f"feather_radius={format_number(scene.feather_radius)}",
        f"color_space={scene.color_space}",
    ]
    for view in scene.sorted_views:
        lines.extend(
            [
                "[view]",
                f"camera_id={view.camera_id}",
                f"focal={format_number(view.focal)}",
                f"principal_x={format_number(view.principal_x)}",
                f"principal_y={format_number(view.principal_y)}",
                "extrinsic=" + ",".join(format_number(value) for value in view.extrinsic),
                "homography=" + ",".join(format_number(value) for value in view.homography),
                f"distortion_k1={format_number(view.distortion_k1)}",
                f"distortion_k2={format_number(view.distortion_k2)}",
                f"exposure_gain={format_number(view.exposure_gain)}",
                f"exposure_mode={view.exposure_mode}",
                "color_gain=" + ",".join(format_number(value) for value in view.color_gain),
                f"overlap_weight={format_number(view.overlap_weight)}",
            ]
        )
    return "\n".join(lines) + "\n"


def _v3_view_dict(view: ViewParameters) -> dict[str, Any]:
    return {
        "camera_id": view.camera_id,
        "intrinsics": {
            "focal": view.focal,
            "principal": [view.principal_x, view.principal_y],
        },
        "extrinsic": list(view.extrinsic),
        "homography": list(view.homography),
        "overlap_weight": view.overlap_weight,
        "distortion_chain": [stage.to_dict() for stage in view.distortion_chain],
        "exposure": {
            "curve": view.exposure_curve.to_dict(),
            "color_gain": list(view.color_gain),
        },
    }


def serialize_v3(scene: SceneParameters) -> str:
    data = {
        "version": "v3",
        "canvas": {
            "width": scene.canvas_width,
            "height": scene.canvas_height,
            "color_space": scene.color_space,
        },
        "feather_radius": scene.feather_radius,
        "views": [_v3_view_dict(view) for view in scene.sorted_views],
    }
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def serialize_scene(scene: SceneParameters, version: str) -> str:
    if version == "v1":
        return serialize_v1(scene)
    if version == "v2":
        return serialize_v2(scene)
    if version == "v3":
        return serialize_v3(scene)
    raise BusinessError(
        ErrorCode.VERSION_UNSUPPORTED,
        f"unsupported target version {version!r}",
        stage="migration",
        field_path="target_version",
        details={"supported": list(SUPPORTED_VERSIONS)},
    )


def write_scene(path: str | Path, scene: SceneParameters, version: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(serialize_scene(scene, version), encoding="utf-8", newline="\n")


def to_internal_view(raw: dict[str, Any]) -> ViewParameters:
    """Test/helper hook for building a canonical view from primitive data."""

    chain = tuple(
        DistortionStage(str(item["model"]), tuple((str(k), qfloat(v)) for k, v in item["params"].items()))
        for item in raw.get("distortion_chain", [])
    )
    curve_data = raw.get("exposure_curve", {})
    curve = ExposureCurve(
        str(curve_data.get("mode", "linear_gain")),
        qfloat(curve_data.get("gain", raw["exposure_gain"])),
        qfloat(curve_data.get("gamma", 1.0)),
        tuple((str(k), qfloat(v)) for k, v in curve_data.get("params", {}).items()),
    )
    return _make_view(
        camera_id=int(raw["camera_id"]),
        focal=float(raw["focal"]),
        principal_x=float(raw["principal_x"]),
        principal_y=float(raw["principal_y"]),
        extrinsic=tuple(float(value) for value in raw["extrinsic"]),
        distortion_k1=float(raw["distortion_k1"]),
        distortion_k2=float(raw["distortion_k2"]),
        exposure_gain=float(raw["exposure_gain"]),
        homography=tuple(float(value) for value in raw["homography"]),
        overlap_weight=float(raw.get("overlap_weight", 1.0)),
        exposure_mode=str(raw.get("exposure_mode", "linear")),
        color_gain=tuple(float(value) for value in raw.get("color_gain", [1, 1, 1])),
        distortion_chain=chain or None,
        exposure_curve=curve,
    )

