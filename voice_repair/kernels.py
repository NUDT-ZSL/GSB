from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from .errors import RepairError
from .task import DCParams, DeclickParams, DenoiseParams, NormalizeParams


FetchInterval = Callable[[int, int], np.ndarray]

DENOISE_EPSILON = 1e-15
OLA_EPSILON = 1e-12


def remove_dc(values: np.ndarray, params: DCParams, global_mean: float | None) -> np.ndarray:
    if params.mode == "mean":
        if global_mean is None:
            raise RepairError("E_DC_MEAN", "global mean is required for dc.mode='mean'")
        return values.astype(np.float64, copy=False) - float(global_mean)
    return values.astype(np.float64, copy=False) - params.offset


def declick_padded(
    padded: np.ndarray,
    core_length: int,
    params: DeclickParams,
) -> np.ndarray:
    radius = params.radius
    needed = core_length + 2 * radius
    if padded.size != needed:
        raise RepairError(
            "E_DECLICK_CONTEXT",
            f"declick context has {padded.size} samples, expected {needed}",
        )
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * radius + 1)
    medians = np.median(windows[:core_length], axis=1)
    center = padded[radius : radius + core_length].copy()
    replace = np.abs(center - medians) > params.threshold
    center[replace] = medians[replace]
    return center


def declick_full(values: np.ndarray, params: DeclickParams) -> np.ndarray:
    radius = params.radius
    padded = np.pad(values.astype(np.float64, copy=False), radius, mode="edge")
    return declick_padded(padded, values.size, params)


def declick_interval(
    fetch: FetchInterval,
    total_samples: int,
    start: int,
    end: int,
    params: DeclickParams,
) -> np.ndarray:
    radius = params.radius
    ideal_start = start - radius
    ideal_end = end + radius
    fetch_start = max(0, ideal_start)
    fetch_end = min(total_samples, ideal_end)
    fetched = fetch(fetch_start, fetch_end)
    if fetched.size != fetch_end - fetch_start:
        raise RepairError("E_CONTEXT_SHORT_READ", "declick source returned a short interval")
    left_pad = fetch_start - ideal_start
    right_pad = ideal_end - fetch_end
    padded = np.pad(fetched, (left_pad, right_pad), mode="edge")
    return declick_padded(padded, end - start, params)


@dataclass(frozen=True)
class DenoiseModel:
    total_samples: int
    params: DenoiseParams
    window: np.ndarray
    window_square: np.ndarray
    frame_count: int

    @classmethod
    def build(cls, total_samples: int, params: DenoiseParams) -> DenoiseModel:
        if total_samples <= 0:
            raise RepairError("E_WAV_EMPTY", "cannot denoise an empty signal")
        n = params.frame_size
        index = np.arange(n, dtype=np.float64)
        window = 0.5 - 0.5 * np.cos(2.0 * np.pi * index / n)
        hop = params.hop_size
        if total_samples <= n:
            frame_count = 1
        else:
            frame_count = math.ceil((total_samples - n) / hop) + 1
        return cls(
            total_samples=total_samples,
            params=params,
            window=window,
            window_square=window * window,
            frame_count=frame_count,
        )

    @property
    def frame_size(self) -> int:
        return self.params.frame_size

    @property
    def hop_size(self) -> int:
        return self.params.hop_size

    def frame_bounds(self, frame_index: int) -> tuple[int, int]:
        start = frame_index * self.hop_size
        return start, min(self.total_samples, start + self.frame_size)

    def _extract_frame(self, values: np.ndarray, offset: int, frame_index: int) -> np.ndarray:
        frame = np.zeros(self.frame_size, dtype=np.float64)
        global_start, global_end = self.frame_bounds(frame_index)
        local_start = global_start - offset
        local_end = global_end - offset
        if local_start < 0 or local_end > values.size:
            raise RepairError("E_CONTEXT_SHORT_READ", "denoise source interval is too small")
        frame[: global_end - global_start] = values[local_start:local_end]
        return frame

    def noise_profile(self, fetch: FetchInterval) -> np.ndarray:
        count = min(self.params.noise_frames, self.frame_count)
        last_end = min(
            self.total_samples,
            (count - 1) * self.hop_size + self.frame_size,
        )
        values = fetch(0, last_end)
        magnitudes = []
        for frame_index in range(count):
            frame = self._extract_frame(values, 0, frame_index)
            spectrum = np.fft.rfft(frame * self.window)
            magnitudes.append(np.abs(spectrum))
        return np.mean(np.stack(magnitudes, axis=0), axis=0, dtype=np.float64)

    def clean_frame(self, frame: np.ndarray, noise: np.ndarray) -> np.ndarray:
        spectrum = np.fft.rfft(frame * self.window)
        magnitude = np.abs(spectrum)
        clean_magnitude = np.maximum(
            magnitude - self.params.oversubtraction * noise,
            self.params.floor_ratio * magnitude,
        )
        ratio = clean_magnitude / np.maximum(magnitude, DENOISE_EPSILON)
        return np.fft.irfft(spectrum * ratio, n=self.frame_size) * self.window

    def covering_frames(self, start: int, end: int) -> tuple[int, int]:
        if end <= start:
            raise RepairError("E_SAMPLE_RANGE", "denoise interval must be non-empty")
        first = max(0, (start - self.frame_size) // self.hop_size + 1)
        last = min(self.frame_count - 1, (end - 1) // self.hop_size)
        return first, last

    def denoise_interval(
        self,
        fetch: FetchInterval,
        noise: np.ndarray,
        start: int,
        end: int,
    ) -> np.ndarray:
        first, last = self.covering_frames(start, end)
        input_start = first * self.hop_size
        input_end = min(self.total_samples, last * self.hop_size + self.frame_size)
        values = fetch(input_start, input_end)
        length = end - start
        numerator = np.zeros(length, dtype=np.float64)
        denominator = np.zeros(length, dtype=np.float64)

        for frame_index in range(first, last + 1):
            frame = self._extract_frame(values, input_start, frame_index)
            cleaned = self.clean_frame(frame, noise)
            frame_start, frame_end = self.frame_bounds(frame_index)
            overlap_start = max(start, frame_start)
            overlap_end = min(end, frame_end)
            if overlap_end <= overlap_start:
                continue
            source_start = overlap_start - frame_start
            source_end = overlap_end - frame_start
            target_start = overlap_start - start
            target_end = overlap_end - start
            numerator[target_start:target_end] += cleaned[source_start:source_end]
            denominator[target_start:target_end] += self.window_square[
                source_start:source_end
            ]

        output = np.zeros(length, dtype=np.float64)
        np.divide(numerator, denominator, out=output, where=denominator > OLA_EPSILON)
        return output


def denoise_full(values: np.ndarray, params: DenoiseParams) -> np.ndarray:
    model = DenoiseModel.build(values.size, params)
    full = np.asarray(values, dtype=np.float64)

    def fetch(start: int, end: int) -> np.ndarray:
        return full[start:end]

    noise = model.noise_profile(fetch)
    numerator = np.zeros(values.size, dtype=np.float64)
    denominator = np.zeros(values.size, dtype=np.float64)
    for frame_index in range(model.frame_count):
        frame = model._extract_frame(full, 0, frame_index)
        cleaned = model.clean_frame(frame, noise)
        frame_start, frame_end = model.frame_bounds(frame_index)
        length = frame_end - frame_start
        numerator[frame_start:frame_end] += cleaned[:length]
        denominator[frame_start:frame_end] += model.window_square[:length]
    output = np.zeros(values.size, dtype=np.float64)
    np.divide(numerator, denominator, out=output, where=denominator > OLA_EPSILON)
    return output


def normalization_gain(values: np.ndarray, params: NormalizeParams) -> float:
    peak = float(np.max(np.abs(values))) if values.size else 0.0
    if not math.isfinite(peak):
        raise RepairError("E_NORMALIZE_NONFINITE", "signal peak is not finite")
    if peak <= DENOISE_EPSILON:
        return 1.0
    return params.target_peak / peak


def apply_normalization(values: np.ndarray, gain: float) -> np.ndarray:
    return np.asarray(values, dtype=np.float64) * gain


def serial_reference_pipeline(
    input_values: np.ndarray,
    *,
    global_mean: float,
    dc: DCParams,
    declick: DeclickParams,
    denoise: DenoiseParams,
    normalize: NormalizeParams,
) -> tuple[np.ndarray, dict[str, float]]:
    dc_values = remove_dc(input_values, dc, global_mean)
    declicked = declick_full(dc_values, declick)
    denoised = denoise_full(declicked, denoise)
    gain = normalization_gain(denoised, normalize)
    normalized = apply_normalization(denoised, gain)
    metrics = {
        "dc_mean": float(global_mean),
        "denoise_peak": float(np.max(np.abs(denoised))),
        "normalization_gain": float(gain),
    }
    return normalized, metrics
