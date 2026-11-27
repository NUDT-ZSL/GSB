from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .errors import ProjectValidationError, ValidationIssue
from .geometry import dot, norm, subtract
from .model import (
    AcousticSample,
    AssessmentConfig,
    Furniture,
    Project,
    Relationship,
    Room,
    Screen,
    StereoConfig,
    Vec2,
    Vec3,
    ViewingPose,
)
from .utils import fingerprint_json

SCHEMA_VERSION = 1
_MAX_ROOM_DIMENSION_M = 1000.0
_MAX_SCREEN_DIMENSION_M = 100.0
_VECTOR_TOLERANCE = 1e-6
_BOUNDS_TOLERANCE = 1e-7


class _Validator:
    def __init__(self) -> None:
        self.issues: list[ValidationIssue] = []

    def add(self, path: str, code: str, message: str) -> None:
        self.issues.append(ValidationIssue(path=path, code=code, message=message))

    def required(
        self,
        mapping: dict[str, Any],
        key: str,
        path: str,
        code: str = "E_REQUIRED",
    ) -> bool:
        if key not in mapping:
            self.add(
                f"{path}.{key}",
                code,
                f"required field '{key}' is missing",
            )
            return False
        return True

    def string(
        self,
        mapping: dict[str, Any],
        key: str,
        path: str,
        *,
        required: bool = True,
        allow_empty: bool = False,
    ) -> str | None:
        if key not in mapping:
            if required:
                self.add(
                    f"{path}.{key}",
                    "E_REQUIRED",
                    f"required field '{key}' is missing",
                )
            return None
        value = mapping[key]
        if not isinstance(value, str):
            self.add(
                f"{path}.{key}",
                "E_TYPE",
                f"'{key}' must be a string",
            )
            return None
        if not allow_empty and not value.strip():
            self.add(
                f"{path}.{key}",
                "E_EMPTY",
                f"'{key}' must not be empty",
            )
            return None
        return value

    def number(
        self,
        mapping: dict[str, Any],
        key: str,
        path: str,
        *,
        required: bool = True,
        minimum: float | None = None,
        maximum: float | None = None,
        strictly_positive: bool = False,
    ) -> float | None:
        field_path = f"{path}.{key}"
        if key not in mapping:
            if required:
                self.add(
                    field_path,
                    "E_REQUIRED",
                    f"required field '{key}' is missing",
                )
            return None
        value = mapping[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.add(
                field_path,
                "E_TYPE",
                f"'{key}' must be a number",
            )
            return None
        number = float(value)
        if not math.isfinite(number):
            self.add(
                field_path,
                "E_NON_FINITE",
                f"'{key}' must be finite",
            )
            return None
        if strictly_positive and number <= 0.0:
            self.add(
                field_path,
                "E_RANGE",
                f"'{key}' must be greater than 0",
            )
            return None
        if minimum is not None and number < minimum:
            self.add(
                field_path,
                "E_RANGE",
                f"'{key}' must be >= {minimum}",
            )
            return None
        if maximum is not None and number > maximum:
            self.add(
                field_path,
                "E_RANGE",
                f"'{key}' must be <= {maximum}",
            )
            return None
        return number

    def integer(
        self,
        mapping: dict[str, Any],
        key: str,
        path: str,
        *,
        required: bool = True,
        minimum: int | None = None,
    ) -> int | None:
        field_path = f"{path}.{key}"
        if key not in mapping:
            if required:
                self.add(
                    field_path,
                    "E_REQUIRED",
                    f"required field '{key}' is missing",
                )
            return None
        value = mapping[key]
        if isinstance(value, bool) or not isinstance(value, int):
            self.add(
                field_path,
                "E_TYPE",
                f"'{key}' must be an integer",
            )
            return None
        if minimum is not None and value < minimum:
            self.add(
                field_path,
                "E_RANGE",
                f"'{key}' must be >= {minimum}",
            )
            return None
        return value

    def object(
        self,
        mapping: dict[str, Any],
        key: str,
        path: str,
        *,
        required: bool = True,
    ) -> dict[str, Any] | None:
        if key not in mapping:
            if required:
                self.add(
                    f"{path}.{key}",
                    "E_REQUIRED",
                    f"required object '{key}' is missing",
                )
            return None
        value = mapping[key]
        if not isinstance(value, dict):
            self.add(
                f"{path}.{key}",
                "E_TYPE",
                f"'{key}' must be an object",
            )
            return None
        return value

    def array(
        self,
        mapping: dict[str, Any],
        key: str,
        path: str,
        *,
        required: bool = False,
    ) -> list[Any] | None:
        if key not in mapping:
            if required:
                self.add(
                    f"{path}.{key}",
                    "E_REQUIRED",
                    f"required array '{key}' is missing",
                )
            return None
        value = mapping[key]
        if not isinstance(value, list):
            self.add(
                f"{path}.{key}",
                "E_TYPE",
                f"'{key}' must be an array",
            )
            return None
        return value

    def vector3(
        self,
        mapping: dict[str, Any],
        key: str,
        path: str,
        *,
        required: bool = True,
    ) -> Vec3 | None:
        field_path = f"{path}.{key}"
        value = self.array(
            mapping, key, path, required=required
        )
        if value is None:
            return None
        if len(value) != 3:
            self.add(
                field_path,
                "E_LENGTH",
                f"'{key}' must contain exactly 3 numbers",
            )
            return None
        result: list[float] = []
        valid = True
        for index, component in enumerate(value):
            component_path = f"{field_path}[{index}]"
            if isinstance(component, bool) or not isinstance(
                component, (int, float)
            ):
                self.add(
                    component_path,
                    "E_TYPE",
                    "vector component must be a number",
                )
                valid = False
                continue
            number = float(component)
            if not math.isfinite(number):
                self.add(
                    component_path,
                    "E_NON_FINITE",
                    "vector component must be finite",
                )
                valid = False
                continue
            result.append(number)
        if not valid or len(result) != 3:
            return None
        return (result[0], result[1], result[2])

    def vector2(
        self,
        mapping: dict[str, Any],
        key: str,
        path: str,
        *,
        required: bool = True,
    ) -> Vec2 | None:
        field_path = f"{path}.{key}"
        value = self.array(
            mapping, key, path, required=required
        )
        if value is None:
            return None
        if len(value) != 2:
            self.add(
                field_path,
                "E_LENGTH",
                f"'{key}' must contain exactly 2 numbers",
            )
            return None
        result: list[float] = []
        for index, component in enumerate(value):
            component_path = f"{field_path}[{index}]"
            if isinstance(component, bool) or not isinstance(
                component, (int, float)
            ):
                self.add(
                    component_path,
                    "E_TYPE",
                    "vector component must be a number",
                )
                return None
            number = float(component)
            if not math.isfinite(number):
                self.add(
                    component_path,
                    "E_NON_FINITE",
                    "vector component must be finite",
                )
                return None
            result.append(number)
        return (result[0], result[1])


def _inside_room(point: Vec3, room: Room) -> bool:
    return all(
        room.minimum[axis] - _BOUNDS_TOLERANCE
        <= point[axis]
        <= room.maximum[axis] + _BOUNDS_TOLERANCE
        for axis in range(3)
    )


def _parse_room(validator: _Validator, value: Any) -> Room | None:
    if not isinstance(value, dict):
        validator.add("$.room", "E_TYPE", "'room' must be an object")
        return None
    width = validator.number(
        value,
        "width_m",
        "$.room",
        strictly_positive=True,
        maximum=_MAX_ROOM_DIMENSION_M,
    )
    depth = validator.number(
        value,
        "depth_m",
        "$.room",
        strictly_positive=True,
        maximum=_MAX_ROOM_DIMENSION_M,
    )
    height = validator.number(
        value,
        "height_m",
        "$.room",
        strictly_positive=True,
        maximum=_MAX_ROOM_DIMENSION_M,
    )
    if width is None or depth is None or height is None:
        return None
    return Room(width_m=width, depth_m=depth, height_m=height)


def _unit_vector(
    validator: _Validator,
    value: Vec3,
    path: str,
    label: str,
) -> bool:
    length = norm(value)
    if abs(length - 1.0) > _VECTOR_TOLERANCE:
        validator.add(
            path,
            "E_UNIT_VECTOR",
            f"{label} must have unit length (got {length:.9f})",
        )
        return False
    return True


def _parse_screen(
    validator: _Validator,
    value: Any,
    room: Room | None,
) -> Screen | None:
    if not isinstance(value, dict):
        validator.add("$.screen", "E_TYPE", "'screen' must be an object")
        return None
    screen_id = validator.string(value, "id", "$.screen")
    center = validator.vector3(value, "center_m", "$.screen")
    width = validator.number(
        value,
        "width_m",
        "$.screen",
        strictly_positive=True,
        maximum=_MAX_SCREEN_DIMENSION_M,
    )
    height = validator.number(
        value,
        "height_m",
        "$.screen",
        strictly_positive=True,
        maximum=_MAX_SCREEN_DIMENSION_M,
    )
    design_distance = validator.number(
        value,
        "design_viewing_distance_m",
        "$.screen",
        strictly_positive=True,
    )
    zero_parallax = validator.number(
        value,
        "zero_parallax_distance_m",
        "$.screen",
        strictly_positive=True,
    )

    resolution_object = validator.object(
        value, "resolution", "$.screen"
    )
    resolution_width = resolution_height = None
    if resolution_object is not None:
        resolution_width = validator.integer(
            resolution_object,
            "width_px",
            "$.screen.resolution",
            minimum=1,
        )
        resolution_height = validator.integer(
            resolution_object,
            "height_px",
            "$.screen.resolution",
            minimum=1,
        )

    blocks_object = validator.object(value, "blocks", "$.screen")
    block_cols = block_rows = None
    if blocks_object is not None:
        block_cols = validator.integer(
            blocks_object, "cols", "$.screen.blocks", minimum=1
        )
        block_rows = validator.integer(
            blocks_object, "rows", "$.screen.blocks", minimum=1
        )

    per_block_object = validator.object(
        value, "samples_per_block", "$.screen"
    )
    per_block_cols = per_block_rows = None
    if per_block_object is not None:
        per_block_cols = validator.integer(
            per_block_object,
            "cols",
            "$.screen.samples_per_block",
            minimum=1,
        )
        per_block_rows = validator.integer(
            per_block_object,
            "rows",
            "$.screen.samples_per_block",
            minimum=1,
        )

    right = validator.vector3(value, "right", "$.screen")
    up = validator.vector3(value, "up", "$.screen")
    normal = validator.vector3(value, "normal", "$.screen")
    basis_valid = True
    for label, vector in (("right", right), ("up", up), ("normal", normal)):
        if vector is not None and not _unit_vector(
            validator, vector, f"$.screen.{label}", f"screen {label}"
        ):
            basis_valid = False
    if right is not None and up is not None and normal is not None:
        if abs(dot(right, up)) > 1e-5:
            validator.add(
                "$.screen",
                "E_SCREEN_BASIS",
                "screen right and up vectors must be orthogonal",
            )
            basis_valid = False
        if abs(dot(right, normal)) > 1e-5 or abs(dot(up, normal)) > 1e-5:
            validator.add(
                "$.screen",
                "E_SCREEN_BASIS",
                "screen normal must be orthogonal to right and up",
            )
            basis_valid = False
        cross_value = (
            right[1] * up[2] - right[2] * up[1],
            right[2] * up[0] - right[0] * up[2],
            right[0] * up[1] - right[1] * up[0],
        )
        if dot(cross_value, normal) < 1.0 - 1e-5:
            validator.add(
                "$.screen",
                "E_SCREEN_BASIS",
                "screen basis must satisfy normal = right x up",
            )
            basis_valid = False

    if (
        resolution_width is not None
        and resolution_height is not None
        and block_cols is not None
        and block_rows is not None
    ):
        if resolution_width % block_cols != 0:
            validator.add(
                "$.screen.blocks.cols",
                "E_GRID_RESOLUTION_MISMATCH",
                "horizontal resolution must be divisible by block columns",
            )
        if resolution_height % block_rows != 0:
            validator.add(
                "$.screen.blocks.rows",
                "E_GRID_RESOLUTION_MISMATCH",
                "vertical resolution must be divisible by block rows",
            )

    if (
        resolution_width is not None
        and resolution_height is not None
        and block_cols is not None
        and block_rows is not None
        and per_block_cols is not None
        and per_block_rows is not None
    ):
        sample_cols = block_cols * per_block_cols
        sample_rows = block_rows * per_block_rows
        if sample_cols > resolution_width:
            validator.add(
                "$.screen.samples_per_block.cols",
                "E_SAMPLE_RESOLUTION_MISMATCH",
                "horizontal sample grid exceeds display resolution",
            )
        if sample_rows > resolution_height:
            validator.add(
                "$.screen.samples_per_block.rows",
                "E_SAMPLE_RESOLUTION_MISMATCH",
                "vertical sample grid exceeds display resolution",
            )

    if (
        center is not None
        and width is not None
        and height is not None
        and right is not None
        and up is not None
        and room is not None
    ):
        half_width = width / 2.0
        half_height = height / 2.0
        corners = (
            tuple(
                center[axis]
                - half_width * right[axis]
                - half_height * up[axis]
                for axis in range(3)
            ),
            tuple(
                center[axis]
                - half_width * right[axis]
                + half_height * up[axis]
                for axis in range(3)
            ),
            tuple(
                center[axis]
                + half_width * right[axis]
                - half_height * up[axis]
                for axis in range(3)
            ),
            tuple(
                center[axis]
                + half_width * right[axis]
                + half_height * up[axis]
                for axis in range(3)
            ),
        )
        if not _inside_room(center, room) or any(
            not _inside_room(corner, room) for corner in corners
        ):
            validator.add(
                "$.screen.center_m",
                "E_SCREEN_ROOM_BOUNDS",
                "screen must fit completely inside the room",
            )
        if normal is not None:
            inward = subtract(room.center, center)
            if dot(inward, normal) <= 0.0:
                validator.add(
                    "$.screen.normal",
                    "E_SCREEN_NORMAL_ORIENTATION",
                    "screen normal must point from the display into the room",
                )

    if (
        screen_id is None
        or center is None
        or width is None
        or height is None
        or design_distance is None
        or zero_parallax is None
        or resolution_width is None
        or resolution_height is None
        or block_cols is None
        or block_rows is None
        or per_block_cols is None
        or per_block_rows is None
        or right is None
        or up is None
        or normal is None
        or not basis_valid
    ):
        return None
    return Screen(
        id=screen_id,
        center=center,
        width_m=width,
        height_m=height,
        resolution=(resolution_width, resolution_height),
        blocks=(block_cols, block_rows),
        samples_per_block=(per_block_cols, per_block_rows),
        right=right,
        up=up,
        normal=normal,
        design_viewing_distance_m=design_distance,
        zero_parallax_distance_m=zero_parallax,
    )


def _parse_stereo(
    validator: _Validator, value: Any
) -> StereoConfig | None:
    if not isinstance(value, dict):
        validator.add("$.stereo", "E_TYPE", "'stereo' must be an object")
        return None
    ipd = validator.number(
        value,
        "interocular_distance_m",
        "$.stereo",
        minimum=0.01,
        maximum=0.2,
    )
    if ipd is None:
        return None
    return StereoConfig(interocular_distance_m=ipd)


def _parse_assessment(
    validator: _Validator, value: Any
) -> AssessmentConfig | None:
    if not isinstance(value, dict):
        validator.add(
            "$.assessment", "E_TYPE", "'assessment' must be an object"
        )
        return None
    comfort = validator.object(value, "comfort_parallax_px", "$.assessment")
    comfort_min = comfort_max = None
    if comfort is not None:
        comfort_min = validator.number(
            comfort, "min", "$.assessment.comfort_parallax_px"
        )
        comfort_max = validator.number(
            comfort, "max", "$.assessment.comfort_parallax_px"
        )
        if (
            comfort_min is not None
            and comfort_max is not None
            and not (comfort_min < comfort_max)
        ):
            validator.add(
                "$.assessment.comfort_parallax_px",
                "E_COMFORT_RANGE",
                "comfort minimum must be strictly less than maximum",
            )
        if (
            comfort_min is not None
            and comfort_max is not None
            and not (comfort_min <= 0.0 <= comfort_max)
        ):
            validator.add(
                "$.assessment.comfort_parallax_px",
                "E_COMFORT_RANGE",
                "comfort interval must contain zero parallax",
            )
    depth_threshold = validator.number(
        value,
        "depth_distortion_threshold",
        "$.assessment",
        minimum=0.0,
        maximum=0.99,
    )

    weights = validator.object(value, "score_weights", "$.assessment")
    ratio_weight = magnitude_weight = None
    if weights is not None:
        ratio_weight = validator.number(
            weights,
            "overflow_ratio",
            "$.assessment.score_weights",
            minimum=0.0,
            maximum=1.0,
        )
        magnitude_weight = validator.number(
            weights,
            "overflow_magnitude",
            "$.assessment.score_weights",
            minimum=0.0,
            maximum=1.0,
        )
        if (
            ratio_weight is not None
            and magnitude_weight is not None
            and abs(ratio_weight + magnitude_weight - 1.0) > 1e-6
        ):
            validator.add(
                "$.assessment.score_weights",
                "E_SCORE_WEIGHTS",
                "score weights must sum to 1",
            )
            ratio_weight = magnitude_weight = None

    min_ratio = validator.number(
        value,
        "min_comfortable_block_ratio",
        "$.assessment",
        minimum=0.0,
        maximum=1.0,
    )
    zone_radius = validator.number(
        value,
        "viewing_zone_radius_m",
        "$.assessment",
        strictly_positive=True,
    )
    if (
        comfort_min is None
        or comfort_max is None
        or depth_threshold is None
        or ratio_weight is None
        or magnitude_weight is None
        or min_ratio is None
        or zone_radius is None
    ):
        return None
    return AssessmentConfig(
        comfort_parallax_min_px=comfort_min,
        comfort_parallax_max_px=comfort_max,
        depth_distortion_threshold=depth_threshold,
        overflow_ratio_weight=ratio_weight,
        overflow_magnitude_weight=magnitude_weight,
        min_comfortable_block_ratio=min_ratio,
        viewing_zone_radius_m=zone_radius,
    )


def _parse_poses(
    validator: _Validator,
    value: Any,
    room: Room | None,
) -> tuple[ViewingPose, ...]:
    if not isinstance(value, list):
        validator.add(
            "$.viewing_poses", "E_TYPE", "'viewing_poses' must be an array"
        )
        return ()
    if not value:
        validator.add(
            "$.viewing_poses",
            "E_EMPTY",
            "at least one viewing pose is required",
        )
    poses: list[ViewingPose] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        path = f"$.viewing_poses[{index}]"
        if not isinstance(item, dict):
            validator.add(path, "E_TYPE", "viewing pose must be an object")
            continue
        pose_id = validator.string(item, "id", path)
        position = validator.vector3(item, "position_m", path)
        forward = validator.vector3(item, "forward", path)
        up = validator.vector3(item, "up", path)
        furniture_id = validator.string(item, "furniture_id", path)
        if pose_id is not None:
            if pose_id in seen:
                validator.add(
                    f"{path}.id",
                    "E_DUPLICATE_ID",
                    f"duplicate viewing pose id '{pose_id}'",
                )
                pose_id = None
            else:
                seen.add(pose_id)
        if position is not None and room is not None:
            if not _inside_room(position, room):
                validator.add(
                    f"{path}.position_m",
                    "E_POSITION_BOUNDS",
                    "viewing pose must be inside the room",
                )
                position = None
        if forward is not None and not _unit_vector(
            validator, forward, f"{path}.forward", "viewing forward"
        ):
            forward = None
        if up is not None and not _unit_vector(
            validator, up, f"{path}.up", "viewing up"
        ):
            up = None
        if forward is not None and up is not None:
            if abs(dot(forward, up)) > 1e-5:
                validator.add(
                    path,
                    "E_POSE_BASIS",
                    "viewing forward and up must be orthogonal",
                )
                forward = None
        if (
            pose_id is not None
            and position is not None
            and forward is not None
            and up is not None
            and furniture_id is not None
        ):
            poses.append(
                ViewingPose(
                    id=pose_id,
                    position=position,
                    forward=forward,
                    up=up,
                    furniture_id=furniture_id,
                )
            )
    return tuple(poses)


def _parse_furniture(
    validator: _Validator,
    value: Any,
    room: Room | None,
) -> tuple[Furniture, ...]:
    if not isinstance(value, list):
        validator.add(
            "$.furniture", "E_TYPE", "'furniture' must be an array"
        )
        return ()
    if not value:
        validator.add(
            "$.furniture",
            "E_EMPTY",
            "at least one furniture item is required",
        )
    result: list[Furniture] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        path = f"$.furniture[{index}]"
        if not isinstance(item, dict):
            validator.add(path, "E_TYPE", "furniture must be an object")
            continue
        furniture_id = validator.string(item, "id", path)
        kind = validator.string(item, "kind", path)
        bounds = validator.object(item, "bounds_m", path)
        minimum = maximum = None
        if bounds is not None:
            minimum = validator.vector3(bounds, "min", f"{path}.bounds_m")
            maximum = validator.vector3(bounds, "max", f"{path}.bounds_m")
        if furniture_id is not None:
            if furniture_id in seen:
                validator.add(
                    f"{path}.id",
                    "E_DUPLICATE_ID",
                    f"duplicate furniture id '{furniture_id}'",
                )
                furniture_id = None
            else:
                seen.add(furniture_id)
        if minimum is not None and maximum is not None:
            if any(
                minimum[axis] >= maximum[axis] for axis in range(3)
            ):
                validator.add(
                    f"{path}.bounds_m",
                    "E_BOUNDS_ORDER",
                    "furniture minimum must be strictly below maximum",
                )
                minimum = maximum = None
            elif room is not None and (
                not _inside_room(minimum, room)
                or not _inside_room(maximum, room)
            ):
                validator.add(
                    f"{path}.bounds_m",
                    "E_FURNITURE_BOUNDS",
                    "furniture bounds must lie inside the room",
                )
                minimum = maximum = None
        if (
            furniture_id is not None
            and kind is not None
            and minimum is not None
            and maximum is not None
        ):
            result.append(
                Furniture(
                    id=furniture_id,
                    kind=kind,
                    minimum=minimum,
                    maximum=maximum,
                )
            )
    return tuple(result)


def _parse_relationships(
    validator: _Validator, value: Any
) -> tuple[Relationship, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        validator.add(
            "$.relationships",
            "E_TYPE",
            "'relationships' must be an array",
        )
        return ()
    result: list[Relationship] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        path = f"$.relationships[{index}]"
        if not isinstance(item, dict):
            validator.add(path, "E_TYPE", "relationship must be an object")
            continue
        relationship_id = validator.string(item, "id", path)
        from_ref = validator.string(item, "from", path)
        to_ref = validator.string(item, "to", path)
        if relationship_id is not None:
            if relationship_id in seen:
                validator.add(
                    f"{path}.id",
                    "E_DUPLICATE_ID",
                    f"duplicate relationship id '{relationship_id}'",
                )
                relationship_id = None
            else:
                seen.add(relationship_id)
        if from_ref is not None and to_ref is not None:
            if from_ref == to_ref:
                validator.add(
                    path,
                    "E_RELATIONSHIP_SELF",
                    "relationship endpoints must differ",
                )
            elif relationship_id is not None:
                result.append(
                    Relationship(
                        id=relationship_id,
                        from_ref=from_ref,
                        to_ref=to_ref,
                    )
                )
    return tuple(result)


def _parse_acoustic_samples(
    validator: _Validator,
    value: Any,
    room: Room | None,
) -> tuple[AcousticSample, ...]:
    if not isinstance(value, list):
        validator.add(
            "$.acoustic_samples",
            "E_TYPE",
            "'acoustic_samples' must be an array",
        )
        return ()
    if not value:
        validator.add(
            "$.acoustic_samples",
            "E_EMPTY",
            "at least one acoustic sample is required",
        )
    result: list[AcousticSample] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        path = f"$.acoustic_samples[{index}]"
        if not isinstance(item, dict):
            validator.add(
                path, "E_TYPE", "acoustic sample must be an object"
            )
            continue
        sample_id = validator.string(item, "id", path)
        position = validator.vector2(item, "position_m", path)
        radius = validator.number(
            item,
            "coverage_radius_m",
            path,
            strictly_positive=True,
        )
        pose_id_value = item.get("viewing_pose_id")
        pose_id: str | None = None
        if pose_id_value is not None:
            if not isinstance(pose_id_value, str) or not pose_id_value.strip():
                validator.add(
                    f"{path}.viewing_pose_id",
                    "E_TYPE",
                    "'viewing_pose_id' must be a non-empty string when present",
                )
            else:
                pose_id = pose_id_value
        if sample_id is not None:
            if sample_id in seen:
                validator.add(
                    f"{path}.id",
                    "E_DUPLICATE_ID",
                    f"duplicate acoustic sample id '{sample_id}'",
                )
                sample_id = None
            else:
                seen.add(sample_id)
        if position is not None and room is not None:
            if not (
                0.0 - _BOUNDS_TOLERANCE <= position[0] <= room.width_m
                + _BOUNDS_TOLERANCE
                and 0.0 - _BOUNDS_TOLERANCE
                <= position[1]
                <= room.depth_m + _BOUNDS_TOLERANCE
            ):
                validator.add(
                    f"{path}.position_m",
                    "E_POSITION_BOUNDS",
                    "acoustic sample must lie on the room floor plan",
                )
                position = None
        if sample_id is not None and position is not None and radius is not None:
            result.append(
                AcousticSample(
                    id=sample_id,
                    position=position,
                    coverage_radius_m=radius,
                    viewing_pose_id=pose_id,
                )
            )
    return tuple(result)


def validate_project_data(
    data: Any,
) -> tuple[list[ValidationIssue], Project | None]:
    validator = _Validator()
    if not isinstance(data, dict):
        validator.add("$", "E_ROOT_TYPE", "project root must be an object")
        return validator.issues, None

    schema_version = validator.integer(
        data, "schema_version", "$", minimum=1
    )
    if schema_version is not None and schema_version != SCHEMA_VERSION:
        validator.add(
            "$.schema_version",
            "E_SCHEMA_VERSION",
            f"unsupported schema version {schema_version}; expected {SCHEMA_VERSION}",
        )
    project_id = validator.string(data, "project_id", "$")
    seed = None
    if "seed" in data:
        seed = validator.integer(data, "seed", "$", minimum=0)

    for key in (
        "room",
        "screen",
        "stereo",
        "assessment",
        "viewing_poses",
        "furniture",
        "acoustic_samples",
    ):
        if key not in data:
            validator.add(
                f"$.{key}",
                "E_REQUIRED",
                f"required field '{key}' is missing",
            )
    room = (
        _parse_room(validator, data.get("room"))
        if "room" in data
        else None
    )
    screen = (
        _parse_screen(validator, data.get("screen"), room)
        if "screen" in data
        else None
    )
    stereo = (
        _parse_stereo(validator, data.get("stereo"))
        if "stereo" in data
        else None
    )
    assessment = (
        _parse_assessment(validator, data.get("assessment"))
        if "assessment" in data
        else None
    )
    poses = (
        _parse_poses(validator, data.get("viewing_poses"), room)
        if "viewing_poses" in data
        else ()
    )
    furniture = (
        _parse_furniture(validator, data.get("furniture"), room)
        if "furniture" in data
        else ()
    )
    relationships = _parse_relationships(
        validator, data.get("relationships")
    )
    acoustic_samples = (
        _parse_acoustic_samples(
            validator, data.get("acoustic_samples"), room
        )
        if "acoustic_samples" in data
        else ()
    )

    furniture_ids = {item.id for item in furniture}
    pose_ids = {pose.id for pose in poses}
    raw_poses_value = data.get("viewing_poses", [])
    raw_relationships_value = data.get("relationships", [])
    raw_acoustic_value = data.get("acoustic_samples", [])
    raw_poses = (
        raw_poses_value if isinstance(raw_poses_value, list) else []
    )
    raw_relationships = (
        raw_relationships_value
        if isinstance(raw_relationships_value, list)
        else []
    )
    raw_acoustic = (
        raw_acoustic_value if isinstance(raw_acoustic_value, list) else []
    )
    pose_index = {
        item.get("id"): index
        for index, item in enumerate(raw_poses)
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    relationship_index = {
        item.get("id"): index
        for index, item in enumerate(raw_relationships)
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    acoustic_index = {
        item.get("id"): index
        for index, item in enumerate(raw_acoustic)
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for pose in poses:
        if pose.furniture_id not in furniture_ids:
            validator.add(
                f"$.viewing_poses[{pose_index.get(pose.id, 0)}]"
                ".furniture_id",
                "E_REFERENCE",
                f"viewing pose '{pose.id}' references unknown furniture "
                f"'{pose.furniture_id}'",
            )
    for relationship in relationships:
        for label, reference in (
            ("from", relationship.from_ref),
            ("to", relationship.to_ref),
        ):
            if reference.startswith("screen:"):
                if screen is None or reference != f"screen:{screen.id}":
                    index = relationship_index.get(relationship.id, 0)
                    validator.add(
                        f"$.relationships[{index}].{label}",
                        "E_REFERENCE",
                        f"relationship '{relationship.id}' {label} references "
                        f"unknown screen '{reference}'",
                    )
            elif reference not in furniture_ids:
                index = relationship_index.get(relationship.id, 0)
                validator.add(
                    f"$.relationships[{index}].{label}",
                    "E_REFERENCE",
                    f"relationship '{relationship.id}' {label} references "
                    f"unknown furniture '{reference}'",
                )
    for sample in acoustic_samples:
        if (
            sample.viewing_pose_id is not None
            and sample.viewing_pose_id not in pose_ids
        ):
            index = acoustic_index.get(sample.id, 0)
            validator.add(
                f"$.acoustic_samples[{index}].viewing_pose_id",
                "E_REFERENCE",
                f"acoustic sample '{sample.id}' references unknown viewing "
                f"pose '{sample.viewing_pose_id}'",
            )
    if screen is not None:
        for pose in poses:
            view_direction = subtract(screen.center, pose.position)
            if dot(view_direction, pose.forward) <= 0.0:
                index = pose_index.get(pose.id, 0)
                validator.add(
                    f"$.viewing_poses[{index}]",
                    "E_VIEW_DIRECTION",
                    f"viewing pose '{pose.id}' does not face the screen",
                )

    issues = sorted(set(validator.issues))
    if issues:
        return issues, None

    assert schema_version is not None
    assert project_id is not None
    assert room is not None
    assert screen is not None
    assert stereo is not None
    assert assessment is not None
    assert isinstance(data, dict)
    project = Project(
        source_path="<memory>",
        raw=data,
        fingerprint=fingerprint_json(data),
        schema_version=schema_version,
        project_id=project_id,
        seed=seed,
        room=room,
        screen=screen,
        stereo=stereo,
        assessment=assessment,
        viewing_poses=poses,
        furniture=furniture,
        relationships=relationships,
        acoustic_samples=acoustic_samples,
    )
    return [], project


def project_from_data(
    data: Any,
    *,
    source: str = "<memory>",
) -> Project:
    issues, project = validate_project_data(data)
    if issues or project is None:
        raise ProjectValidationError(issues)
    assert project is not None
    return Project(
        source_path=source,
        raw=project.raw,
        fingerprint=project.fingerprint,
        schema_version=project.schema_version,
        project_id=project.project_id,
        seed=project.seed,
        room=project.room,
        screen=project.screen,
        stereo=project.stereo,
        assessment=project.assessment,
        viewing_poses=project.viewing_poses,
        furniture=project.furniture,
        relationships=project.relationships,
        acoustic_samples=project.acoustic_samples,
    )


def load_project(path: str | Path) -> Project:
    source = str(path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as error:
        raise ProjectValidationError(
            [
                ValidationIssue(
                    path="$",
                    code="E_IO",
                    message=f"cannot read project: {error}",
                )
            ]
        ) from error
    except json.JSONDecodeError as error:
        raise ProjectValidationError(
            [
                ValidationIssue(
                    path="$",
                    code="E_JSON_PARSE",
                    message=(
                        f"invalid JSON at line {error.lineno}, "
                        f"column {error.colno}: {error.msg}"
                    ),
                )
            ]
        ) from error
    return project_from_data(data, source=source)
