from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .deterministic import array_digest, sha256_bytes
from .errors import BusinessError, E_SAMPLE_INVALID, E_THRESHOLD_INVALID
from .precision import SUPPORTED_PRECISIONS, SUPPORTED_ROUNDING


@dataclass(frozen=True)
class Region:
    id: str
    label: int
    kind: str
    geometry: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "geometry": self.geometry,
        }


@dataclass(frozen=True)
class StyleParams:
    strength: float = 0.72
    color_matrix: tuple[tuple[float, float, float], ...] = (
        (0.96, 0.06, -0.02),
        (-0.03, 0.94, 0.09),
        (0.05, -0.04, 1.01),
    )
    edge_preserve: float = 0.28
    iterations: int = 3
    precision: str = "float64"
    fixed_scale: int = 4096
    fixed_rounding: str = "half_even"
    boundary_radius: int = 3
    boundary_sigma: float = 1.45
    feather_radius: float = 3.2
    style_threshold: float = 0.58
    boundary_threshold: float = 0.085
    threshold_policy: str = "tolerance_band"
    threshold_tolerance: float = 0.012
    error_tolerance: float = 0.01
    version: int = 1

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StyleParams":
        matrix = raw.get(
            "color_matrix",
            [
                [0.96, 0.06, -0.02],
                [-0.03, 0.94, 0.09],
                [0.05, -0.04, 1.01],
            ],
        )
        params = cls(
            strength=float(raw.get("strength", 0.72)),
            color_matrix=tuple(tuple(float(v) for v in row) for row in matrix),
            edge_preserve=float(raw.get("edge_preserve", 0.28)),
            iterations=int(raw.get("iterations", 3)),
            precision=str(raw.get("precision", "float64")),
            fixed_scale=int(raw.get("fixed_scale", 4096)),
            fixed_rounding=str(raw.get("fixed_rounding", "half_even")),
            boundary_radius=int(raw.get("boundary_radius", 3)),
            boundary_sigma=float(raw.get("boundary_sigma", 1.45)),
            feather_radius=float(raw.get("feather_radius", 3.2)),
            style_threshold=float(raw.get("style_threshold", 0.58)),
            boundary_threshold=float(raw.get("boundary_threshold", 0.085)),
            threshold_policy=str(raw.get("threshold_policy", "tolerance_band")),
            threshold_tolerance=float(raw.get("threshold_tolerance", 0.012)),
            error_tolerance=float(raw.get("error_tolerance", 0.01)),
            version=int(raw.get("version", 1)),
        )
        params.validate()
        return params

    def validate(self) -> None:
        finite_checks = {
            "strength": self.strength,
            "edge_preserve": self.edge_preserve,
            "boundary_sigma": self.boundary_sigma,
            "feather_radius": self.feather_radius,
            "style_threshold": self.style_threshold,
            "boundary_threshold": self.boundary_threshold,
            "threshold_tolerance": self.threshold_tolerance,
            "error_tolerance": self.error_tolerance,
        }
        invalid = [name for name, value in finite_checks.items() if not math.isfinite(value)]
        if invalid:
            raise BusinessError(
                E_THRESHOLD_INVALID,
                "style parameters contain non-finite values",
                {"fields": invalid},
            )
        if not 0.0 <= self.strength <= 1.0:
            raise BusinessError(
                E_THRESHOLD_INVALID,
                "style strength must be within [0, 1]",
                {"strength": self.strength},
            )
        if not 0.0 <= self.style_threshold <= 1.0:
            raise BusinessError(
                E_THRESHOLD_INVALID,
                "style strength threshold must be within [0, 1]",
                {"style_threshold": self.style_threshold},
            )
        if self.boundary_threshold < 0.0:
            raise BusinessError(
                E_THRESHOLD_INVALID,
                "boundary residual threshold must be non-negative",
                {"boundary_threshold": self.boundary_threshold},
            )
        if self.threshold_tolerance < 0.0:
            raise BusinessError(
                E_THRESHOLD_INVALID,
                "threshold tolerance must be non-negative",
                {"threshold_tolerance": self.threshold_tolerance},
            )
        if self.threshold_policy not in ("strict", "tolerance_band"):
            raise BusinessError(
                E_THRESHOLD_INVALID,
                "invalid threshold policy",
                {
                    "threshold_policy": self.threshold_policy,
                    "supported": ["strict", "tolerance_band"],
                },
            )
        if self.error_tolerance <= 0.0:
            raise BusinessError(
                E_THRESHOLD_INVALID,
                "error tolerance must be positive",
                {"error_tolerance": self.error_tolerance},
            )
        if self.precision not in SUPPORTED_PRECISIONS:
            raise BusinessError(
                E_THRESHOLD_INVALID,
                "invalid precision tier",
                {"precision": self.precision, "supported": list(SUPPORTED_PRECISIONS)},
            )
        if self.fixed_rounding not in SUPPORTED_ROUNDING:
            raise BusinessError(
                E_THRESHOLD_INVALID,
                "invalid fixed rounding mode",
                {"fixed_rounding": self.fixed_rounding, "supported": list(SUPPORTED_ROUNDING)},
            )
        if self.fixed_scale < 2:
            raise BusinessError(
                E_THRESHOLD_INVALID,
                "fixed scale must be at least 2",
                {"fixed_scale": self.fixed_scale},
            )
        if not 0 <= self.iterations <= 32:
            raise BusinessError(
                E_THRESHOLD_INVALID,
                "iterations must be within [0, 32]",
                {"iterations": self.iterations},
            )
        if not 1 <= self.boundary_radius <= 12:
            raise BusinessError(
                E_THRESHOLD_INVALID,
                "boundary radius must be within [1, 12]",
                {"boundary_radius": self.boundary_radius},
            )
        if self.feather_radius <= 0.0:
            raise BusinessError(
                E_THRESHOLD_INVALID,
                "feather radius must be positive",
                {"feather_radius": self.feather_radius},
            )
        if np.asarray(self.color_matrix).shape != (3, 3) or not np.all(
            np.isfinite(np.asarray(self.color_matrix, dtype=np.float64))
        ):
            raise BusinessError(
                E_THRESHOLD_INVALID,
                "color matrix must be a finite 3x3 matrix",
            )
        if self.version < 1:
            raise BusinessError(
                E_THRESHOLD_INVALID,
                "style parameter version must be positive",
                {"version": self.version},
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "strength": self.strength,
            "color_matrix": [list(row) for row in self.color_matrix],
            "edge_preserve": self.edge_preserve,
            "iterations": self.iterations,
            "precision": self.precision,
            "fixed_scale": self.fixed_scale,
            "fixed_rounding": self.fixed_rounding,
            "boundary_radius": self.boundary_radius,
            "boundary_sigma": self.boundary_sigma,
            "feather_radius": self.feather_radius,
            "style_threshold": self.style_threshold,
            "boundary_threshold": self.boundary_threshold,
            "threshold_policy": self.threshold_policy,
            "threshold_tolerance": self.threshold_tolerance,
            "error_tolerance": self.error_tolerance,
            "version": self.version,
        }

    def revised(
        self,
        *,
        version: int | None = None,
        policy: str | None = None,
        threshold_tolerance: float | None = None,
        error_tolerance: float | None = None,
    ) -> "StyleParams":
        data = self.as_dict()
        if version is not None:
            data["version"] = int(version)
        if policy is not None:
            data["threshold_policy"] = policy
        if threshold_tolerance is not None:
            data["threshold_tolerance"] = float(threshold_tolerance)
        if error_tolerance is not None:
            data["error_tolerance"] = float(error_tolerance)
        return StyleParams.from_dict(data)


@dataclass
class Sample:
    base: np.ndarray
    mask: np.ndarray
    regions: tuple[Region, ...]
    style: StyleParams
    seed: int
    source: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def height(self) -> int:
        return int(self.base.shape[0])

    @property
    def width(self) -> int:
        return int(self.base.shape[1])

    @property
    def base_hash(self) -> str:
        return array_digest(self.base)

    @property
    def mask_hash(self) -> str:
        return array_digest(self.mask)

    @property
    def style_hash(self) -> str:
        from .deterministic import sha256_json

        return sha256_json(self.style.as_dict())

    @property
    def region_map(self) -> dict[str, Region]:
        return {region.id: region for region in self.regions}

    def validate(self) -> None:
        if self.base.ndim != 3 or self.base.shape[2] != 3:
            raise BusinessError(
                E_SAMPLE_INVALID,
                "base image must be HxWx3 RGB",
                {"shape": list(self.base.shape)},
            )
        if self.mask.shape != self.base.shape[:2]:
            raise BusinessError(
                E_SAMPLE_INVALID,
                "mask shape must match image height and width",
                {"base_shape": list(self.base.shape), "mask_shape": list(self.mask.shape)},
            )
        if not np.all(np.isfinite(self.base)):
            raise BusinessError(E_SAMPLE_INVALID, "base image contains non-finite pixels")
        labels = set(int(x) for x in np.unique(self.mask) if int(x) != 0)
        expected = {region.label for region in self.regions}
        if labels != expected:
            raise BusinessError(
                E_SAMPLE_INVALID,
                "mask labels and region definitions do not match",
                {"mask_labels": sorted(labels), "region_labels": sorted(expected)},
            )
        for region in self.regions:
            if not np.any(self.mask == region.label):
                raise BusinessError(
                    E_SAMPLE_INVALID,
                    "region has no mask pixels",
                    {"region": region.id},
                )

    def with_mask(self, mask: np.ndarray, style: StyleParams | None = None) -> "Sample":
        updated = Sample(
            base=self.base.copy(),
            mask=np.asarray(mask, dtype=np.uint8).copy(),
            regions=self.regions,
            style=style or self.style,
            seed=self.seed,
            source=self.source,
            metadata=dict(self.metadata),
        )
        updated.validate()
        return updated

    @classmethod
    def load(cls, sample_dir: str | Path) -> "Sample":
        root = Path(sample_dir)
        manifest_path = root / "sample.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            base = np.load(root / manifest["base_file"], allow_pickle=False)
            mask = np.load(root / manifest["mask_file"], allow_pickle=False)
            style = StyleParams.from_dict(manifest["style"])
            regions = tuple(
                Region(
                    id=str(item["id"]),
                    label=int(item["label"]),
                    kind=str(item["kind"]),
                    geometry=dict(item.get("geometry", {})),
                )
                for item in manifest["regions"]
            )
        except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_SAMPLE_INVALID,
                "failed to load deterministic sample",
                {"sample_dir": str(root), "reason": str(exc)},
            ) from exc
        sample = cls(
            base=np.asarray(base, dtype=np.float64),
            mask=np.asarray(mask, dtype=np.uint8),
            regions=regions,
            style=style,
            seed=int(manifest.get("seed", 0)),
            source=root,
            metadata=manifest,
        )
        sample.validate()
        if manifest.get("base_sha256") and manifest["base_sha256"] != sample.base_hash:
            raise BusinessError(
                E_SAMPLE_INVALID,
                "base image checksum does not match manifest",
                {"expected": manifest["base_sha256"], "actual": sample.base_hash},
            )
        if manifest.get("mask_sha256") and manifest["mask_sha256"] != sample.mask_hash:
            raise BusinessError(
                E_SAMPLE_INVALID,
                "mask checksum does not match manifest",
                {"expected": manifest["mask_sha256"], "actual": sample.mask_hash},
            )
        return sample


def mask_payload(mask: np.ndarray) -> dict[str, Any]:
    array = np.asarray(mask, dtype=np.uint8)
    return {
        "dtype": "uint8",
        "shape": [int(x) for x in array.shape],
        "sha256": sha256_bytes(np.ascontiguousarray(array).tobytes()),
        "data": array.tobytes().hex(),
    }


def mask_from_payload(payload: dict[str, Any]) -> np.ndarray:
    try:
        array = np.frombuffer(bytes.fromhex(payload["data"]), dtype=np.uint8)
        shape = tuple(int(x) for x in payload["shape"])
        array = array.reshape(shape)
        if sha256_bytes(np.ascontiguousarray(array).tobytes()) != payload["sha256"]:
            raise ValueError("mask payload checksum mismatch")
        return array
    except (KeyError, ValueError, TypeError) as exc:
        raise BusinessError(
            E_SAMPLE_INVALID,
            "invalid mask snapshot payload",
            {"reason": str(exc)},
        ) from exc
