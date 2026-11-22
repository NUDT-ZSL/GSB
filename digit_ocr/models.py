"""Immutable domain model for samples, configuration and snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import numpy as np

from .canonical import canonical_bytes, sha256_bytes, sha256_json, stable_float
from .errors import E_INVALID_INPUT, BusinessError


CONTROLLED_LABELS: Tuple[int, ...] = tuple(range(10))
CONFIDENCE_TIERS: Tuple[str, ...] = ("high", "medium", "low", "none")


def _validate_version(version: int, name: str) -> None:
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise BusinessError(
            E_INVALID_INPUT,
            "{0} version must be an integer >= 1".format(name),
            category="version",
        )


@dataclass(frozen=True)
class NormalizationConfig:
    """Pixel normalization and projection feature rules.

    Bin boundaries are half-open and explicitly defined by:
      start = floor(index * size / bins)
      stop  = floor((index + 1) * size / bins)
    Pixel coordinates are zero-based and row-major. Feature order is all row
    projections followed by all column projections.
    """

    version: int
    canvas_height: int = 28
    canvas_width: int = 28
    input_min: float = 0.0
    input_max: float = 255.0
    output_min: float = 0.0
    output_max: float = 1.0
    projection_bins: int = 8
    cell_grid_size: int = 4
    feature_dim: int = 32
    projection_order: str = "rows_then_columns_then_cells"
    pixel_index_order: str = "row_major_zero_based"
    bin_boundary_rule: str = "half_open_floor_scaled"
    normalization_rule: str = "clip_then_affine_scale"

    def __post_init__(self) -> None:
        _validate_version(self.version, "normalization")
        if self.canvas_height <= 0 or self.canvas_width <= 0:
            raise BusinessError(
                E_INVALID_INPUT,
                "canvas dimensions must be positive",
                category="normalization",
            )
        if self.projection_bins <= 0:
            raise BusinessError(
                E_INVALID_INPUT,
                "projection_bins must be positive",
                category="normalization",
            )
        if self.projection_bins > min(self.canvas_height, self.canvas_width):
            raise BusinessError(
                E_INVALID_INPUT,
                "every projection bin must cover at least one pixel",
                category="normalization",
            )
        if self.cell_grid_size <= 0:
            raise BusinessError(
                E_INVALID_INPUT,
                "cell_grid_size must be positive",
                category="normalization",
            )
        if self.cell_grid_size > min(self.canvas_height, self.canvas_width):
            raise BusinessError(
                E_INVALID_INPUT,
                "every cell projection must cover at least one pixel",
                category="normalization",
            )
        if self.feature_dim != self.projection_bins * 2 + self.cell_grid_size**2:
            raise BusinessError(
                E_INVALID_INPUT,
                "feature_dim must equal 2 * projection_bins + cell_grid_size squared",
                category="normalization",
            )
        if float(self.input_max) <= float(self.input_min):
            raise BusinessError(
                E_INVALID_INPUT,
                "input_max must be greater than input_min",
                category="normalization",
            )
        if self.projection_order != "rows_then_columns_then_cells":
            raise BusinessError(
                E_INVALID_INPUT,
                "unsupported projection order",
                category="normalization",
            )

    def to_payload(self) -> Dict[str, Any]:
        return {
            "kind": "normalization",
            "version": self.version,
            "canvas_height": self.canvas_height,
            "canvas_width": self.canvas_width,
            "input_min": stable_float(self.input_min),
            "input_max": stable_float(self.input_max),
            "output_min": stable_float(self.output_min),
            "output_max": stable_float(self.output_max),
            "projection_bins": self.projection_bins,
            "cell_grid_size": self.cell_grid_size,
            "feature_dim": self.feature_dim,
            "projection_order": self.projection_order,
            "pixel_index_order": self.pixel_index_order,
            "bin_boundary_rule": self.bin_boundary_rule,
            "normalization_rule": self.normalization_rule,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "NormalizationConfig":
        return cls(
            version=int(payload["version"]),
            canvas_height=int(payload["canvas_height"]),
            canvas_width=int(payload["canvas_width"]),
            input_min=float(payload["input_min"]),
            input_max=float(payload["input_max"]),
            output_min=float(payload["output_min"]),
            output_max=float(payload["output_max"]),
            projection_bins=int(payload["projection_bins"]),
            cell_grid_size=int(payload["cell_grid_size"]),
            feature_dim=int(payload["feature_dim"]),
            projection_order=str(payload["projection_order"]),
            pixel_index_order=str(payload["pixel_index_order"]),
            bin_boundary_rule=str(payload["bin_boundary_rule"]),
            normalization_rule=str(payload["normalization_rule"]),
        )

    @property
    def checksum(self) -> str:
        return sha256_json(self.to_payload())


@dataclass(frozen=True)
class ThresholdConfig:
    version: int
    reject_threshold: float
    high_max: float
    medium_max: float
    accept_rule: str = "accept_if_best_distance_lte_reject_threshold"
    confidence_rule: str = "distance_tiers_then_rejection"

    def __post_init__(self) -> None:
        _validate_version(self.version, "thresholds")
        values = (self.reject_threshold, self.high_max, self.medium_max)
        if any(float(value) < 0.0 for value in values):
            raise BusinessError(
                E_INVALID_INPUT,
                "threshold values must be non-negative",
                category="thresholds",
            )
        if float(self.high_max) > float(self.medium_max):
            raise BusinessError(
                E_INVALID_INPUT,
                "high_max must not exceed medium_max",
                category="thresholds",
            )

    def to_payload(self) -> Dict[str, Any]:
        return {
            "kind": "thresholds",
            "version": self.version,
            "reject_threshold": stable_float(self.reject_threshold),
            "high_max": stable_float(self.high_max),
            "medium_max": stable_float(self.medium_max),
            "accept_rule": self.accept_rule,
            "confidence_rule": self.confidence_rule,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ThresholdConfig":
        return cls(
            version=int(payload["version"]),
            reject_threshold=float(payload["reject_threshold"]),
            high_max=float(payload["high_max"]),
            medium_max=float(payload["medium_max"]),
            accept_rule=str(payload["accept_rule"]),
            confidence_rule=str(payload["confidence_rule"]),
        )

    @property
    def checksum(self) -> str:
        return sha256_json(self.to_payload())


@dataclass(frozen=True)
class DistanceMetric:
    name: str = "euclidean"
    version: int = 1

    def __post_init__(self) -> None:
        _validate_version(self.version, "distance_metric")
        if self.name not in ("euclidean", "squared_euclidean", "manhattan"):
            raise BusinessError(
                E_INVALID_INPUT,
                "unsupported distance metric: {0}".format(self.name),
                category="distance_metric",
            )

    def to_payload(self) -> Dict[str, Any]:
        return {"kind": "distance_metric", "name": self.name, "version": self.version}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DistanceMetric":
        return cls(name=str(payload["name"]), version=int(payload["version"]))

    @property
    def checksum(self) -> str:
        return sha256_json(self.to_payload())


@dataclass(frozen=True)
class Prototype:
    class_id: int
    version: int
    pixels: Tuple[int, ...]
    canvas_height: int = 28
    canvas_width: int = 28

    def __post_init__(self) -> None:
        _validate_version(self.version, "prototype")
        if self.class_id not in CONTROLLED_LABELS:
            raise BusinessError(
                E_INVALID_INPUT,
                "prototype class_id must be in controlled labels 0..9",
                category="prototype",
            )
        expected = self.canvas_height * self.canvas_width
        if len(self.pixels) != expected:
            raise BusinessError(
                E_INVALID_INPUT,
                "prototype pixel count does not match canvas",
                category="prototype",
            )
        if any(int(value) < 0 or int(value) > 255 for value in self.pixels):
            raise BusinessError(
                E_INVALID_INPUT,
                "prototype pixels must be uint8 values",
                category="prototype",
            )

    def to_payload(self) -> Dict[str, Any]:
        return {
            "class_id": self.class_id,
            "version": self.version,
            "canvas_height": self.canvas_height,
            "canvas_width": self.canvas_width,
            "pixels": list(self.pixels),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Prototype":
        return cls(
            class_id=int(payload["class_id"]),
            version=int(payload["version"]),
            pixels=tuple(int(value) for value in payload["pixels"]),
            canvas_height=int(payload["canvas_height"]),
            canvas_width=int(payload["canvas_width"]),
        )

    @property
    def pixel_sha256(self) -> str:
        header = (
            "prototype:{0}:{1}x{2}:".format(
                self.class_id, self.canvas_width, self.canvas_height
            )
        ).encode("ascii")
        return sha256_bytes(header + bytes(bytearray(self.pixels)))

    @property
    def checksum(self) -> str:
        return sha256_json(self.to_payload())


@dataclass(frozen=True)
class PrototypeSet:
    version: int
    prototypes: Tuple[Prototype, ...]

    def __post_init__(self) -> None:
        _validate_version(self.version, "prototypes")
        ordered = tuple(sorted(self.prototypes, key=lambda item: item.class_id))
        if ordered != self.prototypes:
            object.__setattr__(self, "prototypes", ordered)
        classes = [prototype.class_id for prototype in self.prototypes]
        if classes != list(CONTROLLED_LABELS):
            raise BusinessError(
                E_INVALID_INPUT,
                "prototype set must contain each controlled label exactly once",
                category="prototypes",
            )

    def member(self, class_id: int) -> Prototype:
        for prototype in self.prototypes:
            if prototype.class_id == class_id:
                return prototype
        raise BusinessError(
            E_INVALID_INPUT,
            "prototype class is not present",
            category="prototypes",
        )

    def to_payload(self) -> Dict[str, Any]:
        return {
            "kind": "prototype_set",
            "version": self.version,
            "prototypes": [prototype.to_payload() for prototype in self.prototypes],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PrototypeSet":
        return cls(
            version=int(payload["version"]),
            prototypes=tuple(
                Prototype.from_payload(item) for item in payload["prototypes"]
            ),
        )

    @property
    def checksum(self) -> str:
        return sha256_json(self.to_payload())

    def member_hashes(self) -> Dict[str, str]:
        return {
            str(prototype.class_id): prototype.checksum
            for prototype in self.prototypes
        }

    def replace_member(self, prototype: Prototype) -> "PrototypeSet":
        members = [
            prototype if item.class_id == prototype.class_id else item
            for item in self.prototypes
        ]
        return PrototypeSet(version=self.version + 1, prototypes=tuple(members))


@dataclass(frozen=True)
class RecognitionConfig:
    normalization: NormalizationConfig
    prototypes: PrototypeSet
    thresholds: ThresholdConfig
    distance_metric: DistanceMetric = DistanceMetric()

    def __post_init__(self) -> None:
        for prototype in self.prototypes.prototypes:
            if (
                prototype.canvas_height != self.normalization.canvas_height
                or prototype.canvas_width != self.normalization.canvas_width
            ):
                raise BusinessError(
                    E_INVALID_INPUT,
                    "prototype canvas must match normalization canvas",
                    category="config",
                )

    def to_payload(self) -> Dict[str, Any]:
        return {
            "kind": "recognition_config",
            "normalization": self.normalization.to_payload(),
            "prototypes": self.prototypes.to_payload(),
            "thresholds": self.thresholds.to_payload(),
            "distance_metric": self.distance_metric.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RecognitionConfig":
        if payload.get("kind") != "recognition_config":
            raise BusinessError(
                E_INVALID_INPUT,
                "recognition config payload has an unsupported kind",
                category="config",
            )
        return cls(
            normalization=NormalizationConfig.from_payload(payload["normalization"]),
            prototypes=PrototypeSet.from_payload(payload["prototypes"]),
            thresholds=ThresholdConfig.from_payload(payload["thresholds"]),
            distance_metric=DistanceMetric.from_payload(payload["distance_metric"]),
        )

    @property
    def checksum(self) -> str:
        return sha256_json(self.to_payload())

    def with_thresholds(self, thresholds: ThresholdConfig) -> "RecognitionConfig":
        return replace(self, thresholds=thresholds)

    def with_prototypes(self, prototypes: PrototypeSet) -> "RecognitionConfig":
        return replace(self, prototypes=prototypes)

    def with_normalization(
        self, normalization: NormalizationConfig
    ) -> "RecognitionConfig":
        return replace(self, normalization=normalization)

    def with_distance_metric(self, metric: DistanceMetric) -> "RecognitionConfig":
        return replace(self, distance_metric=metric)


@dataclass(frozen=True)
class ImageSample:
    image_id: str
    seed: int
    label: int
    uncertain: bool
    pixels: bytes
    height: int = 28
    width: int = 28

    def __post_init__(self) -> None:
        if not self.image_id:
            raise BusinessError(
                E_INVALID_INPUT,
                "image_id must not be empty",
                category="sample",
            )
        if self.label not in CONTROLLED_LABELS:
            raise BusinessError(
                E_INVALID_INPUT,
                "sample label must be in controlled labels 0..9",
                image_id=self.image_id,
                category="sample",
            )
        if self.height <= 0 or self.width <= 0:
            raise BusinessError(
                E_INVALID_INPUT,
                "sample canvas dimensions must be positive",
                image_id=self.image_id,
                category="sample",
            )
        if len(self.pixels) != self.height * self.width:
            raise BusinessError(
                E_INVALID_INPUT,
                "sample pixel count does not match canvas",
                image_id=self.image_id,
                category="sample",
            )
        if not isinstance(self.pixels, bytes):
            object.__setattr__(self, "pixels", bytes(self.pixels))

    @classmethod
    def from_array(
        cls,
        image_id: str,
        image: np.ndarray,
        *,
        seed: int,
        label: int,
        uncertain: bool,
    ) -> "ImageSample":
        array = np.asarray(image)
        if array.ndim != 2:
            raise BusinessError(
                E_INVALID_INPUT,
                "sample image must be a 2-D grayscale array",
                image_id=image_id,
                category="sample",
            )
        if not np.issubdtype(array.dtype, np.number):
            raise BusinessError(
                E_INVALID_INPUT,
                "sample image must be numeric",
                image_id=image_id,
                category="sample",
            )
        if not np.all(np.isfinite(array)):
            raise BusinessError(
                E_INVALID_INPUT,
                "sample image contains non-finite values",
                image_id=image_id,
                category="sample",
            )
        quantized = np.clip(np.rint(array), 0, 255).astype(np.uint8)
        return cls(
            image_id=image_id,
            seed=int(seed),
            label=int(label),
            uncertain=bool(uncertain),
            pixels=quantized.tobytes(order="C"),
            height=int(quantized.shape[0]),
            width=int(quantized.shape[1]),
        )

    def as_array(self) -> np.ndarray:
        return np.frombuffer(self.pixels, dtype=np.uint8).reshape(
            self.height, self.width
        ).copy()

    @property
    def image_sha256(self) -> str:
        header = "gray8:{0}x{1}:".format(self.width, self.height).encode("ascii")
        return sha256_bytes(header + self.pixels)

    def to_payload(self, include_pixels: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "image_id": self.image_id,
            "seed": self.seed,
            "label": self.label,
            "uncertain": self.uncertain,
            "height": self.height,
            "width": self.width,
            "image_sha256": self.image_sha256,
        }
        if include_pixels:
            payload["pixels"] = list(self.pixels)
        return payload


@dataclass(frozen=True)
class MetricsSnapshot:
    version: int
    checksum: str
    data: Mapping[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": "metrics",
            "version": self.version,
            "checksum": self.checksum,
            "data": dict(self.data),
        }


def validate_unique_samples(samples: Sequence[ImageSample]) -> List[ImageSample]:
    ids = [sample.image_id for sample in samples]
    duplicates = sorted({image_id for image_id in ids if ids.count(image_id) > 1})
    if duplicates:
        raise BusinessError(
            E_INVALID_INPUT,
            "duplicate image_id in batch: {0}".format(duplicates[0]),
            image_id=duplicates[0],
            category="sample_batch",
        )
    return sorted(samples, key=lambda sample: sample.image_id)


def save_config(config: RecognitionConfig, path: Path) -> None:
    """Write a complete recognition configuration as canonical JSON."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_bytes(config.to_payload()))


def load_config(path: Path) -> RecognitionConfig:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessError(
            E_INVALID_INPUT,
            "recognition config cannot be loaded: {0}".format(exc),
            category="config_io",
        )
    if not isinstance(payload, dict):
        raise BusinessError(
            E_INVALID_INPUT,
            "recognition config must be a JSON object",
            category="config_io",
        )
    return RecognitionConfig.from_payload(payload)
