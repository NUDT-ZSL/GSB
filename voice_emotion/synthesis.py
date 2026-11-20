"""Deterministic multi-speaker speech-like PCM synthesis.

This is intentionally a compact domain fixture, not a vocoder.  The same seed,
speaker id and utterance descriptor always produces the same PCM; speaker
profiles are stable between enrollment and dialogue so speaker assignment is
testable without external data.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .audio import float_to_int16
from .canonical import canonical_json, hash_value


DEFAULT_SAMPLE_RATE = 16_000


def deterministic_seed(*parts: Any) -> int:
    digest = hashlib.sha256(canonical_json(list(parts)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "little", signed=False)


def rng_for(*parts: Any) -> np.random.Generator:
    return np.random.default_rng(deterministic_seed(*parts))


@dataclass(frozen=True)
class SpeakerProfile:
    speaker_id: int
    base_f0: float
    formants: tuple[float, float, float]
    formant_gains: tuple[float, float, float]
    speech_rate: float
    brightness: float
    energy: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker_id": self.speaker_id,
            "base_f0": self.base_f0,
            "formants": list(self.formants),
            "formant_gains": list(self.formant_gains),
            "speech_rate": self.speech_rate,
            "brightness": self.brightness,
            "energy": self.energy,
        }


def speaker_profile(speaker_id: int, seed: int) -> SpeakerProfile:
    rng = rng_for("speaker-profile", int(seed), int(speaker_id))
    f1 = 380.0 + 420.0 * float(rng.random())
    f2 = 950.0 + 1_150.0 * float(rng.random())
    f3 = 2_350.0 + 1_250.0 * float(rng.random())
    gains = tuple(0.45 + 0.8 * float(rng.random()) for _ in range(3))
    return SpeakerProfile(
        speaker_id=int(speaker_id),
        base_f0=92.0 + 125.0 * float(rng.random()),
        formants=(f1, f2, f3),
        formant_gains=gains,
        speech_rate=2.4 + 2.8 * float(rng.random()),
        brightness=0.2 + 0.8 * float(rng.random()),
        energy=0.65 + 0.35 * float(rng.random()),
    )


def _voice_signal(
    profile: SpeakerProfile,
    sample_count: int,
    sample_rate: int,
    rng: np.random.Generator,
    pitch_scale: float = 1.0,
    phase_offset: float = 0.0,
) -> np.ndarray:
    if sample_count <= 0:
        return np.zeros(0, dtype=np.float64)
    t = np.arange(sample_count, dtype=np.float64) / float(sample_rate)
    vibrato = 0.025 * np.sin(2.0 * np.pi * (4.2 + 0.7 * rng.random()) * t + phase_offset)
    drift = 0.015 * np.sin(2.0 * np.pi * (0.55 + 0.25 * rng.random()) * t + 0.5 * phase_offset)
    f0 = profile.base_f0 * pitch_scale * (1.0 + vibrato + drift)
    phase = 2.0 * np.pi * np.cumsum(f0) / float(sample_rate)
    signal = np.zeros(sample_count, dtype=np.float64)
    harmonic_limit = min(18, max(4, int((sample_rate / 2.0) / max(profile.base_f0, 1.0))))
    for harmonic in range(1, harmonic_limit + 1):
        frequency = harmonic * profile.base_f0 * pitch_scale
        resonance = 0.0
        for formant, gain in zip(profile.formants, profile.formant_gains):
            bandwidth = 190.0 + 0.09 * formant
            resonance += gain * np.exp(-0.5 * ((frequency - formant) / bandwidth) ** 2)
        amplitude = (1.0 / (harmonic**1.15)) * (0.24 + resonance + 0.18 * profile.brightness)
        signal += amplitude * np.sin(harmonic * phase + phase_offset * harmonic * 0.11)
    syllable_gate = 0.42 + 0.58 * (0.5 + 0.5 * np.sin(2.0 * np.pi * profile.speech_rate * t + phase_offset))
    slow_envelope = 0.78 + 0.22 * np.sin(2.0 * np.pi * 0.37 * t + 0.5 * phase_offset)
    noise = rng.standard_normal(sample_count) * 0.008
    signal = (signal + noise) * syllable_gate * slow_envelope * profile.energy
    fade_samples = min(sample_count // 2, int(0.012 * sample_rate))
    if fade_samples > 0:
        fade = np.linspace(0.0, 1.0, fade_samples, endpoint=False)
        signal[:fade_samples] *= fade
        signal[-fade_samples:] *= fade[::-1]
    return signal


def synthesize_registration(
    speaker_id: int,
    seed: int,
    duration_ms: int,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    sample_index: int = 0,
) -> np.ndarray:
    rng = rng_for("registration", int(seed), int(speaker_id), int(duration_ms), int(sample_index))
    profile = speaker_profile(speaker_id, seed)
    sample_count = int(round(duration_ms * sample_rate / 1000.0))
    signal = _voice_signal(
        profile,
        sample_count,
        sample_rate,
        rng,
        pitch_scale=0.98 + 0.04 * float(rng.random()),
        phase_offset=2.0 * np.pi * float(rng.random()),
    )
    peak = float(np.max(np.abs(signal))) if signal.size else 0.0
    if peak > 0:
        signal = signal * (0.72 / peak)
    return signal.astype(np.float64)


def make_timeline(
    dialog_id: str,
    seed: int,
    duration_ms: int,
    speaker_ids: Iterable[int],
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> dict[str, Any]:
    speakers = sorted(int(item) for item in speaker_ids)
    if not speakers:
        raise ValueError("speaker_ids cannot be empty")
    total_samples = int(round(duration_ms * sample_rate / 1000.0))
    rng = rng_for("timeline", str(dialog_id), int(seed), int(duration_ms), speakers)
    turns: list[dict[str, Any]] = []
    cursor = 0
    previous_speaker: int | None = None
    turn_index = 0
    while cursor < total_samples:
        remaining = total_samples - cursor
        target_ms = 230 + int(rng.integers(0, 420))
        duration_samples = min(remaining, max(1, int(round(target_ms * sample_rate / 1000.0))))
        if remaining - duration_samples < int(0.11 * sample_rate):
            duration_samples = remaining
        candidates = [speaker for speaker in speakers if speaker != previous_speaker] or speakers
        speaker_id = int(candidates[int(rng.integers(0, len(candidates)))])
        turns.append(
            {
                "turn_index": turn_index,
                "speaker_id": speaker_id,
                "start_sample": cursor,
                "end_sample": cursor + duration_samples,
                "gain": round(0.62 + 0.32 * float(rng.random()), 6),
                "pitch_scale": round(0.96 + 0.08 * float(rng.random()), 6),
                "phase_offset": round(2.0 * np.pi * float(rng.random()), 12),
                "rng_seed": deterministic_seed("turn", dialog_id, seed, turn_index),
            }
        )
        cursor += duration_samples
        previous_speaker = speaker_id
        turn_index += 1
    return {
        "dialog_id": str(dialog_id),
        "seed": int(seed),
        "sample_rate": int(sample_rate),
        "duration_ms": int(duration_ms),
        "total_samples": total_samples,
        "turns": turns,
    }


def synthesize_dialog(
    dialog_id: str,
    seed: int,
    duration_ms: int,
    speaker_ids: Iterable[int],
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> tuple[np.ndarray, dict[str, Any]]:
    timeline = make_timeline(dialog_id, seed, duration_ms, speaker_ids, sample_rate)
    output = np.zeros(timeline["total_samples"], dtype=np.float64)
    profiles = {
        int(speaker_id): speaker_profile(int(speaker_id), seed)
        for speaker_id in timeline_speakers(timeline)
    }
    for turn in timeline["turns"]:
        start = int(turn["start_sample"])
        end = int(turn["end_sample"])
        rng = np.random.default_rng(int(turn["rng_seed"]))
        signal = _voice_signal(
            profiles[int(turn["speaker_id"])],
            end - start,
            sample_rate,
            rng,
            pitch_scale=float(turn["pitch_scale"]),
            phase_offset=float(turn["phase_offset"]),
        )
        output[start:end] += float(turn["gain"]) * signal
    peak = float(np.max(np.abs(output))) if output.size else 0.0
    if peak > 0:
        output *= 0.88 / peak
    return float_to_int16(output), timeline


def timeline_speakers(timeline: dict[str, Any]) -> list[int]:
    return sorted({int(turn["speaker_id"]) for turn in timeline["turns"]})


def frame_starts(
    sample_count: int,
    frame_samples: int,
    hop_samples: int,
) -> np.ndarray:
    if frame_samples <= 0 or hop_samples <= 0:
        return np.zeros(0, dtype=np.int64)
    if sample_count < frame_samples:
        return np.zeros(0, dtype=np.int64)
    last_start = sample_count - frame_samples
    return np.arange(0, last_start + 1, hop_samples, dtype=np.int64)


def synthesize_truth(
    timeline: dict[str, Any],
    seed: int,
    frame_ms: float = 20.0,
    frame_shift_ms: float = 20.0,
) -> list[dict[str, Any]]:
    sample_rate = int(timeline["sample_rate"])
    frame_samples = int(round(frame_ms * sample_rate / 1000.0))
    hop_samples = int(round(frame_shift_ms * sample_rate / 1000.0))
    starts = frame_starts(int(timeline["total_samples"]), frame_samples, hop_samples)
    turns = timeline["turns"]
    speaker_base: dict[int, tuple[float, float, float, float]] = {}
    for speaker_id in timeline_speakers(timeline):
        profile = speaker_profile(speaker_id, seed)
        rng = rng_for("emotion-base", seed, speaker_id)
        speaker_base[speaker_id] = (
            float(rng.uniform(-0.55, 0.55)),
            float(rng.uniform(-0.45, 0.55)),
            float(rng.uniform(0.0, 2.0 * np.pi)),
            float(rng.uniform(0.0, 2.0 * np.pi)),
        )
    rows: list[dict[str, Any]] = []
    turn_index = 0
    for frame_index, start in enumerate(starts.tolist()):
        center = int(start + frame_samples // 2)
        while turn_index + 1 < len(turns) and center >= int(turns[turn_index]["end_sample"]):
            turn_index += 1
        turn = turns[turn_index]
        speaker_id = int(turn["speaker_id"])
        profile = speaker_profile(speaker_id, seed)
        t = center / float(sample_rate)
        base_v, base_a, phase_v, phase_a = speaker_base[speaker_id]
        formant_span = (profile.formants[2] - profile.formants[0]) / 3_000.0
        valence = base_v + 0.22 * np.sin(2.0 * np.pi * 0.31 * t + phase_v)
        arousal = (
            base_a
            + 0.20 * np.sin(2.0 * np.pi * 0.47 * t + phase_a)
            + 0.15 * formant_span
            + 0.08 * profile.speech_rate / 5.2
        )
        rows.append(
            {
                "frame_index": int(frame_index),
                "speaker_id": speaker_id,
                "valence": round(float(np.clip(valence, -1.0, 1.0)), 6),
                "arousal": round(float(np.clip(arousal, -1.0, 1.0)), 6),
                "is_speech": True,
            }
        )
    return rows


def profile_digest(speaker_id: int, seed: int) -> str:
    return hash_value(speaker_profile(speaker_id, seed).to_dict())
