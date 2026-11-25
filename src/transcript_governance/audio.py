"""Deterministic PCM synthesis, framing and frame-level audio features."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np


SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class FrameConfig:
    sample_rate: int = SAMPLE_RATE
    frame_ms: int = 25
    hop_ms: int = 10

    def __post_init__(self) -> None:
        if self.sample_rate != SAMPLE_RATE:
            raise ValueError("this deterministic component currently uses 16 kHz PCM")
        if self.frame_ms <= 0 or self.hop_ms <= 0:
            raise ValueError("frame_ms and hop_ms must be positive")
        if self.hop_ms > self.frame_ms:
            raise ValueError("hop_ms cannot exceed frame_ms")

    @property
    def frame_samples(self) -> int:
        return self.frame_ms * self.sample_rate // 1000

    @property
    def hop_samples(self) -> int:
        return self.hop_ms * self.sample_rate // 1000

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_rate": self.sample_rate,
            "frame_ms": self.frame_ms,
            "hop_ms": self.hop_ms,
            "frame_samples": self.frame_samples,
            "hop_samples": self.hop_samples,
        }


@dataclass(frozen=True)
class Frame:
    index: int
    start_sample: int
    nominal_end_sample: int
    source_end_sample: int
    start_ms: int
    end_ms: int
    actual_end_ms: int
    padded: bool
    energy: float
    rms: float
    zero_crossing_rate: float
    is_speech: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start_sample": self.start_sample,
            "nominal_end_sample": self.nominal_end_sample,
            "source_end_sample": self.source_end_sample,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "actual_end_ms": self.actual_end_ms,
            "padded": self.padded,
            "energy": self.energy,
            "rms": self.rms,
            "zero_crossing_rate": self.zero_crossing_rate,
            "is_speech": self.is_speech,
        }


@dataclass(frozen=True)
class AudioFeatures:
    pcm_sha256: str
    sample_count: int
    duration_ms: int
    config: FrameConfig
    frames: tuple[Frame, ...]
    energy_quantiles: dict[str, float]
    energy_threshold: float
    speech_segments_ms: tuple[tuple[int, int], ...]
    vad_config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_frames: bool = False) -> dict[str, Any]:
        data = {
            "pcm_sha256": self.pcm_sha256,
            "sample_count": self.sample_count,
            "duration_ms": self.duration_ms,
            "config": self.config.to_dict(),
            "frame_count": len(self.frames),
            "energy_quantiles": self.energy_quantiles,
            "energy_threshold": self.energy_threshold,
            "speech_segments_ms": [list(item) for item in self.speech_segments_ms],
            "vad_config": self.vad_config,
        }
        if include_frames:
            data["frames"] = [frame.to_dict() for frame in self.frames]
        return data


@dataclass(frozen=True)
class PcmSample:
    samples: np.ndarray
    pcm_bytes: bytes
    sample_rate: int
    gain: float
    seed: int
    speech_segments_ms: tuple[tuple[int, int], ...]
    silence_segments_ms: tuple[tuple[int, int], ...]

    @property
    def duration_ms(self) -> int:
        return len(self.samples) * 1000 // self.sample_rate


DEFAULT_PLAN_MS: tuple[tuple[str, int], ...] = (
    ("speech", 1150),
    ("silence", 650),
    ("speech", 1350),
    ("silence", 520),
    ("speech", 1250),
    ("silence", 700),
    ("speech", 1700),
    ("silence", 430),
)


def _speech_chunk(duration_ms: int, seed: int, segment_index: int) -> np.ndarray:
    sample_rate = SAMPLE_RATE
    count = duration_ms * sample_rate // 1000
    t = np.arange(count, dtype=np.float64) / sample_rate
    rng = np.random.default_rng(seed * 10_000 + segment_index + 17)

    f0 = 118.0 + 18.0 * np.sin(2.0 * math.pi * (2.1 + 0.17 * segment_index) * t)
    phase = 2.0 * math.pi * np.cumsum(f0) / sample_rate
    voiced = (
        0.58 * np.sin(phase)
        + 0.20 * np.sin(2.0 * phase + 0.4)
        + 0.09 * np.sin(3.0 * phase - 0.7)
    )

    syllable_rate = 3.4 + 0.35 * segment_index
    envelope = 0.35 + 0.65 * (0.5 + 0.5 * np.sin(2.0 * math.pi * syllable_rate * t - 0.8))
    attack = np.minimum(1.0, t / 0.035)
    release = np.minimum(1.0, (duration_ms / 1000.0 - t) / 0.055)
    edge = np.clip(np.minimum(attack, release), 0.0, 1.0)

    noise = rng.normal(0.0, 1.0, count)
    consonant_env = np.clip(np.sin(2.0 * math.pi * (7.5 + segment_index) * t), 0.0, 1.0) ** 4
    unvoiced = 0.16 * noise * consonant_env

    chunk = 0.42 * envelope * edge * voiced + 0.12 * edge * unvoiced
    peak = float(np.max(np.abs(chunk))) if count else 1.0
    if peak > 0:
        chunk = chunk / max(peak, 0.8) * 0.82
    return chunk.astype(np.float32)


def synthesize_sample_pcm(
    seed: int = 421,
    gain: float = 0.72,
    plan_ms: Sequence[tuple[str, int]] = DEFAULT_PLAN_MS,
) -> PcmSample:
    """Synthesize deterministic 16 kHz mono PCM from speech/silence blocks."""

    if gain <= 0:
        raise ValueError("gain must be positive")
    pieces: list[np.ndarray] = []
    speech_segments: list[tuple[int, int]] = []
    silence_segments: list[tuple[int, int]] = []
    cursor_ms = 0

    for index, (kind, duration_ms) in enumerate(plan_ms):
        if duration_ms <= 0:
            raise ValueError("plan durations must be positive")
        start_ms = cursor_ms
        end_ms = cursor_ms + duration_ms
        count = duration_ms * SAMPLE_RATE // 1000
        if kind == "speech":
            pieces.append(_speech_chunk(duration_ms, seed, index))
            speech_segments.append((start_ms, end_ms))
        elif kind == "silence":
            pieces.append(np.zeros(count, dtype=np.float32))
            silence_segments.append((start_ms, end_ms))
        else:
            raise ValueError(f"unknown PCM block kind: {kind}")
        cursor_ms = end_ms

    samples = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
    samples = np.clip(samples * gain, -1.0, 1.0).astype(np.float32)
    pcm16 = np.round(samples * 32767.0).astype("<i2")
    return PcmSample(
        samples=samples,
        pcm_bytes=pcm16.tobytes(),
        sample_rate=SAMPLE_RATE,
        gain=gain,
        seed=seed,
        speech_segments_ms=tuple(speech_segments),
        silence_segments_ms=tuple(silence_segments),
    )


def read_pcm_bytes(data: bytes) -> np.ndarray:
    if len(data) % 2:
        raise ValueError("16-bit PCM byte length must be even")
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def frame_windows(sample_count: int, config: FrameConfig) -> list[Frame]:
    """Return the single canonical frame grid used by features and alignment."""

    frames: list[Frame] = []
    index = 0
    while True:
        start = index * config.hop_samples
        if start >= sample_count:
            break
        nominal_end = start + config.frame_samples
        source_end = min(nominal_end, sample_count)
        frames.append(
            Frame(
                index=index,
                start_sample=start,
                nominal_end_sample=nominal_end,
                source_end_sample=source_end,
                start_ms=start * 1000 // config.sample_rate,
                end_ms=nominal_end * 1000 // config.sample_rate,
                actual_end_ms=source_end * 1000 // config.sample_rate,
                padded=source_end < nominal_end,
                energy=0.0,
                rms=0.0,
                zero_crossing_rate=0.0,
            )
        )
        index += 1
    return frames


def _smooth_labels(labels: np.ndarray, config: FrameConfig, min_speech_ms: int, fill_gap_ms: int) -> np.ndarray:
    result = labels.copy()
    hop_ms = config.hop_ms
    min_speech_frames = max(1, math.ceil(min_speech_ms / hop_ms))
    max_gap_frames = max(0, math.floor(fill_gap_ms / hop_ms))

    runs: list[list[int]] = []
    start: int | None = None
    for idx, value in enumerate(result):
        if value and start is None:
            start = idx
        if start is not None and (not value or idx == len(result) - 1):
            end = idx if not value else idx + 1
            runs.append(list(range(start, end)))
            start = None

    for run in runs:
        if len(run) < min_speech_frames:
            result[run] = False

    idx = 0
    while idx < len(result):
        if result[idx]:
            idx += 1
            continue
        gap_start = idx
        while idx < len(result) and not result[idx]:
            idx += 1
        gap_end = idx
        if (
            gap_start > 0
            and gap_end < len(result)
            and gap_end - gap_start <= max_gap_frames
            and result[gap_start - 1]
            and result[gap_end]
        ):
            result[gap_start:gap_end] = True
    return result


def labels_to_segments(labels: Sequence[bool], config: FrameConfig) -> tuple[tuple[int, int], ...]:
    segments: list[tuple[int, int]] = []
    start: int | None = None
    for idx, active in enumerate(labels):
        if active and start is None:
            start = idx
        if start is not None and (not active or idx == len(labels) - 1):
            end_frame = idx if not active else idx + 1
            start_ms = start * config.hop_ms
            end_ms = end_frame * config.hop_ms + config.frame_ms - config.hop_ms
            if active and idx == len(labels) - 1:
                end_ms = (idx + 1) * config.hop_ms
            segments.append((start_ms, max(start_ms, end_ms)))
            start = None
    return tuple(segments)


def extract_features(
    pcm: bytes | np.ndarray | PcmSample,
    config: FrameConfig | None = None,
    *,
    energy_quantile: float = 0.32,
    min_speech_ms: int = 90,
    fill_gap_ms: int = 130,
) -> AudioFeatures:
    """Extract energy, ZCR, energy quantiles and deterministic VAD segments."""

    config = config or FrameConfig()
    if isinstance(pcm, PcmSample):
        samples = pcm.samples
        pcm_hash = _bytes_sha256(pcm.pcm_bytes)
    elif isinstance(pcm, bytes):
        samples = read_pcm_bytes(pcm)
        pcm_hash = _bytes_sha256(pcm)
    else:
        samples = np.asarray(pcm, dtype=np.float32)
        pcm16 = np.clip(samples, -1.0, 1.0)
        pcm_hash = _bytes_sha256((np.round(pcm16 * 32767).astype("<i2")).tobytes())

    base_frames = frame_windows(len(samples), config)
    energies: list[float] = []
    zcrs: list[float] = []
    frames: list[Frame] = []

    for window in base_frames:
        raw = samples[window.start_sample : window.source_end_sample]
        padded = np.zeros(config.frame_samples, dtype=np.float32)
        padded[: len(raw)] = raw
        energy = float(np.mean(padded * padded))
        energies.append(energy)
        if len(raw) >= 2:
            crossings = int(np.count_nonzero(np.signbit(raw[:-1]) != np.signbit(raw[1:])))
            zcr = crossings / max(1, len(raw) - 1)
        else:
            zcr = 0.0
        zcrs.append(float(zcr))
        frames.append(
            Frame(
                **{
                    **window.__dict__,
                    "energy": energy,
                    "rms": math.sqrt(energy),
                    "zero_crossing_rate": zcr,
                }
            )
        )

    energy_array = np.asarray(energies, dtype=np.float64)
    if len(energy_array) == 0:
        quantiles = {q: 0.0 for q in ("q10", "q25", "q50", "q75", "q90")}
        threshold = 0.0
        smoothed = np.zeros(0, dtype=bool)
    else:
        q_values = np.quantile(energy_array, [0.10, 0.25, 0.50, 0.75, 0.90])
        quantiles = {
            "q10": float(q_values[0]),
            "q25": float(q_values[1]),
            "q50": float(q_values[2]),
            "q75": float(q_values[3]),
            "q90": float(q_values[4]),
        }
        threshold = float(np.quantile(energy_array, energy_quantile) * 1.15 + 1e-7)
        raw_labels = energy_array > threshold
        smoothed = _smooth_labels(raw_labels, config, min_speech_ms, fill_gap_ms)

    labeled_frames = tuple(
        Frame(
            index=frame.index,
            start_sample=frame.start_sample,
            nominal_end_sample=frame.nominal_end_sample,
            source_end_sample=frame.source_end_sample,
            start_ms=frame.start_ms,
            end_ms=frame.end_ms,
            actual_end_ms=frame.actual_end_ms,
            padded=frame.padded,
            energy=frame.energy,
            rms=frame.rms,
            zero_crossing_rate=frame.zero_crossing_rate,
            is_speech=bool(smoothed[index]) if len(smoothed) else False,
        )
        for index, frame in enumerate(frames)
    )
    segments = labels_to_segments([frame.is_speech for frame in labeled_frames], config)
    duration_ms = len(samples) * 1000 // config.sample_rate
    return AudioFeatures(
        pcm_sha256=pcm_hash,
        sample_count=len(samples),
        duration_ms=duration_ms,
        config=config,
        frames=labeled_frames,
        energy_quantiles=quantiles,
        energy_threshold=threshold,
        speech_segments_ms=segments,
        vad_config={
            "energy_quantile": energy_quantile,
            "threshold_multiplier": 1.15,
            "min_speech_ms": min_speech_ms,
            "fill_gap_ms": fill_gap_ms,
        },
    )


def _bytes_sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class IntervalAlignment:
    utterance_id: str | None
    start_ms: int
    end_ms: int
    first_frame: int | None
    last_frame: int | None
    frame_indices: tuple[int, ...]
    speech_frame_ratio: float
    speech_overlap_ms: int
    nearest_vad_start_ms: int | None
    nearest_vad_end_ms: int | None
    start_boundary_delta_ms: int | None
    end_boundary_delta_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "utterance_id": self.utterance_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "first_frame": self.first_frame,
            "last_frame": self.last_frame,
            "frame_indices": list(self.frame_indices),
            "speech_frame_ratio": self.speech_frame_ratio,
            "speech_overlap_ms": self.speech_overlap_ms,
            "nearest_vad_start_ms": self.nearest_vad_start_ms,
            "nearest_vad_end_ms": self.nearest_vad_end_ms,
            "start_boundary_delta_ms": self.start_boundary_delta_ms,
            "end_boundary_delta_ms": self.end_boundary_delta_ms,
        }


def align_interval(
    features: AudioFeatures,
    start_ms: int,
    end_ms: int,
    utterance_id: str | None = None,
) -> IntervalAlignment:
    """Map a millisecond interval to the same canonical frame grid and VAD edges."""

    indices = tuple(
        frame.index
        for frame in features.frames
        if max(start_ms, frame.start_ms) < min(end_ms, frame.end_ms)
    )
    speech_frames = sum(1 for idx in indices if features.frames[idx].is_speech)
    speech_ms = 0
    for vad_start, vad_end in features.speech_segments_ms:
        speech_ms += max(0, min(end_ms, vad_end) - max(start_ms, vad_start))

    vad_starts = [item[0] for item in features.speech_segments_ms]
    vad_ends = [item[1] for item in features.speech_segments_ms]
    nearest_start = min(vad_starts, key=lambda value: abs(value - start_ms), default=None)
    nearest_end = min(vad_ends, key=lambda value: abs(value - end_ms), default=None)
    return IntervalAlignment(
        utterance_id=utterance_id,
        start_ms=start_ms,
        end_ms=end_ms,
        first_frame=indices[0] if indices else None,
        last_frame=indices[-1] if indices else None,
        frame_indices=indices,
        speech_frame_ratio=speech_frames / len(indices) if indices else 0.0,
        speech_overlap_ms=speech_ms,
        nearest_vad_start_ms=nearest_start,
        nearest_vad_end_ms=nearest_end,
        start_boundary_delta_ms=(start_ms - nearest_start) if nearest_start is not None else None,
        end_boundary_delta_ms=(end_ms - nearest_end) if nearest_end is not None else None,
    )


def changed_vad_intervals(
    old: AudioFeatures,
    new: AudioFeatures,
    padding_ms: int = 0,
) -> tuple[tuple[int, int], ...]:
    """Return frame-grid intervals whose VAD labels changed between configs."""

    count = max(len(old.frames), len(new.frames))
    changed: list[tuple[int, int]] = []
    run_start: int | None = None
    for idx in range(count):
        old_label = old.frames[idx].is_speech if idx < len(old.frames) else None
        new_label = new.frames[idx].is_speech if idx < len(new.frames) else None
        differs = old_label != new_label
        if differs and run_start is None:
            run_start = idx
        if run_start is not None and (not differs or idx == count - 1):
            run_end = idx if not differs else idx + 1
            start_ms = run_start * old.config.hop_ms - padding_ms
            end_ms = run_end * old.config.hop_ms + old.config.frame_ms + padding_ms
            changed.append((max(0, start_ms), end_ms))
            run_start = None
    return tuple(changed)
