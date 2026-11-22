"""Canonical internal representation shared by all parameter versions."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

import numpy as np

from .errors import BusinessError, ErrorCode

NUMERIC_DECIMALS = 12


def qfloat(value: float) -> float:
    """Round to the declared semantic precision and canonicalize -0.0."""

    number = round(float(value), NUMERIC_DECIMALS)
    if not math.isfinite(number):
        raise ValueError(f"non-finite number: {value!r}")
    if abs(number) < 0.5 * (10.0 ** -NUMERIC_DECIMALS):
        return 0.0
    return number


def qtuple(values: Iterable[float]) -> tuple[float, ...]:
    return tuple(qfloat(value) for value in values)


def format_number(value: float) -> str:
    """Stable text representation for field-oriented formats."""

    number = qfloat(value)
    if number == 0:
        return "0"
    if number.is_integer() and abs(number) < 1e15:
        return str(int(number))
    return format(number, ".15g")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DistortionStage:
    """One stage in a per-view normalized-coordinate distortion chain."""

    model: str
    params: tuple[tuple[str, float], ...]

    @classmethod
    def radial(cls, k1: float, k2: float) -> "DistortionStage":
        return cls("radial", (("k1", qfloat(k1)), ("k2", qfloat(k2))))

    def param(self, name: str, default: float = 0.0) -> float:
        for key, value in self.params:
            if key == name:
                return value
        return default

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.model, "params": dict(self.params)}


@dataclass(frozen=True)
class ExposureCurve:
    """Deterministic exposure transfer used by the gain compensation stage."""

    mode: str = "linear_gain"
    gain: float = 1.0
    gamma: float = 1.0
    params: tuple[tuple[str, float], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "mode": self.mode,
            "gain": self.gain,
            "gamma": self.gamma,
        }
        if self.params:
            data["params"] = dict(self.params)
        return data


@dataclass(frozen=True)
class ViewParameters:
    camera_id: int
    focal: float
    principal_x: float
    principal_y: float
    extrinsic: tuple[float, ...]
    distortion_k1: float
    distortion_k2: float
    exposure_gain: float
    homography: tuple[float, ...]
    overlap_weight: float = 1.0
    exposure_mode: str = "linear"
    color_gain: tuple[float, float, float] = (1.0, 1.0, 1.0)
    distortion_chain: tuple[DistortionStage, ...] = ()
    exposure_curve: ExposureCurve = field(default_factory=ExposureCurve)

    def __post_init__(self) -> None:
        if len(self.extrinsic) != 12:
            raise ValueError("extrinsic must contain 12 row-major values")
        if len(self.homography) != 9:
            raise ValueError("homography must contain 9 row-major values")
        if len(self.color_gain) != 3:
            raise ValueError("color_gain must contain 3 values")
        if not self.distortion_chain:
            object.__setattr__(
                self,
                "distortion_chain",
                (DistortionStage.radial(self.distortion_k1, self.distortion_k2),),
            )
        if self.exposure_curve.gain != self.exposure_gain and not self.exposure_curve.params:
            object.__setattr__(
                self,
                "exposure_curve",
                ExposureCurve(
                    self.exposure_curve.mode,
                    self.exposure_gain,
                    self.exposure_curve.gamma,
                ),
            )

    @property
    def intrinsic_matrix(self) -> np.ndarray:
        return np.array(
            [
                [self.focal, 0.0, self.principal_x],
                [0.0, self.focal, self.principal_y],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )

    @property
    def homography_matrix(self) -> np.ndarray:
        return np.array(self.homography, dtype=np.float64).reshape(3, 3)

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "focal": self.focal,
            "principal_x": self.principal_x,
            "principal_y": self.principal_y,
            "extrinsic": list(self.extrinsic),
            "distortion_k1": self.distortion_k1,
            "distortion_k2": self.distortion_k2,
            "exposure_gain": self.exposure_gain,
            "homography": list(self.homography),
            "overlap_weight": self.overlap_weight,
            "exposure_mode": self.exposure_mode,
            "color_gain": list(self.color_gain),
            "distortion_chain": [stage.to_dict() for stage in self.distortion_chain],
            "exposure_curve": self.exposure_curve.to_dict(),
        }


@dataclass(frozen=True)
class SceneParameters:
    canvas_width: int
    canvas_height: int
    feather_radius: float
    color_space: str
    views: tuple[ViewParameters, ...]
    interpolation: str = "bilinear"
    weight_tolerance: float = 1e-9

    def __post_init__(self) -> None:
        ids = [view.camera_id for view in self.views]
        if len(ids) != len(set(ids)):
            raise ValueError("camera_id values must be unique")

    @property
    def sorted_views(self) -> tuple[ViewParameters, ...]:
        return tuple(sorted(self.views, key=lambda view: view.camera_id))

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "canvas_width": self.canvas_width,
            "canvas_height": self.canvas_height,
            "feather_radius": self.feather_radius,
            "color_space": self.color_space,
            "interpolation": self.interpolation,
            "weight_tolerance": self.weight_tolerance,
            "views": [view.semantic_dict() for view in self.sorted_views],
        }

    @property
    def semantic_hash(self) -> str:
        return sha256_json(self.semantic_dict())

    def with_views(self, views: Iterable[ViewParameters]) -> "SceneParameters":
        return replace(self, views=tuple(views))


@dataclass(frozen=True)
class MigrationItem:
    action: str
    field_path: str
    source: str
    reason: str
    value: Any | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "action": self.action,
            "field_path": self.field_path,
            "source": self.source,
            "reason": self.reason,
        }
        if self.value is not None:
            result["value"] = self.value
        return result


@dataclass(frozen=True)
class ParsedScene:
    scene: SceneParameters
    format_version: str
    completions: tuple[MigrationItem, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def view_count(self) -> int:
        return len(self.scene.views)


@dataclass(frozen=True)
class MigrationResult:
    scene: SceneParameters
    source_version: str
    target_version: str
    items: tuple[MigrationItem, ...]
    dropped_fields: tuple[str, ...] = ()

    def report_dict(self) -> dict[str, Any]:
        return {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "semantic_hash": self.scene.semantic_hash,
            "items": [item.to_dict() for item in self.items],
            "dropped_fields": list(self.dropped_fields),
        }


def validate_scene_structure(scene: SceneParameters) -> None:
    """Validate structure that is independent of a parameter version."""

    if scene.canvas_width <= 0 or scene.canvas_height <= 0:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "canvas dimensions must be positive",
            stage="parse",
            field_path="canvas",
        )
    if not scene.views:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            "at least one view is required",
            stage="parse",
            field_path="views",
        )
    seen: set[int] = set()
    for index, view in enumerate(scene.views):
        if view.camera_id in seen:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"duplicate camera_id {view.camera_id}",
                stage="parse",
                field_path=f"views[{index}].camera_id",
            )
        seen.add(view.camera_id)
        if len(view.extrinsic) != 12:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"extrinsic must have 12 values, got {len(view.extrinsic)}",
                stage="parse",
                field_path=f"views[{index}].extrinsic",
                details={"expected": 12, "actual": len(view.extrinsic)},
            )
        if len(view.homography) != 9:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"homography must have 9 values, got {len(view.homography)}",
                stage="parse",
                field_path=f"views[{index}].homography",
                details={"expected": 9, "actual": len(view.homography)},
            )
        if view.focal <= 0:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                "focal length must be positive",
                stage="parse",
                field_path=f"views[{index}].focal",
            )

