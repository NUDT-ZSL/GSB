"""Deterministic local synthesis of dialog audio, truth, and speaker registry."""

from __future__ import annotations

import math
import shutil
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import (
    atomic_write_json,
    checksum_value,
    read_jsonl,
    write_jsonl_atomic,
)
from .errors import BusinessError, E_CONFIG_INVALID


FRAME_MS = 20
SAMPLE_RATE = 16000
FRAME_SAMPLES = 320


def _speaker_base(speaker_id: int) -> tuple[float, tuple[float, ...]]:
    f0 = 104.0 + 39.0 * speaker_id
    formants = (
        680.0 + 115.0 * speaker_id,
        1160.0 + 155.0 * speaker_id,
        2470.0 + 210.0 * speaker_id,
        3390.0 + 185.0 * speaker_id,
    )
    return f0, formants


def synthesize_voice(
    speaker_id: int,
    seed: int,
    num_samples: int,
    *,
    valence: float = 0.0,
    arousal: float = 0.4,
    fade: bool = True,
) -> np.ndarray:
    """Create a deterministic voiced mono signal with a speaker-specific timbre."""
    if num_samples <= 0:
        return np.zeros(0, dtype=np.float64)
    rng = np.random.default_rng(int(seed) & 0xFFFFFFFFFFFFFFFF)
    base_f0, formants = _speaker_base(speaker_id)
    variation = int(rng.integers(-6, 7)) * 0.55
    f0_base = base_f0 + variation
    t = np.arange(num_samples, dtype=np.float64) / SAMPLE_RATE
    phase_offset = float(rng.uniform(0.0, 2.0 * math.pi))
    vibrato = 0.013 * np.sin(2.0 * math.pi * (3.1 + 0.08 * speaker_id) * t + phase_offset)
    drift = 0.018 * np.sin(2.0 * math.pi * 0.42 * t + phase_offset * 0.3)
    f0 = f0_base * (1.0 + vibrato + drift)
    phase = 2.0 * math.pi * np.cumsum(f0) / SAMPLE_RATE

    brightness = float(np.clip(0.55 + 0.65 * valence, -0.2, 1.2))
    signal = np.zeros(num_samples, dtype=np.float64)
    harmonic_phases = rng.uniform(0.0, 2.0 * math.pi, size=18)
    for harmonic in range(1, 19):
        frequency = harmonic * f0_base
        formant_gain = 0.0
        for formant_index, formant in enumerate(formants):
            width = 190.0 + 45.0 * formant_index
            formant_gain += (1.5 - 0.2 * formant_index) * math.exp(
                -0.5 * ((frequency - formant) / width) ** 2
            )
        tilt = (1.0 / harmonic**1.05) * (1.0 + brightness * 0.55 / harmonic)
        amplitude = tilt * (0.16 + formant_gain)
        signal += amplitude * np.sin(harmonic * phase + harmonic_phases[harmonic - 1])

    breath = rng.normal(0.0, 0.0035, size=num_samples)
    amplitude_scale = 0.19 * (0.72 + 0.45 * max(0.0, arousal))
    signal = signal * amplitude_scale + breath
    if fade:
        fade_samples = min(num_samples // 4, int(0.018 * SAMPLE_RATE))
        if fade_samples > 0:
            ramp = np.sin(np.linspace(0.0, math.pi / 2.0, fade_samples)) ** 2
            signal[:fade_samples] *= ramp
            signal[-fade_samples:] *= ramp[::-1]
    return signal


def registry_clip(
    speaker_id: int,
    clip_seed: int,
    clip_ms: int,
) -> np.ndarray:
    num_samples = int(round(clip_ms * SAMPLE_RATE / 1000.0))
    return synthesize_voice(speaker_id, clip_seed, num_samples, valence=0.0, arousal=0.42)


def _write_wav(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(samples, -0.98, 0.98)
    pcm = np.round(clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.setnframes(len(pcm))
        handle.writeframes(pcm.tobytes())


def _emotion_for_turn(rng: np.random.Generator, turn_index: int) -> tuple[float, float]:
    valence = float(np.clip(rng.normal(0.0, 0.42), -0.85, 0.85))
    arousal = float(np.clip(rng.normal(0.52, 0.18), 0.08, 0.93))
    if turn_index % 5 == 0:
        valence = float(np.clip(valence + 0.2, -0.9, 0.9))
    return valence, arousal


def synthesize_dialog(
    dialog_id: str,
    seed: int,
    duration_ms: int,
    speaker_count: int,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    if duration_ms % FRAME_MS != 0:
        raise BusinessError(
            E_CONFIG_INVALID,
            "duration_ms must align to the 20 ms frame grid",
            {"field": "duration_ms", "actual": duration_ms, "frame_ms": FRAME_MS},
        )
    num_frames = duration_ms // FRAME_MS
    num_samples = int(duration_ms * SAMPLE_RATE / 1000)
    rng = np.random.default_rng(int(seed) & 0xFFFFFFFFFFFFFFFF)
    truth_speaker = np.zeros(num_frames, dtype=np.int64)
    truth_valence = np.zeros(num_frames, dtype=np.float64)
    truth_arousal = np.zeros(num_frames, dtype=np.float64)
    truth_speech = np.zeros(num_frames, dtype=bool)
    audio = np.zeros(num_samples, dtype=np.float64)

    cursor = int(rng.integers(0, 3)) if num_frames > 4 else 0
    last_speaker = 0
    turn = 0
    while cursor < num_frames:
        speaker_id = turn % speaker_count
        requested = int(rng.integers(12, 34))
        active_frames = min(requested, num_frames - cursor)
        if active_frames <= 0:
            break
        start_frame = cursor
        end_frame = cursor + active_frames
        start_sample = start_frame * FRAME_SAMPLES
        end_sample = min(end_frame * FRAME_SAMPLES, num_samples)
        valence, arousal = _emotion_for_turn(rng, turn)
        turn_seed = int(seed) * 1009 + turn * 7919 + speaker_id * 104729 + 17
        voice = synthesize_voice(
            speaker_id,
            turn_seed,
            end_sample - start_sample,
            valence=valence,
            arousal=arousal,
        )
        audio[start_sample:end_sample] += voice
        truth_speaker[start_frame:end_frame] = speaker_id
        truth_speech[start_frame:end_frame] = True
        envelope = np.sin(np.linspace(0.0, math.pi, active_frames, endpoint=False))
        truth_valence[start_frame:end_frame] = np.clip(
            valence + 0.025 * envelope, -0.95, 0.95
        )
        truth_arousal[start_frame:end_frame] = np.clip(
            arousal + 0.018 * envelope, 0.05, 0.95
        )
        last_speaker = speaker_id
        cursor = end_frame + int(rng.integers(3, 11))
        turn += 1

    speech_idx = np.flatnonzero(truth_speech)
    first_speaker = int(truth_speaker[speech_idx[0]]) if speech_idx.size else 0
    for frame in range(num_frames):
        if truth_speech[frame]:
            last_speaker = int(truth_speaker[frame])
        else:
            truth_speaker[frame] = last_speaker if frame else first_speaker
    truth_valence[~truth_speech] = 0.0
    truth_arousal[~truth_speech] = 0.0

    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0.96:
        audio *= 0.96 / peak
    rows = [
        {
            "frame_index": frame,
            "speaker_id": int(truth_speaker[frame]),
            "valence": round(float(truth_valence[frame]), 10),
            "arousal": round(float(truth_arousal[frame]), 10),
            "is_speech": bool(truth_speech[frame]),
        }
        for frame in range(num_frames)
    ]
    return audio, rows


def generate_dataset(
    root: Path,
    *,
    dialog_count: int = 5,
    speaker_count: int = 3,
    seed: int = 20261120,
    force: bool = False,
) -> dict[str, Any]:
    if dialog_count <= 0 or speaker_count <= 0:
        raise BusinessError(
            E_CONFIG_INVALID,
            "dialog_count and speaker_count must be positive",
            {"field": "dialog_count", "actual": dialog_count},
        )
    if speaker_count > 8:
        raise BusinessError(
            E_CONFIG_INVALID,
            "At most 8 synthetic speakers are supported",
            {"field": "speaker_count", "actual": speaker_count},
        )
    if root.exists() and force:
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    audio_dir = root / "audio"
    truth_dir = root / "truth"
    audio_dir.mkdir(exist_ok=True)
    truth_dir.mkdir(exist_ok=True)

    manifest_rows: list[dict[str, Any]] = []
    for index in range(dialog_count):
        dialog_id = f"dlg_{index + 1:04d}"
        dialog_seed = int(seed) + 10007 * index
        duration_ms = 6000 + 400 * ((index * 3 + 1) % 8)
        num_samples = int(duration_ms * SAMPLE_RATE / 1000)
        audio, truth_rows = synthesize_dialog(dialog_id, dialog_seed, duration_ms, speaker_count)
        _write_wav(audio_dir / f"{dialog_id}.wav", audio)
        write_jsonl_atomic(truth_dir / f"{dialog_id}.jsonl", truth_rows)
        manifest_rows.append(
            {
                "dialog_id": dialog_id,
                "sample_rate": SAMPLE_RATE,
                "duration_ms": duration_ms,
                "seed": dialog_seed,
                "speaker_count": speaker_count,
                "frame_ms": FRAME_MS,
            }
        )
    write_jsonl_atomic(root / "manifest.jsonl", manifest_rows)

    registry_rows: list[dict[str, Any]] = []
    for speaker_id in range(speaker_count):
        for sample_index in range(2):
            row = {
                "speaker_id": speaker_id,
                "clip_seed": int(seed) + speaker_id * 97 + sample_index * 131,
                "clip_ms": 760 + sample_index * 80,
                "registry_version": 1,
            }
            row["sample_checksum"] = registry_sample_checksum(row)
            registry_rows.append(row)
    write_jsonl_atomic(root / "registrations.jsonl", registry_rows)
    atomic_write_json(
        root / "generation.json",
        {
            "generator": "voice_state.synth.v1",
            "dialog_count": dialog_count,
            "speaker_count": speaker_count,
            "seed": seed,
            "frame_ms": FRAME_MS,
            "sample_rate": SAMPLE_RATE,
        },
    )
    return {
        "dataset_root": str(root),
        "dialog_count": dialog_count,
        "speaker_count": speaker_count,
        "manifest_checksum": checksum_value(manifest_rows),
        "registry_checksum": checksum_value(sorted(registry_rows, key=_registry_sort_key)),
    }


def append_registry_sample(
    registry_path: Path,
    *,
    speaker_id: int,
    clip_seed: int,
    clip_ms: int,
    registry_version: int | None = None,
) -> dict[str, Any]:
    rows = read_jsonl(registry_path)
    if not rows:
        raise BusinessError(E_CONFIG_INVALID, "Registry is empty", {"field": "registrations"})
    rows = [_normalize_registry_row(row) for row in rows]
    current_version = max(int(row["registry_version"]) for row in rows)
    new_version = current_version + 1 if registry_version is None else int(registry_version)
    if new_version <= current_version:
        raise BusinessError(
            E_CONFIG_INVALID,
            "registry_version must increase when appending",
            {"field": "registry_version", "actual": new_version, "current": current_version},
        )
    if clip_ms <= 0:
        raise BusinessError(
            E_CONFIG_INVALID,
            "clip_ms must be positive",
            {"field": "clip_ms", "actual": clip_ms},
        )
    row = {
        "speaker_id": int(speaker_id),
        "clip_seed": int(clip_seed),
        "clip_ms": int(clip_ms),
        "registry_version": new_version,
    }
    row["sample_checksum"] = registry_sample_checksum(row)
    if any(
        item["speaker_id"] == row["speaker_id"]
        and item["clip_seed"] == row["clip_seed"]
        and item["clip_ms"] == row["clip_ms"]
        for item in rows
    ):
        raise BusinessError(
            E_CONFIG_INVALID,
            "Duplicate registry sample",
            {"field": "clip_seed", "actual": clip_seed},
        )
    rows.append(row)
    write_jsonl_atomic(registry_path, rows)
    return {
        "appended": row,
        "registry_version": new_version,
        "checksum": checksum_value(sorted(rows, key=_registry_sort_key)),
    }


def _registry_sort_key(row: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(row["registry_version"]),
        int(row["speaker_id"]),
        int(row["clip_seed"]),
        int(row["clip_ms"]),
    )


def _registry_checksum_source(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "speaker_id": int(row["speaker_id"]),
        "clip_seed": int(row["clip_seed"]),
        "clip_ms": int(row["clip_ms"]),
        "registry_version": int(row["registry_version"]),
    }


def registry_sample_checksum(row: dict[str, Any]) -> str:
    return checksum_value(_registry_checksum_source(row))


def _normalize_registry_row(row: dict[str, Any]) -> dict[str, Any]:
    expected = registry_sample_checksum(row)
    stored = row.get("sample_checksum")
    if stored is not None and stored != expected:
        raise BusinessError(
            E_CONFIG_INVALID,
            "Registry sample checksum mismatch",
            {
                "field": "sample_checksum",
                "speaker_id": row.get("speaker_id"),
                "expected": expected,
                "actual": stored,
            },
        )
    return {**row, "sample_checksum": expected}

