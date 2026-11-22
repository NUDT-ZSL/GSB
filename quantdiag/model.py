from __future__ import annotations

from dataclasses import dataclass

import numpy as np


IMAGE_SIZE = 16
NUM_CLASSES = 4
DEFAULT_NETWORK_SEED = 20261123
LAYER_ORDER = ("conv1", "conv2", "fc")
CONV_LAYERS = ("conv1", "conv2")


@dataclass(frozen=True)
class ConvSpec:
    name: str
    in_channels: int
    out_channels: int
    kernel: int = 3
    pool: int = 2


NETWORK_SPECS = {
    "conv1": ConvSpec("conv1", 1, 4),
    "conv2": ConvSpec("conv2", 4, 8),
}


class NetworkWeights:
    """Small fixed CNN whose parameters are derived only from a local seed."""

    def __init__(self, seed: int = DEFAULT_NETWORK_SEED) -> None:
        self.seed = int(seed)
        rng = np.random.default_rng(self.seed)
        self._weights: dict[str, np.ndarray] = {}
        self._biases: dict[str, np.ndarray] = {}

        for name in CONV_LAYERS:
            spec = NETWORK_SPECS[name]
            fan_in = spec.in_channels * spec.kernel * spec.kernel
            scale = 1.0 / np.sqrt(fan_in)
            self._weights[name] = rng.normal(
                0.0, scale, size=(spec.out_channels, spec.in_channels, spec.kernel, spec.kernel)
            ).astype(np.float64)
            self._biases[name] = rng.normal(0.0, 0.03, size=(spec.out_channels,)).astype(np.float64)

        # conv2 output: 16 -> conv 14 -> pool 7 -> conv 5 -> pool 2.
        fc_in = 8 * 2 * 2
        self._weights["fc"] = rng.normal(0.0, 1.0 / np.sqrt(fc_in), size=(NUM_CLASSES, fc_in)).astype(
            np.float64
        )
        self._biases["fc"] = rng.normal(0.0, 0.03, size=(NUM_CLASSES,)).astype(np.float64)

    def weight(self, layer: str) -> np.ndarray:
        return self._weights[layer]

    def bias(self, layer: str) -> np.ndarray:
        return self._biases[layer]

    def fingerprint(self) -> str:
        from .utils import sha256_json

        return sha256_json(
            {
                name: {
                    "weight": self._weights[name].reshape(-1).tolist(),
                    "bias": self._biases[name].reshape(-1).tolist(),
                }
                for name in LAYER_ORDER
            }
        )

    def base_scale(self, layer: str) -> float:
        return max(float(np.max(np.abs(self.weight(layer)))) / 127.0, 1e-9)

    def channel_scales(self, layer: str) -> tuple[float, ...]:
        weight = self.weight(layer)
        if layer == "fc":
            axes = tuple(range(1, weight.ndim))
        else:
            axes = tuple(range(1, weight.ndim))
        maximum = np.max(np.abs(weight), axis=axes)
        return tuple(max(float(v) / 127.0, 1e-9) for v in maximum)


def relu(array: np.ndarray) -> np.ndarray:
    return np.maximum(array, 0.0)


def conv2d_valid(images: np.ndarray, weights: np.ndarray, bias: np.ndarray) -> np.ndarray:
    batch, in_channels, height, width = images.shape
    out_channels, weight_channels, kernel, _ = weights.shape
    if in_channels != weight_channels:
        raise ValueError("convolution channel mismatch")
    out_h = height - kernel + 1
    out_w = width - kernel + 1
    output = np.zeros((batch, out_channels, out_h, out_w), dtype=np.float64)
    for b in range(batch):
        for oc in range(out_channels):
            kernel_values = weights[oc]
            for oy in range(out_h):
                for ox in range(out_w):
                    total = float(bias[oc])
                    for ic in range(in_channels):
                        for ky in range(kernel):
                            for kx in range(kernel):
                                total += float(images[b, ic, oy + ky, ox + kx]) * float(
                                    kernel_values[ic, ky, kx]
                                )
                    output[b, oc, oy, ox] = total
    return output


def max_pool2d(array: np.ndarray, size: int = 2) -> np.ndarray:
    batch, channels, height, width = array.shape
    out_h = height // size
    out_w = width // size
    output = np.empty((batch, channels, out_h, out_w), dtype=np.float64)
    for b in range(batch):
        for c in range(channels):
            for oy in range(out_h):
                for ox in range(out_w):
                    block = array[b, c, oy * size : (oy + 1) * size, ox * size : (ox + 1) * size]
                    output[b, c, oy, ox] = float(np.max(block))
    return output


def linear_exact(inputs: np.ndarray, weights: np.ndarray, bias: np.ndarray) -> np.ndarray:
    rows, in_features = inputs.shape
    out_features = weights.shape[0]
    if in_features != weights.shape[1]:
        raise ValueError("linear feature mismatch")
    output = np.empty((rows, out_features), dtype=np.float64)
    for row in range(rows):
        for out_feature in range(out_features):
            total = float(bias[out_feature])
            for index in range(in_features):
                total += float(inputs[row, index]) * float(weights[out_feature, index])
            output[row, out_feature] = total
    return output


def forward_float(
    weights: NetworkWeights,
    images: np.ndarray,
    *,
    collect: bool = True,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    images = np.asarray(images, dtype=np.float64)
    if images.ndim != 3 or images.shape[1:] != (IMAGE_SIZE, IMAGE_SIZE):
        raise ValueError(f"expected images with shape (batch, {IMAGE_SIZE}, {IMAGE_SIZE})")
    x = images[:, None, :, :]
    outputs: dict[str, np.ndarray] = {}

    conv = conv2d_valid(x, weights.weight("conv1"), weights.bias("conv1"))
    activation = relu(conv)
    output = max_pool2d(activation)
    if collect:
        outputs["conv1"] = output

    conv = conv2d_valid(output, weights.weight("conv2"), weights.bias("conv2"))
    activation = relu(conv)
    output = max_pool2d(activation)
    if collect:
        outputs["conv2"] = output

    flat = output.reshape(output.shape[0], -1)
    scores = linear_exact(flat, weights.weight("fc"), weights.bias("fc"))
    if collect:
        outputs["fc"] = scores
    return scores, outputs


def validate_images(images: np.ndarray) -> np.ndarray:
    array = np.asarray(images, dtype=np.float64)
    if array.ndim == 2:
        array = array[None, :, :]
    if array.ndim != 3 or array.shape[1:] != (IMAGE_SIZE, IMAGE_SIZE):
        raise ValueError(f"expected images with shape (batch, {IMAGE_SIZE}, {IMAGE_SIZE})")
    if not np.all(np.isfinite(array)):
        raise ValueError("images contain non-finite values")
    return np.ascontiguousarray(array, dtype=np.float64)


def generate_image_batch(count: int = 8, seed: int = 20261123) -> tuple[np.ndarray, np.ndarray]:
    """Generate deterministic grayscale images and labels without external data."""

    if count <= 0:
        raise ValueError("count must be positive")
    rng = np.random.default_rng(int(seed))
    yy, xx = np.mgrid[0:IMAGE_SIZE, 0:IMAGE_SIZE]
    images = np.empty((count, IMAGE_SIZE, IMAGE_SIZE), dtype=np.float64)
    labels = np.empty((count,), dtype=np.int64)
    for index in range(count):
        label = index % NUM_CLASSES
        phase = 0.31 * (index + 1)
        if label == 0:
            pattern = np.sin((yy + phase) * 0.9)
        elif label == 1:
            pattern = np.sin((xx - phase) * 0.8)
        elif label == 2:
            pattern = np.sin((xx + yy + phase) * 0.55)
        else:
            radius = np.sqrt((yy - 7.5) ** 2 + (xx - 7.5) ** 2)
            pattern = np.sin(radius * 1.35 - phase)
        texture = rng.normal(0.0, 0.035, size=(IMAGE_SIZE, IMAGE_SIZE))
        image = 0.5 + 0.32 * pattern + texture
        images[index] = np.clip(image, 0.0, 1.0)
        labels[index] = label
    return images, labels
