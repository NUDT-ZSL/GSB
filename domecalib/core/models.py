"""Serializable domain models for dome calibration projects."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
import hashlib
import json
from typing import Optional

import numpy as np

from .errors import CalibrationError, E_RESOLUTION_MISMATCH


def stable_hash(payload: dict) -> str:
    """Deterministic SHA-256 hash of a JSON serialisable payload."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


@dataclass
class DomeSpec:
    radius: float
    center: tuple[float, float, float]
    tilt_deg: float

    @property
    def tilt(self) -> float:
        return np.radians(self.tilt_deg)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DomeSpec":
        return cls(radius=float(data["radius"]),
                   center=tuple(float(v) for v in data["center"]),
                   tilt_deg=float(data["tilt_deg"]))


@dataclass
class ProjectorSpec:
    """Physical and optical description of one projector."""

    projector_id: str
    position: tuple[float, float, float]
    forward: tuple[float, float, float]
    up_hint: tuple[float, float, float]
    fov_h_deg: float
    width: int
    height: int
    k1: float
    k2: float
    calib_frac: float = 0.20

    def resolution(self) -> tuple[int, int]:
        return self.width, self.height

    def signature(self) -> dict:
        """JSON-serialisable payload used for version hashing."""
        return {
            "projector_id": self.projector_id,
            "position": [float(v) for v in self.position],
            "forward": [float(v) for v in self.forward],
            "up_hint": [float(v) for v in self.up_hint],
            "fov_h_deg": float(self.fov_h_deg),
            "width": int(self.width),
            "height": int(self.height),
            "k1": float(self.k1),
            "k2": float(self.k2),
            "calib_frac": float(self.calib_frac),
        }

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectorSpec":
        return cls(
            projector_id=str(data["projector_id"]),
            position=tuple(float(v) for v in data["position"]),
            forward=tuple(float(v) for v in data["forward"]),
            up_hint=tuple(float(v) for v in data["up_hint"]),
            fov_h_deg=float(data["fov_h_deg"]),
            width=int(data["width"]),
            height=int(data["height"]),
            k1=float(data["k1"]),
            k2=float(data["k2"]),
            calib_frac=float(data.get("calib_frac", 0.20)),
        )


@dataclass
class ControlPointSet:
    """Per-projector calibration points.

    ``ideal_lon/lat`` are the true dome grid coordinates while
    ``observed_lon/lat`` are the measured (perturbed) coordinates used as TPS
    source positions.  ``noise_scale`` records the index-dependent perturbation
    amplitude used by the sample generator.
    """

    projector_id: str
    ideal_lon: np.ndarray
    ideal_lat: np.ndarray
    observed_lon: np.ndarray
    observed_lat: np.ndarray
    noise_scale: float

    def count(self) -> int:
        return int(self.ideal_lon.size)

    def to_dict(self) -> dict:
        return {
            "projector_id": self.projector_id,
            "ideal_lon": self.ideal_lon.tolist(),
            "ideal_lat": self.ideal_lat.tolist(),
            "observed_lon": self.observed_lon.tolist(),
            "observed_lat": self.observed_lat.tolist(),
            "noise_scale": float(self.noise_scale),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ControlPointSet":
        return cls(
            projector_id=str(data["projector_id"]),
            ideal_lon=np.asarray(data["ideal_lon"], dtype=float),
            ideal_lat=np.asarray(data["ideal_lat"], dtype=float),
            observed_lon=np.asarray(data["observed_lon"], dtype=float),
            observed_lat=np.asarray(data["observed_lat"], dtype=float),
            noise_scale=float(data["noise_scale"]),
        )


@dataclass
class BlendingParams:
    """Global feather parameters applied when building blend masks."""

    feather_width_deg: float = 6.0
    curve: str = "smoothstep"  # "linear" or "smoothstep"
    gamma: float = 1.0

    def signature(self) -> dict:
        return asdict(self)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BlendingParams":
        return cls(
            feather_width_deg=float(data.get("feather_width_deg", 6.0)),
            curve=str(data.get("curve", "smoothstep")),
            gamma=float(data.get("gamma", 1.0)),
        )


@dataclass
class ProjectorLUT:
    """Per-pixel reverse mapping result for one projector."""

    projector_id: str
    width: int
    height: int
    lon: np.ndarray            # (H, W) corrected longitude, nan when invalid
    lat: np.ndarray            # (H, W) corrected latitude, nan when invalid
    raw_lon: np.ndarray        # pre-TPS reverse mapping
    raw_lat: np.ndarray
    valid: np.ndarray          # (H, W) bool
    spec_hash: str
    points_hash: str
    blend_hash: str
    version: int

    def check_resolution(self, width: int, height: int) -> None:
        if self.width != width or self.height != height:
            raise CalibrationError(
                E_RESOLUTION_MISMATCH,
                f"stored LUT {self.width}x{self.height} does not match "
                f"projector {width}x{height}",
                {"projector_id": self.projector_id,
                 "stored": [self.width, self.height],
                 "expected": [width, height]},
            )


@dataclass
class BlendMask:
    """Per-projector feather mask on the projector pixel grid."""

    projector_id: str
    width: int
    height: int
    weight: np.ndarray         # (H, W) float in [0, 1]
    valid: np.ndarray          # (H, W) bool
    blend_hash: str
    version: int


@dataclass
class CorrectionQuality:
    """TPS fit diagnostics, including leave-one-out cross validation."""

    projector_id: str
    status: str                # "ok" | "degraded" | "failed"
    condition_number: float
    regularization: float
    rms_residual_rad: float
    max_residual_rad: float
    loocv_rms_rad: float
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CalibrationProject:
    """Full project definition plus generated calibration artefacts."""

    seed: int
    dome: DomeSpec
    projectors: list[ProjectorSpec]
    control_points: list[ControlPointSet]
    blending: BlendingParams
    grid_width: int
    grid_height: int
    # generator metadata (true warp coefficients, versions, ...)
    warp_params: dict = field(default_factory=dict)
    luts: dict[str, ProjectorLUT] = field(default_factory=dict)
    masks: dict[str, BlendMask] = field(default_factory=dict)
    quality: dict[str, CorrectionQuality] = field(default_factory=dict)
    projector_versions: dict[str, int] = field(default_factory=dict)

    def projector(self, projector_id: str) -> ProjectorSpec:
        for spec in self.projectors:
            if spec.projector_id == projector_id:
                return spec
        raise CalibrationError(
            "E1006_INVALID_PARAMETER",
            f"unknown projector id '{projector_id}'",
            {"projector_id": projector_id},
        )

    def points_for(self, projector_id: str) -> Optional[ControlPointSet]:
        for pts in self.control_points:
            if pts.projector_id == projector_id:
                return pts
        return None
