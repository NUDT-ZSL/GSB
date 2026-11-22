"""Deterministic synthetic meeting audio and frame-level ground truth."""

from __future__ import annotations

import hashlib
import io
import wave
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import MeetingConfig, canonical_json_bytes


@dataclass(frozen=True)
class SpeakerSpec:
    speaker_id: int
    base_f0: float
    formants: tuple[float, float, float]
    formant_bandwidths: tuple[float, float, float]
    phase: float


@dataclass
class SynthesisResult:
    sample_rate: int
    frame_ms: float
    hop_ms: float
    frame_count: int
    pcm: np.ndarray
    pcm_bytes: bytes
    truth: list[dict[str, Any]]
    truth_segments: list[dict[str, Any]]
    registrations: dict[int, np.ndarray]
    speaker_specs: tuple[SpeakerSpec, ...]
    schedule: dict[int, tuple[int, int]]
    pcm_sha256: str
    truth_bytes: bytes

    @property
    def total_samples(self) -> int:
        return int(self.pcm.shape[0])

    @property
    def duration_ms(self) -> float:
        return self.total_samples * 1000.0 / self.sample_rate

    def wav_bytes(self) -> bytes:
        return pcm_to_wav_bytes(self.pcm, self.sample_rate)

    def registration_wav_bytes(self, speaker_id: int) -> bytes:
        return pcm_to_wav_bytes(self.registrations[speaker_id], self.sample_rate)


_F0_TEMPLATES = (112.0, 176.0, 238.0, 143.0, 205.0, 267.0)
_FORMANT_TEMPLATES = (
    (680.0, 1220.0, 2580.0),
    (410.0, 1740.0, 2790.0),
    (860.0, 1510.0, 3040.0),
    (520.0, 1980.0, 2410.0),
    (760.0, 1080.0, 2860.0),
    (620.0, 1640.0, 3210.0),
)
_BANDWIDTH_TEMPLATES = (
    (110.0, 150.0, 220.0),
    (90.0, 170.0, 240.0),
    (130.0, 160.0, 250.0),
    (100.0, 180.0, 210.0),
    (120.0, 140.0, 230.0),
    (95.0, 165.0, 245.0),
)


def _speaker_specs(max_speakers: int, seed: int) -> tuple[SpeakerSpec, ...]:
    rng = np.random.default_rng(seed + 991)
    specs: list[SpeakerSpec] = []
    for index in range(max_speakers):
        template = index % len(_F0_TEMPLATES)
        jitter = float(rng.uniform(-7.0, 7.0))
        specs.append(
            SpeakerSpec(
                speaker_id=index + 1,
                base_f0=_F0_TEMPLATES[template] + jitter,
                formants=_FORMANT_TEMPLATES[template],
                formant_bandwidths=_BANDWIDTH_TEMPLATES[template],
                phase=float(rng.uniform(0.0, 2.0 * np.pi)),
            )
        )
    return tuple(specs)


def _schedule(frame_count: int, max_speakers: int) -> dict[int, tuple[int, int]]:
    """Turn-taking schedule with deterministic inter-speaker gaps."""
    if max_speakers == 1:
        return {1: (frame_count // 10, frame_count * 9 // 10)}
    usable_start = frame_count * 10 // 100
    usable_end = frame_count * 87 // 100
    usable = usable_end - usable_start
    gap = max(2, frame_count * 25 // 1000)
    slot = (usable - gap * (max_speakers - 1)) // max_speakers
    schedule: dict[int, tuple[int, int]] = {}
    for index in range(max_speakers):
        start = usable_start + index * (slot + gap)
        end = start + slot
        schedule[index + 1] = (start, end)
    return schedule


def _smooth_noise(rng: np.random.Generator, length: int, width: int) -> np.ndarray:
    raw = rng.normal(0.0, 1.0, length)
    kernel = np.hanning(max(3, width))
    kernel /= kernel.sum()
    smoothed = np.convolve(raw, kernel, mode="same")
    scale = float(np.max(np.abs(smoothed))) or 1.0
    return smoothed / scale


def _synthesize_voice(
    spec: SpeakerSpec,
    sample_rate: int,
    length: int,
    seed: int,
    *,
    registration_variant: bool = False,
) -> np.ndarray:
    rng = np.random.default_rng(seed + spec.speaker_id * 1009 + (71 if registration_variant else 0))
    t = np.arange(length, dtype=np.float64) / sample_rate
    f0_curve = spec.base_f0 * (
        1.0
        + 0.025 * np.sin(2.0 * np.pi * 0.73 * t + spec.phase)
        + 0.009 * _smooth_noise(rng, length, max(3, sample_rate // 120))
    )
    phase = 2.0 * np.pi * np.cumsum(f0_curve) / sample_rate + spec.phase
    signal = np.zeros(length, dtype=np.float64)
    max_harmonic = min(18, int((sample_rate * 0.45) // max(70.0, spec.base_f0)))
    for harmonic in range(1, max_harmonic + 1):
        harmonic_hz = harmonic * f0_curve
        formant_response = np.zeros(length, dtype=np.float64)
        for formant, bandwidth in zip(spec.formants, spec.formant_bandwidths):
            formant_response += np.exp(
                -0.5 * ((harmonic_hz - formant) / bandwidth) ** 2
            ) * (0.55 + 650.0 / formant)
        amplitude = (0.15 + formant_response) / (harmonic**1.12)
        signal += amplitude * np.sin(harmonic * phase + 0.17 * harmonic)
    breath = _smooth_noise(rng, length, max(3, sample_rate // 300))
    signal = signal / (float(np.max(np.abs(signal))) or 1.0)
    signal = 0.88 * signal + 0.04 * breath
    fade = max(8, int(0.006 * sample_rate))
    envelope = np.ones(length, dtype=np.float64)
    envelope[:fade] = np.linspace(0.0, 1.0, fade, endpoint=False)
    envelope[-fade:] = np.linspace(1.0, 0.0, fade, endpoint=False)
    return signal * envelope


def synthesize_meeting(config: MeetingConfig, seed: int = 20261123) -> SynthesisResult:
    if config.frame_ms != 20.0:
        raise ValueError("truth-grid compatible configurations require frame_ms=20")
    sample_rate = config.sample_rate
    total_samples = config.total_samples
    frame_count = config.frame_count
    specs = _speaker_specs(config.max_speakers, seed)
    schedule = _schedule(frame_count, config.max_speakers)
    mix = np.zeros(total_samples, dtype=np.float64)
    truth: list[dict[str, Any]] = []
    truth_segments: list[dict[str, Any]] = []
    registrations: dict[int, np.ndarray] = {}

    background_rng = np.random.default_rng(seed + 4049)
    mix += 1.8e-4 * _smooth_noise(background_rng, total_samples, max(3, sample_rate // 80))

    for spec in specs:
        start_frame, end_frame = schedule[spec.speaker_id]
        start_sample = start_frame * config.hop_samples
        end_sample = end_frame * config.hop_samples
        length = end_sample - start_sample
        voice = _synthesize_voice(spec, sample_rate, length, seed)
        gain = config.gain_for(spec.speaker_id)
        mix[start_sample:end_sample] += gain * 0.21 * voice

        reg_length = int(1.15 * sample_rate)
        registrations[spec.speaker_id] = np.clip(
            np.rint(
                0.22
                * _synthesize_voice(
                    spec,
                    sample_rate,
                    reg_length,
                    seed + 17011,
                    registration_variant=True,
                )
                * 32767.0
            ),
            -32768,
            32767,
        ).astype("<i2")

        truth_segments.append(
            {
                "speaker_id": spec.speaker_id,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "start_sample": start_sample,
                "end_sample": end_sample,
                "start_ms": start_sample * 1000.0 / sample_rate,
                "end_ms": end_sample * 1000.0 / sample_rate,
            }
        )

    for frame_index in range(frame_count):
        sample_start = frame_index * config.hop_samples
        sample_end = sample_start + config.frame_samples
        for spec in specs:
            active_start, active_end = schedule[spec.speaker_id]
            active_sample_start = active_start * config.hop_samples
            active_sample_end = active_end * config.hop_samples
            truth.append(
                {
                    "frame_index": frame_index,
                    "speaker_id": spec.speaker_id,
                    "is_speech": (
                        sample_start < active_sample_end
                        and sample_end > active_sample_start
                    ),
                    "sample_start": sample_start,
                    "sample_end": sample_end,
                }
            )

    peak = float(np.max(np.abs(mix))) or 1.0
    if peak > 0.94:
        mix *= 0.94 / peak
    pcm = np.clip(np.rint(mix * 32767.0), -32768, 32767).astype("<i2")
    pcm_bytes = pcm.tobytes(order="C")
    truth_bytes = canonical_json_bytes(truth)
    return SynthesisResult(
        sample_rate=sample_rate,
        frame_ms=config.frame_ms,
        hop_ms=config.hop_ms,
        frame_count=frame_count,
        pcm=pcm,
        pcm_bytes=pcm_bytes,
        truth=truth,
        truth_segments=sorted(truth_segments, key=lambda item: item["speaker_id"]),
        registrations=registrations,
        speaker_specs=specs,
        schedule=schedule,
        pcm_sha256=hashlib.sha256(pcm_bytes).hexdigest(),
        truth_bytes=truth_bytes,
    )


def pcm_to_wav_bytes(pcm: np.ndarray, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.astype("<i2", copy=False).tobytes(order="C"))
    return buffer.getvalue()
