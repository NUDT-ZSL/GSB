"""Deterministic normalization, seeded masking and mask-aware completion."""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from .codec import to_native
from .io_utils import sha256_hex


@dataclass(frozen=True)
class PreprocessConfig:
    seed: int = 406
    mask_blocks: int = 2
    mask_length: int = 5
    mask_decay: float = 0.25
    normalization_epsilon: float = 1e-6
    smooth_kernel: int = 5


@dataclass(frozen=True)
class NormalizationStats:
    mean: np.ndarray
    scale: np.ndarray

    @staticmethod
    def default(dim: int) -> "NormalizationStats":
        return NormalizationStats(np.zeros(dim, dtype=np.float64), np.ones(dim, dtype=np.float64))

    @staticmethod
    def from_windows(windows: Sequence[np.ndarray], epsilon: float = 1e-6) -> "NormalizationStats":
        matrix = np.stack([np.asarray(w, dtype=np.float64).reshape(-1) for w in windows])
        mean = matrix.mean(axis=0)
        scale = matrix.std(axis=0)
        scale = np.where(scale < epsilon, 1.0, scale)
        return NormalizationStats(mean=mean, scale=scale)


@dataclass
class BoundaryTrace:
    window_id: str
    dimension: int
    time_index: int
    sensor_index: int
    trace_type: str
    original_value: float
    completed_value: float
    delta: float
    mask_left: int | None
    mask_right: int | None
    weight_left: float
    weight_right: float

    def to_dict(self) -> dict:
        return to_native(self)


@dataclass
class PreprocessedWindow:
    window_id: str
    raw: np.ndarray
    normalized: np.ndarray
    mask: np.ndarray
    completed: np.ndarray
    contribution_weights: np.ndarray
    input_shape: tuple[int, ...]
    traces: list[BoundaryTrace] = field(default_factory=list)

    @property
    def dim(self) -> int:
        return int(self.normalized.size)

    def trace_dicts(self) -> list[dict]:
        return [item.to_dict() for item in self.traces]


def window_seed(root_seed: int, window_id: str) -> int:
    return (int(root_seed) ^ zlib.crc32(window_id.encode("utf-8"))) & 0xFFFFFFFF


class WindowPreprocessor:
    """Normalize, deterministically mask and complete one window."""

    def __init__(
        self,
        config: PreprocessConfig | None = None,
        stats: NormalizationStats | None = None,
        input_shape: tuple[int, int] = (32, 4),
    ):
        self.config = config or PreprocessConfig()
        self.input_shape = tuple(input_shape)
        dim = int(np.prod(input_shape))
        self.stats = stats or NormalizationStats.default(dim)
        if self.stats.mean.size != dim or self.stats.scale.size != dim:
            raise ValueError("normalization stats dimension does not match input shape")

    def process(self, window_id: str, values: np.ndarray | Sequence[float]) -> PreprocessedWindow:
        raw_array = np.asarray(values, dtype=np.float64)
        input_shape = tuple(raw_array.shape) if raw_array.ndim == 2 else self.input_shape
        raw = raw_array.reshape(-1).copy()
        dim = raw.size

        normalized = self._normalize(raw)
        mask = self._generate_mask(dim, normalized)
        completed, traces = self._complete(window_id, normalized, mask, input_shape)
        weights = np.where(mask, self.config.mask_decay, 1.0).astype(np.float64)

        return PreprocessedWindow(
            window_id=window_id,
            raw=raw,
            normalized=normalized,
            mask=mask,
            completed=completed,
            contribution_weights=weights,
            input_shape=input_shape,
            traces=traces,
        )

    def _normalize(self, raw: np.ndarray) -> np.ndarray:
        cfg = self.config
        if np.any(self.stats.scale != 1.0) or np.any(self.stats.mean != 0.0):
            return ((raw - self.stats.mean) / self.stats.scale).astype(np.float64, copy=True)
        center = float(raw.mean())
        scale = float(raw.std())
        if scale < cfg.normalization_epsilon:
            scale = 1.0
        return (raw - center) / scale

    def _generate_mask(self, dim: int, normalized: np.ndarray) -> np.ndarray:
        cfg = self.config
        input_digest = int(sha256_hex(np.ascontiguousarray(normalized).tobytes())[:8], 16)
        seed = (int(cfg.seed) ^ input_digest) & 0xFFFFFFFF
        rng = np.random.default_rng(seed)
        mask = np.zeros(dim, dtype=bool)
        length = max(1, min(int(cfg.mask_length), dim // 6))
        if dim <= 2 * length + 2:
            length = max(1, dim // 8)
        for _ in range(max(1, int(cfg.mask_blocks))):
            upper = max(length + 2, dim - length - 1)
            start = int(rng.integers(1, upper))
            end = min(dim - 1, start + length)
            if end - start < length:
                start = max(1, end - length)
            mask[start:end] = True
        # Never mask both first and last positions for stable interpolation.
        mask[0] = False
        mask[-1] = False
        return mask

    def _complete(
        self,
        window_id: str,
        normalized: np.ndarray,
        mask: np.ndarray,
        input_shape: tuple[int, ...],
    ) -> tuple[np.ndarray, list[BoundaryTrace]]:
        completed = normalized.copy()
        traces: list[BoundaryTrace] = []
        dim = normalized.size

        for start, end in _mask_runs(mask):
            left = start - 1
            right = end
            has_left = left >= 0
            has_right = right < dim

            if has_left and has_right:
                count = end - start
                fill = np.linspace(
                    normalized[left],
                    normalized[right],
                    count + 2,
                    dtype=np.float64,
                )[1:-1]
                completed[start:end] = fill
            elif has_left:
                completed[start:end] = normalized[left]
            elif has_right:
                completed[start:end] = normalized[right]

            for index in range(start, end):
                traces.append(
                    self._trace(
                        window_id,
                        index,
                        "masked_imputation",
                        normalized[index],
                        completed[index],
                        bool(mask[index - 1]) if index > 0 else None,
                        bool(mask[index + 1]) if index + 1 < dim else None,
                        input_shape,
                    )
                )

            if has_left:
                traces.append(
                    self._trace(
                        window_id,
                        left,
                        "left_boundary_weight",
                        normalized[left],
                        completed[left],
                        bool(mask[left - 1]) if left > 0 else None,
                        bool(mask[left]),
                        input_shape,
                    )
                )
            if has_right:
                traces.append(
                    self._trace(
                        window_id,
                        right,
                        "right_boundary_weight",
                        normalized[right],
                        completed[right],
                        bool(mask[right - 1]),
                        bool(mask[right + 1]) if right + 1 < dim else None,
                        input_shape,
                    )
                )

        return completed, traces

    def _trace(
        self,
        window_id: str,
        dimension: int,
        trace_type: str,
        original: float,
        completed: float,
        mask_left: bool | None,
        mask_right: bool | None,
        input_shape: tuple[int, ...],
    ) -> BoundaryTrace:
        if len(input_shape) == 2:
            time_index, sensor_index = np.unravel_index(dimension, input_shape)
        else:
            time_index, sensor_index = dimension, 0
        return BoundaryTrace(
            window_id=window_id,
            dimension=int(dimension),
            time_index=int(time_index),
            sensor_index=int(sensor_index),
            trace_type=trace_type,
            original_value=float(original),
            completed_value=float(completed),
            delta=float(completed - original),
            mask_left=None if mask_left is None else int(mask_left),
            mask_right=None if mask_right is None else int(mask_right),
            weight_left=1.0 if mask_left is False else self.config.mask_decay if mask_left else 0.0,
            weight_right=1.0 if mask_right is False else self.config.mask_decay if mask_right else 0.0,
        )


def _mask_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    runs = []
    start = None
    for index, value in enumerate(mask):
        if value and start is None:
            start = index
        elif not value and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(mask)))
    return runs
