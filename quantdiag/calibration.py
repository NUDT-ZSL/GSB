from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from .model import CONV_LAYERS, NetworkWeights, relu, conv2d_valid
from .utils import array_checksum, sha256_json


@dataclass(frozen=True)
class LayerCalibration:
    name: str
    minimum: float
    maximum: float
    samples: int
    source_hash: str
    version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "min": self.minimum,
            "max": self.maximum,
            "samples": self.samples,
            "source_hash": self.source_hash,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, name: str, payload: dict[str, Any]) -> "LayerCalibration":
        minimum = float(payload["min"])
        maximum = float(payload["max"])
        samples = int(payload["samples"])
        source_hash = str(payload.get("source_hash", ""))
        version = str(payload["version"])
        return cls(name, minimum, maximum, samples, source_hash, version)


@dataclass(frozen=True)
class CalibrationStats:
    version: str
    source_hash: str
    sample_count: int
    layers: tuple[LayerCalibration, ...]

    def layer(self, name: str) -> LayerCalibration | None:
        for item in self.layers:
            if item.name == name:
                return item
        return None

    def layer_version(self, name: str) -> str:
        item = self.layer(name)
        return item.version if item is not None else f"{self.version}:{name}:absent"

    def sample_count_for(self, name: str) -> int:
        item = self.layer(name)
        return item.samples if item is not None else 0

    def checksum(self) -> str:
        return sha256_json(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_hash": self.source_hash,
            "sample_count": self.sample_count,
            "samples": self.sample_count,
            "layers": {
                item.name: {
                    **item.to_dict(),
                }
                for item in self.layers
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CalibrationStats":
        layers_payload = payload.get("layers", {})
        if isinstance(layers_payload, list):
            items = [
                LayerCalibration.from_dict(str(item["name"]), item)
                for item in layers_payload
            ]
        else:
            items = [
                LayerCalibration.from_dict(str(name), item)
                for name, item in layers_payload.items()
            ]
        return cls(
            version=str(payload["version"]),
            source_hash=str(payload.get("source_hash", "")),
            sample_count=int(payload.get("sample_count", 0)),
            layers=tuple(items),
        )

    def with_layer_samples(self, layer_name: str, samples: int, *, source_hash: str | None = None) -> "CalibrationStats":
        if samples < 0:
            raise ValueError("samples must be non-negative")
        replaced: list[LayerCalibration] = []
        for item in self.layers:
            if item.name != layer_name:
                replaced.append(item)
                continue
            src = item.source_hash if source_hash is None else source_hash
            version = _layer_calibration_version(
                item.name, item.minimum, item.maximum, samples, src
            )
            replaced.append(replace(item, samples=samples, source_hash=src, version=version))
        if not any(item.name == layer_name for item in replaced):
            raise KeyError(layer_name)
        version = _global_calibration_version(self.source_hash, replaced)
        return CalibrationStats(
            version=version,
            source_hash=self.source_hash,
            sample_count=max(item.samples for item in replaced),
            layers=tuple(replaced),
        )


def _layer_calibration_version(
    name: str, minimum: float, maximum: float, samples: int, source_hash: str
) -> str:
    return "cal-" + sha256_json(
        {
            "layer": name,
            "min": minimum,
            "max": maximum,
            "samples": samples,
            "source_hash": source_hash,
        }
    )[:16]


def _global_calibration_version(
    source_hash: str, layers: list[LayerCalibration] | tuple[LayerCalibration, ...]
) -> str:
    return "cal-" + sha256_json(
        {
            "source_hash": source_hash,
            "layers": [item.to_dict() for item in layers],
        }
    )[:16]


def empty_calibration(source_hash: str = "") -> CalibrationStats:
    layers: list[LayerCalibration] = []
    for name in CONV_LAYERS:
        layers.append(
            LayerCalibration(
                name=name,
                minimum=0.0,
                maximum=0.0,
                samples=0,
                source_hash=source_hash,
                version=_layer_calibration_version(name, 0.0, 0.0, 0, source_hash),
            )
        )
    # The classifier head still receives a calibration binding for cache/trace completeness.
    layers.append(
        LayerCalibration(
            name="fc",
            minimum=0.0,
            maximum=0.0,
            samples=0,
            source_hash=source_hash,
            version=_layer_calibration_version("fc", 0.0, 0.0, 0, source_hash),
        )
    )
    return CalibrationStats(
        version=_global_calibration_version(source_hash, layers),
        source_hash=source_hash,
        sample_count=0,
        layers=tuple(layers),
    )


def estimate_calibration(
    weights: NetworkWeights,
    calibration_images: np.ndarray,
    *,
    samples_by_layer: dict[str, int] | None = None,
    source_hash: str | None = None,
) -> CalibrationStats:
    """Estimate post-ReLU convolution activation ranges on a local batch."""

    images = np.asarray(calibration_images, dtype=np.float64)
    if images.ndim == 2:
        images = images[None, :, :]
    if images.ndim != 3:
        raise ValueError("calibration images must have shape (batch, height, width)")
    source = source_hash or array_checksum(images)
    x = images[:, None, :, :]
    activation = relu(conv2d_valid(x, weights.weight("conv1"), weights.bias("conv1")))
    first = activation
    second = relu(
        conv2d_valid(
            max_pool2d_local(first),
            weights.weight("conv2"),
            weights.bias("conv2"),
        )
    )
    activations = {"conv1": first, "conv2": second}
    layers: list[LayerCalibration] = []
    for name in CONV_LAYERS:
        sample_count = int((samples_by_layer or {}).get(name, images.shape[0]))
        sample_count = max(1, min(sample_count, images.shape[0]))
        selected = activations[name][:sample_count]
        minimum = float(np.min(selected))
        maximum = float(np.max(selected))
        layers.append(
            LayerCalibration(
                name=name,
                minimum=minimum,
                maximum=maximum,
                samples=sample_count,
                source_hash=source,
                version=_layer_calibration_version(name, minimum, maximum, sample_count, source),
            )
        )
    head_samples = int((samples_by_layer or {}).get("fc", images.shape[0]))
    head_samples = max(1, min(head_samples, images.shape[0]))
    layers.append(
        LayerCalibration(
            name="fc",
            minimum=0.0,
            maximum=0.0,
            samples=head_samples,
            source_hash=source,
            version=_layer_calibration_version("fc", 0.0, 0.0, head_samples, source),
        )
    )
    return CalibrationStats(
        version=_global_calibration_version(source, layers),
        source_hash=source,
        sample_count=max(item.samples for item in layers),
        layers=tuple(layers),
    )


def max_pool2d_local(array: np.ndarray) -> np.ndarray:
    from .model import max_pool2d

    return max_pool2d(array, 2)


def calibration_activation_range(calibration: CalibrationStats, layer: str) -> tuple[float, float] | None:
    item = calibration.layer(layer)
    if item is None or item.samples <= 0:
        return None
    return item.minimum, item.maximum
