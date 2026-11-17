"""Fixed-seed, closed-loop synthetic WAV and dialogue generation."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .constants import FINE_EMOTIONS
from .util import atomic_write_json


SAMPLE_RATE = 16000
EMOTION_PROFILES = {
    "angry": {
        "f0": 210.0, "f0_var": 35.0, "energy": 0.50, "dynamic": 0.62,
        "rate": 5.2, "noise": 0.050, "drift": 1.3,
    },
    "disgust": {
        "f0": 135.0, "f0_var": 14.0, "energy": 0.24, "dynamic": 0.28,
        "rate": 3.2, "noise": 0.085, "drift": 0.7,
    },
    "fear": {
        "f0": 235.0, "f0_var": 48.0, "energy": 0.30, "dynamic": 0.52,
        "rate": 5.8, "noise": 0.070, "drift": 2.0,
    },
    "happy": {
        "f0": 190.0, "f0_var": 24.0, "energy": 0.46, "dynamic": 0.46,
        "rate": 5.0, "noise": 0.038, "drift": 1.7,
    },
    "neutral": {
        "f0": 150.0, "f0_var": 7.0, "energy": 0.27, "dynamic": 0.13,
        "rate": 3.8, "noise": 0.030, "drift": 0.8,
    },
    "sad": {
        "f0": 105.0, "f0_var": 9.0, "energy": 0.13, "dynamic": 0.10,
        "rate": 2.3, "noise": 0.018, "drift": 0.5,
    },
}
INTENTS = ("greet", "inform", "request", "confirm", "complain", "apologize")
SPEAKERS = ("A", "B", "A", "C", "B", "C")


def _unit(seed: int, *parts: Any) -> float:
    material = "|".join([str(seed), *[str(part) for part in parts]]).encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64)


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = np.floor(pcm * 32767.0 + 0.5).astype("<i2")
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm.tobytes())


def synthesize_clip(emotion: str, *, seed: int, index: int, duration: float, speaker_shift: float) -> np.ndarray:
    profile = EMOTION_PROFILES[emotion]
    sample_count = max(1, int(round(duration * SAMPLE_RATE)))
    t = np.arange(sample_count, dtype=np.float64) / SAMPLE_RATE
    phase0 = 2.0 * math.pi * _unit(seed, "phase", index)
    jitter = (_unit(seed, "jitter", index) - 0.5)
    f0_base = profile["f0"] + speaker_shift + 2.0 * jitter
    f0_track = (
        f0_base
        + 0.52 * profile["f0_var"] * np.sin(2.0 * math.pi * profile["drift"] * t + phase0)
        + 0.22 * profile["f0_var"] * np.sin(2.0 * math.pi * (2.6 + 0.2 * jitter) * t + 0.7 * phase0)
    )
    f0_track = np.clip(f0_track, 70.0, 460.0)
    phase = 2.0 * math.pi * np.cumsum(f0_track) / SAMPLE_RATE
    pulse = (0.5 + 0.5 * np.sin(2.0 * math.pi * profile["rate"] * t + phase0)) ** 2.0
    slow = 0.5 + 0.5 * np.sin(2.0 * math.pi * 0.45 * t + 0.4 * phase0)
    envelope_shape = (0.14 + 0.86 * pulse) * (0.72 + 0.28 * slow)
    desired = profile["energy"]
    average_shape = float(np.mean(envelope_shape))
    harmonic_rms = 0.78
    amplitude = desired / max(average_shape * harmonic_rms, 1e-6)
    voice = (
        np.sin(phase)
        + 0.34 * np.sin(2.0 * phase + 0.2 * phase0)
        + 0.16 * np.sin(3.0 * phase + 0.4 * phase0)
    )
    noise_phase = 2.0 * math.pi * _unit(seed, "noise", index)
    high = (
        np.sin(2.0 * math.pi * 2711.0 * t + noise_phase)
        + np.sin(2.0 * math.pi * 3931.0 * t + 0.5 * noise_phase)
        + np.sin(2.0 * math.pi * 5237.0 * t + 1.3 * noise_phase)
    ) / 3.0
    noise_scale = profile["noise"] * (0.75 + 0.5 * _unit(seed, "noise-scale", index))
    signal = amplitude * envelope_shape * voice + noise_scale * high
    # A deterministic very low-level floor avoids accidental all-zero frames.
    signal += 0.0008 * np.sin(2.0 * math.pi * 173.0 * t + phase0)
    peak = float(np.max(np.abs(signal)))
    if peak > 0.98:
        signal = signal / peak * 0.98
    return signal


def _zero_duration_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(b"")


def _build_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "frame_length_ms": 25,
        "frame_shift_ms": 10,
        "acoustic_feature_set": [
            "f0_mean", "f0_std", "f0_range", "f0_slope", "energy_mean",
            "energy_std", "energy_dynamic_range", "zcr_mean", "zcr_std",
            "voiced_ratio", "speech_rate", "rhythm_regularity", "pause_ratio",
            "activity_ratio",
        ],
        "emotion_granularity": "fine",
        "classifier": "auto",
        "boundary_margin": 0.03,
        "reference_precision": 0.05,
        "fusion": {
            "window": 5,
            "window_mode": "adaptive",
            "decay": 0.65,
            "same_speaker_weight": 1.0,
            "cross_speaker_weight": 0.55,
            "missing_predecessor_rule": "nearest_available",
        },
        "feature_dimension_limit": 12,
        "scale_tier": "auto",
        "scale_tiers": {
            "small_max_units": 90,
            "medium_max_units": 1600,
            "duration_weight": 10,
        },
        "concurrency": 4,
        "snapshot_interval": 8,
    }


def _build_injections() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "injections": [
            {"id": "INJ-001", "kind": "nonfinite_sample", "target": "audio/anomaly_nonfinite.wav"},
            {"id": "INJ-002", "kind": "zero_duration", "target": "audio/anomaly_zero.wav"},
            {"id": "INJ-003", "kind": "bad_audio_format", "target": "audio/anomaly_bad_rate.wav"},
            {"id": "INJ-004", "kind": "invalid_granularity", "target": "audio/anomaly_bad_granularity.wav", "value": "seven_way"},
            {"id": "INJ-005", "kind": "missing_predecessor", "target": "T010"},
        ],
    }


def generate_demo(
    out_dir: Path,
    *,
    seed: int = 20261117,
    segments: int = 76,
    turns: int = 18,
    duration: float = 0.82,
    include_anomalies: bool = True,
    write_default_configs: bool = True,
) -> dict[str, Any]:
    if segments <= 0 or turns <= 0 or turns > segments:
        raise ValueError("require segments > 0 and 0 < turns <= segments")
    out_dir = Path(out_dir)
    audio_dir = out_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    turn_records = []
    for index in range(segments):
        emotion = FINE_EMOTIONS[(index * 5 + index // 7) % len(FINE_EMOTIONS)]
        speaker = SPEAKERS[index % len(SPEAKERS)]
        speaker_shift = {"A": -4.0, "B": 3.0, "C": 7.0}[speaker]
        clip_duration = duration + 0.18 * (_unit(seed, "duration", index) - 0.5)
        samples = synthesize_clip(
            emotion,
            seed=seed,
            index=index,
            duration=clip_duration,
            speaker_shift=speaker_shift,
        )
        filename = f"audio/seg_{index + 1:04d}.wav"
        _write_wav(out_dir / filename, samples)
        if index < turns:
            turn_records.append(
                {
                    "turn_id": f"T{index + 1:03d}",
                    "order": index + 1,
                    "speaker": speaker,
                    "audio": filename,
                    "prev_turn": f"T{index:03d}" if index > 0 else None,
                    "intent": INTENTS[(index + seed) % len(INTENTS)],
                    "text": f"synthetic {emotion} utterance {index + 1}",
                    "emotion_label": emotion,
                }
            )
    if include_anomalies:
        anomaly_signal = synthesize_clip(
            "neutral", seed=seed, index=segments + 1, duration=duration, speaker_shift=0.0
        )
        _write_wav(audio_dir / "anomaly_nonfinite.wav", anomaly_signal)
        _zero_duration_wav(audio_dir / "anomaly_zero.wav")
        _write_wav(audio_dir / "anomaly_bad_rate.wav", anomaly_signal, sample_rate=8000)
        bad_granularity_signal = synthesize_clip(
            "happy", seed=seed, index=segments + 4, duration=duration, speaker_shift=1.0
        )
        _write_wav(audio_dir / "anomaly_bad_granularity.wav", bad_granularity_signal)
    script = {
        "schema_version": 1,
        "dialogue_id": f"deterministic-demo-{seed}",
        "turns": turn_records,
    }
    atomic_write_json(out_dir / "dialog_script.json", script)
    if write_default_configs:
        atomic_write_json(out_dir / "config.json", _build_config())
        atomic_write_json(out_dir / "anomalies.json", _build_injections())
    manifest = {
        "seed": seed,
        "segments_requested": segments,
        "turns": turns,
        "duration_base_sec": duration,
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "sample_width_bytes": 2,
        "audio_files": sorted(
            path.relative_to(out_dir).as_posix() for path in audio_dir.glob("*.wav")
        ),
        "anomalies": _build_injections()["injections"] if include_anomalies else [],
    }
    atomic_write_json(out_dir / "sample_manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic local voice-dialogue samples")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=20261117)
    parser.add_argument("--segments", type=int, default=76)
    parser.add_argument("--turns", type=int, default=18)
    parser.add_argument("--duration", type=float, default=0.82)
    parser.add_argument("--no-anomalies", action="store_true")
    parser.add_argument("--no-configs", action="store_true")
    args = parser.parse_args(argv)
    manifest = generate_demo(
        args.out,
        seed=args.seed,
        segments=args.segments,
        turns=args.turns,
        duration=args.duration,
        include_anomalies=not args.no_anomalies,
        write_default_configs=not args.no_configs,
    )
    print(json.dumps({"out": str(args.out), **manifest}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
