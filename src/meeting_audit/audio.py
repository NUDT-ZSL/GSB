from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator, Literal

import numpy as np

from .config import AuditConfig, DEFAULT_CONFIG


ArithmeticPath = Literal["float", "fixed"]


def frame_count(total_samples: int, config: AuditConfig = DEFAULT_CONFIG) -> int:
    if total_samples < config.frame_samples:
        return 0
    return (total_samples - config.frame_samples) // config.hop_samples + 1


def frame_ranges(
    total_samples: int, config: AuditConfig = DEFAULT_CONFIG
) -> list[tuple[int, int, int]]:
    """Return ``(frame_index, start_sample, end_sample)`` with exact indices."""
    count = frame_count(total_samples, config)
    return [
        (index, config.frame_start_sample(index), config.frame_start_sample(index) + config.frame_samples)
        for index in range(count)
    ]


def extract_frame(
    pcm: np.ndarray, frame_index: int, config: AuditConfig = DEFAULT_CONFIG
) -> np.ndarray:
    start = config.frame_start_sample(frame_index)
    end = start + config.frame_samples
    if end > len(pcm):
        raise IndexError(f"frame {frame_index} exceeds PCM length: {end} > {len(pcm)}")
    return pcm[start:end]


@dataclass(frozen=True)
class FrameFeatures:
    frame_index: int
    start_sample: int
    end_sample: int
    at_ms: int
    rms_q: int
    zcr_count: int
    energy_sum_sq: int
    rms_float: float
    zcr_float: float
    path: str

    def to_dict(self) -> dict[str, object]:
        return {
            "frame_index": self.frame_index,
            "start_sample": self.start_sample,
            "end_sample": self.end_sample,
            "at_ms": self.at_ms,
            "frame_center_ms": self.at_ms,
            "rms_q": self.rms_q,
            "zcr_count": self.zcr_count,
            "energy_sum_sq": self.energy_sum_sq,
            "rms_float": round(self.rms_float, 9),
            "zcr_float": round(self.zcr_float, 9),
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "FrameFeatures":
        allowed = {
            "frame_index",
            "start_sample",
            "end_sample",
            "at_ms",
            "rms_q",
            "zcr_count",
            "energy_sum_sq",
            "rms_float",
            "zcr_float",
            "path",
        }
        kwargs = {key: value[key] for key in allowed if key in value}
        kwargs.setdefault("energy_sum_sq", 0)
        return cls(**kwargs)


def _rounded_integer_rms_fixed(frame: np.ndarray) -> int:
    samples = frame.astype(np.int64, copy=False)
    sum_squares = int(np.sum(samples * samples))
    length = len(frame)
    floor_rms = math.isqrt(sum_squares // length)
    # Nearest-integer test against (floor_rms + 0.5)^2.
    threshold_numerator = length * (4 * floor_rms * floor_rms + 4 * floor_rms + 1)
    if 4 * sum_squares >= threshold_numerator:
        return floor_rms + 1
    return floor_rms


def extract_features(
    frame: np.ndarray,
    frame_index: int,
    config: AuditConfig = DEFAULT_CONFIG,
    path: ArithmeticPath = "fixed",
) -> FrameFeatures:
    if len(frame) != config.frame_samples:
        raise ValueError(f"expected {config.frame_samples} samples, got {len(frame)}")
    if frame.dtype != np.int16:
        frame = frame.astype(np.int16, copy=False)

    signs_negative = frame < 0
    zcr_count = int(np.count_nonzero(signs_negative[:-1] != signs_negative[1:]))
    int64_samples = frame.astype(np.int64, copy=False)
    energy_sum_sq = int(np.sum(int64_samples * int64_samples))

    if path == "fixed":
        rms_q = _rounded_integer_rms_fixed(frame)
        rms_float = float(rms_q)
        zcr_float = zcr_count / (config.frame_samples - 1)
    elif path == "float":
        normalized = frame.astype(np.float64) / 32768.0
        rms_raw = float(np.sqrt(np.mean(normalized * normalized)) * 32768.0)
        rms_q = int(np.rint(rms_raw))
        rms_float = rms_raw
        zcr_float = float(
            np.count_nonzero(np.signbit(normalized[:-1]) != np.signbit(normalized[1:]))
            / (config.frame_samples - 1)
        )
    else:
        raise ValueError(f"unknown arithmetic path: {path}")

    start = config.frame_start_sample(frame_index)
    return FrameFeatures(
        frame_index=frame_index,
        start_sample=start,
        end_sample=start + config.frame_samples,
        at_ms=config.frame_center_ms(frame_index),
        rms_q=rms_q,
        zcr_count=zcr_count,
        energy_sum_sq=energy_sum_sq,
        rms_float=rms_float,
        zcr_float=zcr_float,
        path=path,
    )


def extract_all_features(
    pcm: np.ndarray,
    config: AuditConfig = DEFAULT_CONFIG,
    path: ArithmeticPath = "fixed",
) -> list[FrameFeatures]:
    return [
        extract_features(extract_frame(pcm, index, config), index, config, path)
        for index in range(frame_count(len(pcm), config))
    ]


class ChunkedFrameFeatureStream:
    """Accept arbitrary PCM chunk boundaries and emit exact global frames.

    A hop-sized suffix is retained so a frame split across two ``push`` calls is
    reconstructed byte-for-byte.  Flush discards only the incomplete tail, which
    is identical to the batch complete-frame policy.
    """

    def __init__(self, config: AuditConfig = DEFAULT_CONFIG, path: ArithmeticPath = "fixed"):
        self.config = config
        self.path = path
        self._buffer = np.zeros(0, dtype=np.int16)
        self._frame_index = 0

    def push(self, chunk: bytes | np.ndarray) -> list[FrameFeatures]:
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            incoming = np.frombuffer(bytes(chunk), dtype="<i2")
        else:
            incoming = np.asarray(chunk, dtype=np.int16)
        if incoming.size:
            self._buffer = np.concatenate([self._buffer, incoming.astype(np.int16, copy=False)])

        emitted: list[FrameFeatures] = []
        while len(self._buffer) >= self.config.frame_samples:
            frame = self._buffer[: self.config.frame_samples].copy()
            emitted.append(extract_features(frame, self._frame_index, self.config, self.path))
            self._frame_index += 1
            self._buffer = self._buffer[self.config.hop_samples :].copy()
        return emitted

    def flush(self) -> list[FrameFeatures]:
        # The tail is intentionally incomplete under the batch framing rule.
        self._buffer = np.zeros(0, dtype=np.int16)
        return []


def iter_byte_chunks(pcm: np.ndarray, chunk_sample_sizes: list[int]) -> Iterator[bytes]:
    offset = 0
    for size in chunk_sample_sizes:
        yield pcm[offset : offset + size].tobytes()
        offset += size
    if offset < len(pcm):
        yield pcm[offset:].tobytes()
