from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import atomic_write_bytes, canonical_bytes, content_key
from .config import AuditConfig, DEFAULT_CONFIG


FIXED_SEED = 20261127


SPEAKERS: dict[str, dict[str, float | int | str]] = {
    "SPK_A": {"f0_hz": 118, "timbre": "low"},
    "SPK_B": {"f0_hz": 176, "timbre": "mid"},
    "SPK_C": {"f0_hz": 246, "timbre": "high"},
}


# All times are integer milliseconds and land on the 10 ms frame-shift grid.
UTTERANCE_PLAN: list[dict[str, Any]] = [
    {"utterance_id": "U01", "speaker_id": "SPK_A", "start_ms": 400, "end_ms": 3400, "gain": 0.72},
    {"utterance_id": "U02", "speaker_id": "SPK_B", "start_ms": 3700, "end_ms": 6500, "gain": 0.72},
    {"utterance_id": "U03", "speaker_id": "SPK_C", "start_ms": 6800, "end_ms": 9400, "gain": 0.72},
    {"utterance_id": "U04", "speaker_id": "SPK_A", "start_ms": 9700, "end_ms": 11200, "gain": 0.72},
    # Two adjacent claims only 740 ms apart: too-fast switching.
    {"utterance_id": "U05", "speaker_id": "SPK_B", "start_ms": 11380, "end_ms": 11980, "gain": 0.76},
    {"utterance_id": "U06", "speaker_id": "SPK_C", "start_ms": 12120, "end_ms": 14700, "gain": 0.72},
    # B raises gain over A's tail: single-speaker preemption.
    {"utterance_id": "U07", "speaker_id": "SPK_A", "start_ms": 15000, "end_ms": 17600, "gain": 0.46},
    {"utterance_id": "U08", "speaker_id": "SPK_B", "start_ms": 16200, "end_ms": 19100, "gain": 1.0},
    # 1950 ms gap before U09: abnormally long silence.
    {"utterance_id": "U09", "speaker_id": "SPK_C", "start_ms": 21050, "end_ms": 23800, "gain": 0.72},
    {"utterance_id": "U10", "speaker_id": "SPK_B", "start_ms": 24100, "end_ms": 27000, "gain": 0.72},
    {"utterance_id": "U11", "speaker_id": "SPK_A", "start_ms": 27350, "end_ms": 30500, "gain": 0.72},
]


INJECTED_ANOMALIES: list[dict[str, Any]] = [
    {
        "anomaly_type": "rapid_switch",
        "at_utterance_id": "U06",
        "at_ms": 12120,
        "detail_ms": 740,
        "expected_business_code": "BIZ_TURN_TOO_FAST",
    },
    {
        "anomaly_type": "speaker_preemption",
        "at_utterance_id": "U08",
        "at_ms": 16200,
        "detail_ms": 1200,
        "expected_business_code": "BIZ_SPEAKER_PREEMPT",
    },
    {
        "anomaly_type": "silence_too_long",
        "at_utterance_id": "U09",
        "at_ms": 21050,
        "detail_ms": 1950,
        "expected_business_code": "BIZ_SILENCE_TOO_LONG",
    },
]


@dataclass(frozen=True)
class DeterministicSample:
    pcm_int16: np.ndarray
    manifest: dict[str, Any]


def _harmonic_utterance(
    speaker_id: str,
    duration_samples: int,
    gain: float,
    rng: np.random.Generator,
    sample_rate: int,
) -> np.ndarray:
    f0_hz = float(SPEAKERS[speaker_id]["f0_hz"])
    t = np.arange(duration_samples, dtype=np.float64) / sample_rate
    phase_seed = float(rng.uniform(0.0, 0.25))
    wave = np.zeros(duration_samples, dtype=np.float64)
    harmonics = ((1, 1.00), (2, 0.42), (3, 0.20), (4, 0.09), (5, 0.045))
    for harmonic, amplitude in harmonics:
        wave += amplitude * np.sin(
            2.0 * np.pi * f0_hz * harmonic * t + phase_seed * harmonic
        )
    wave /= float(np.max(np.abs(wave)))

    edge_samples = sample_rate // 100  # 10 ms deterministic fade
    edge_samples = min(edge_samples, duration_samples // 4)
    envelope = np.ones(duration_samples, dtype=np.float64)
    envelope[:edge_samples] = np.linspace(0.0, 1.0, edge_samples)
    envelope[-edge_samples:] = np.linspace(1.0, 0.0, edge_samples)
    return wave * envelope * gain


def generate_sample(config: AuditConfig = DEFAULT_CONFIG) -> DeterministicSample:
    """Generate the fixed-seed meeting as int16 mono PCM."""
    rng = np.random.default_rng(FIXED_SEED)
    total_ms = 31_200
    total_samples = total_ms * config.samples_per_ms
    mix = np.zeros(total_samples, dtype=np.float64)

    for item in UTTERANCE_PLAN:
        start = int(item["start_ms"]) * config.samples_per_ms
        end = int(item["end_ms"]) * config.samples_per_ms
        voice = _harmonic_utterance(
            str(item["speaker_id"]),
            end - start,
            float(item["gain"]),
            rng,
            config.sample_rate,
        )
        mix[start:end] += voice

    # Extremely low deterministic room/noise floor; independent of wall clock.
    noise = rng.integers(-96, 97, size=total_samples, dtype=np.int16).astype(np.float64)
    mix += noise / 32768.0 * 0.9

    # Soft clip before deterministic int16 quantization.
    peak = float(np.max(np.abs(mix)))
    if peak > 0.98:
        mix = np.tanh(mix / max(peak, 1.0))
    pcm = np.rint(np.clip(mix * 32767.0, -32768, 32767)).astype(np.int16)

    manifest = {
        "schema": "meeting-audit/sample-manifest/v1",
        "seed": FIXED_SEED,
        "sample_rate_hz": config.sample_rate,
        "channels": 1,
        "sample_width_bytes": 2,
        "encoding": "pcm_s16le",
        "total_ms": total_ms,
        "total_samples": total_samples,
        "pcm_sha256": content_key(pcm.tobytes()),
        "config": config.to_dict(),
        "speakers": SPEAKERS,
        "utterances": UTTERANCE_PLAN,
        "injected_anomalies": INJECTED_ANOMALIES,
    }
    manifest["manifest_key"] = content_key({k: v for k, v in manifest.items() if k != "manifest_key"})
    return DeterministicSample(pcm_int16=pcm, manifest=manifest)


def write_sample_bundle(
    output_dir: str | Path,
    config: AuditConfig = DEFAULT_CONFIG,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    sample = generate_sample(config)
    pcm_path = out / "meeting.pcm"
    wav_path = out / "meeting.wav"
    manifest_path = out / "sample_manifest.json"
    atomic_write_bytes(pcm_path, sample.pcm_int16.tobytes())

    import wave

    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(config.sample_rate)
        wav.writeframes(sample.pcm_int16.tobytes())

    atomic_write_bytes(manifest_path, canonical_bytes(sample.manifest) + b"\n")
    return {"pcm": pcm_path, "wav": wav_path, "manifest": manifest_path}


def load_pcm(path: str | Path) -> np.ndarray:
    raw = Path(path).read_bytes()
    return np.frombuffer(raw, dtype="<i2").copy()


def load_manifest(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
