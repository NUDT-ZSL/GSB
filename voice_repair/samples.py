from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from .audio import wav_bytes


def _rng_for(seed: int, recording_id: str) -> np.random.Generator:
    material = f"{seed}:{recording_id}".encode("utf-8")
    derived = int.from_bytes(hashlib.sha256(material).digest()[:8], "little")
    return np.random.default_rng(derived)


def _synthetic_voice(
    sample_rate: int,
    duration: float,
    rng: np.random.Generator,
    base_freq: float,
) -> np.ndarray:
    count = int(round(sample_rate * duration))
    t = np.arange(count, dtype=np.float64) / sample_rate
    vibrato = 1.0 + 0.007 * np.sin(2.0 * np.pi * 5.1 * t)
    f0 = base_freq * vibrato
    phase = 2.0 * np.pi * np.cumsum(f0) / sample_rate
    wave = (
        0.55 * np.sin(phase)
        + 0.22 * np.sin(2.0 * phase + 0.3)
        + 0.12 * np.sin(3.0 * phase + 1.1)
        + 0.06 * np.sin(5.0 * phase + 0.7)
    )
    syllable_rate = 3.2 + 0.4 * rng.random()
    envelope = 0.35 + 0.65 * np.abs(np.sin(np.pi * syllable_rate * t))
    phrase = 0.62 + 0.38 * np.sin(2.0 * np.pi * 0.23 * t + 0.5) ** 2
    return wave * envelope * phrase


def _inject_impulses(
    signal: np.ndarray,
    rng: np.random.Generator,
    *,
    count: int,
) -> np.ndarray:
    result = signal.copy()
    if count == 0:
        return result
    positions = rng.choice(
        np.arange(64, max(65, result.size - 64)),
        size=count,
        replace=False,
    )
    for position in positions:
        width = int(rng.integers(3, 9))
        sign = -1.0 if rng.random() < 0.5 else 1.0
        amplitude = float(rng.uniform(0.35, 0.82))
        shape = np.exp(-np.arange(width, dtype=np.float64) / 1.7)
        end = min(result.size, int(position) + width)
        result[int(position) : end] += sign * amplitude * shape[: end - int(position)]
    return result


def _make_recording(
    recording_id: str,
    sample_rate: int,
    duration: float,
    seed: int,
    *,
    dc_offset: float,
    gain: float,
    noise_rms: float,
    impulse_count: int,
    base_freq: float,
) -> tuple[np.ndarray, dict]:
    rng = _rng_for(seed, recording_id)
    clean = _synthetic_voice(sample_rate, duration, rng, base_freq)
    noise = rng.normal(0.0, noise_rms, size=clean.size)
    damaged = gain * (clean + noise) + dc_offset
    damaged = _inject_impulses(damaged, rng, count=impulse_count)
    damaged = np.clip(damaged, -0.98, 0.98)
    generation = {
        "sample_rate": sample_rate,
        "duration_seconds": duration,
        "dc_offset": dc_offset,
        "gain": gain,
        "noise_rms": noise_rms,
        "impulse_count": impulse_count,
        "base_freq_hz": base_freq,
    }
    return damaged, generation


def generate_demo_dataset(data_dir: Path, seed: int = 20261125, force: bool = False) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    task_path = data_dir / "task.json"
    if task_path.exists() and not force:
        raise FileExistsError(f"{task_path} already exists; use --force to regenerate")

    definitions = [
        {
            "id": "voice_16k_dc_pop",
            "sample_rate": 16000,
            "duration": 1.45,
            "dc_offset": 0.115,
            "gain": 0.58,
            "noise_rms": 0.022,
            "impulse_count": 9,
            "base_freq": 185.0,
        },
        {
            "id": "voice_44k_noise",
            "sample_rate": 44100,
            "duration": 1.15,
            "dc_offset": -0.083,
            "gain": 0.36,
            "noise_rms": 0.031,
            "impulse_count": 7,
            "base_freq": 143.0,
        },
        {
            "id": "voice_48k_gain",
            "sample_rate": 48000,
            "duration": 1.05,
            "dc_offset": 0.061,
            "gain": 0.27,
            "noise_rms": 0.018,
            "impulse_count": 11,
            "base_freq": 222.0,
        },
    ]

    recordings = []
    for definition in definitions:
        recording_id = definition.pop("id")
        sample_rate = definition.pop("sample_rate")
        duration = definition.pop("duration")
        values, generation = _make_recording(
            recording_id,
            sample_rate,
            duration,
            seed,
            **definition,
        )
        wav_name = f"{recording_id}.wav"
        (data_dir / wav_name).write_bytes(wav_bytes(values, sample_rate))
        recordings.append(
            {
                "id": recording_id,
                "wav": wav_name,
                "generation": generation,
                "stages": {
                    "dc": {
                        "block_frames": 4096,
                        "mode": "mean",
                    },
                    "declick": {
                        "block_frames": 4096,
                        "threshold": 0.19,
                        "radius": 2,
                    },
                    "denoise": {
                        "block_frames": 4096,
                        "frame_size": 512,
                        "hop_size": 128,
                        "noise_frames": 10,
                        "oversubtraction": 1.35,
                        "floor_ratio": 0.035,
                    },
                    "normalize": {
                        "block_frames": 4096,
                        "target_peak": 0.92,
                    },
                },
            }
        )

    task = {
        "schema_version": 1,
        "task_id": "game_voice_demo_batch",
        "seed": seed,
        "parallelism": 3,
        "round_time_budget_ns": 150_000,
        "memory_budget_bytes": 512_000,
        "recordings": recordings,
    }
    task_path.write_text(
        json.dumps(task, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return task_path
