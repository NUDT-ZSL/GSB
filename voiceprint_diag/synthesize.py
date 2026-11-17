"""Fixed-seed generation of deterministic WAV samples and demo manifests."""

from __future__ import annotations

import argparse
import hashlib
import math
import struct
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import write_json_atomic

SAMPLE_RATE = 16000

SPEAKER_PROFILES: dict[str, dict[str, Any]] = {
    "speaker_a": {
        "f0_mean": 116.0,
        "f0_std": 3.2,
        "formants": ((690.0, 90.0), (1230.0, 130.0), (2550.0, 220.0)),
    },
    "speaker_b": {
        "f0_mean": 218.0,
        "f0_std": 7.5,
        "formants": ((510.0, 75.0), (1780.0, 150.0), (2480.0, 210.0)),
    },
    "speaker_c": {
        "f0_mean": 305.0,
        "f0_std": 8.5,
        "formants": ((430.0, 65.0), (2320.0, 180.0), (3150.0, 260.0)),
    },
}


def _stable_seed(text: str, master_seed: int) -> int:
    digest = hashlib.sha256(f"{master_seed}:{text}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "little", signed=False)


def _smooth_noise(rng: np.random.Generator, count: int, width: int = 31) -> np.ndarray:
    raw = rng.normal(0.0, 1.0, count)
    kernel = np.ones(width, dtype=np.float64) / float(width)
    smooth = np.convolve(raw, kernel, mode="same")
    standard_deviation = float(np.std(smooth))
    return smooth / standard_deviation if standard_deviation > 1e-12 else raw


def synthesise_voice(
    profile: dict[str, Any],
    *,
    seed: int,
    duration_seconds: float = 1.37,
    variant: int = 0,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    count = int(round(duration_seconds * SAMPLE_RATE))
    time = np.arange(count, dtype=np.float64) / float(SAMPLE_RATE)
    f0_base = float(profile["f0_mean"]) * (1.0 + 0.012 * variant)
    f0_std = float(profile["f0_std"])
    slow_variation = _smooth_noise(rng, count, width=97)
    fast_variation = _smooth_noise(rng, count, width=17)
    f0_inst = f0_base + f0_std * (0.72 * slow_variation + 0.28 * fast_variation)
    f0_inst = np.clip(f0_inst, max(50.0, f0_base - 4.0 * f0_std), f0_base + 4.0 * f0_std)
    phase = 2.0 * math.pi * np.cumsum(f0_inst) / float(SAMPLE_RATE)
    # Speech-like bursts plus clear inter-syllable low-energy regions.
    syllable_phase = 2.0 * math.pi * (3.65 + 0.08 * variant) * time + 0.4 * variant
    syllables = np.maximum(0.0, np.sin(syllable_phase)) ** 0.55
    pause_pattern = ((np.floor(time / 0.19 + 0.13 * variant) % 7.0) != 5.0).astype(np.float64)
    amplitude_envelope = (0.12 + 0.88 * syllables) * pause_pattern
    harmonic = np.zeros(count, dtype=np.float64)
    formants = tuple(profile["formants"])
    harmonic_phase = rng.uniform(0.0, 2.0 * math.pi, 12)
    for harmonic_index in range(1, 13):
        frequency = harmonic_index * f0_base
        weight = 0.018
        for formant_hz, bandwidth_hz in formants:
            weight += 1.0 * math.exp(-0.5 * ((frequency - formant_hz) / bandwidth_hz) ** 2)
        weight /= harmonic_index ** 0.35
        harmonic += weight * np.sin(harmonic_index * phase + harmonic_phase[harmonic_index - 1])
    harmonic /= max(1e-9, float(np.max(np.abs(harmonic))))
    voiced = 0.64 * amplitude_envelope * harmonic
    # Fixed-seed, stationary background noise around -62 dBFS RMS.
    background = rng.normal(0.0, 0.00082, count)
    signal = voiced + background
    signal = np.tanh(1.15 * signal)
    signal /= max(1.0, float(np.max(np.abs(signal))) / 0.82)
    return np.clip(signal, -0.999969, 0.999969)


def write_pcm16_wav(
    path: Path,
    samples: np.ndarray,
    *,
    sample_rate: int = SAMPLE_RATE,
    channels: int = 1,
    bits_per_sample: int = 16,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if bits_per_sample == 16:
        values = np.asarray(samples, dtype=np.float64).reshape(-1)
        pcm = np.where(values >= 0, np.floor(values * 32767.0 + 0.5), np.ceil(values * 32767.0 - 0.5)).astype("<i2")
        if channels == 2:
            pcm = np.repeat(pcm, 2)
        payload = pcm.tobytes()
    elif bits_per_sample == 8:
        values = np.asarray(samples, dtype=np.float64).reshape(-1)
        pcm = np.where(values >= 0, np.floor(values * 127.0 + 0.5), np.ceil(values * 127.0 - 0.5)).astype(np.int16)
        payload = (pcm + 128).astype(np.uint8).tobytes()
        if channels == 2:
            expanded = np.frombuffer(payload, dtype=np.uint8)
            payload = np.repeat(expanded, 2).tobytes()
    else:
        raise ValueError("demo writer supports 8-bit or 16-bit PCM")
    block_align = channels * (bits_per_sample // 8)
    byte_rate = sample_rate * block_align
    fmt = struct.pack(
        "<HHIIHH",
        1,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
    )
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(payload)) + payload
    if len(payload) % 2:
        chunks += b"\x00"
    riff = b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks
    path.write_bytes(riff)


def _corrupt_data_length(path: Path) -> None:
    blob = bytearray(path.read_bytes())
    data_offset = blob.find(b"data")
    if data_offset < 0 or data_offset + 8 > len(blob):
        raise RuntimeError("generated WAV has no data chunk")
    declared = struct.unpack_from("<I", blob, data_offset + 4)[0]
    struct.pack_into("<I", blob, data_offset + 4, declared + 4)
    path.write_bytes(bytes(blob))


def demo_config() -> dict[str, Any]:
    return {
        "frame_ms": 25.0,
        "hop_ms": 10.0,
        "mel_bands": 26,
        "mel_max_freq": 7600.0,
        "cepstral_order": 12,
        "embedding_dim": 16,
        "quantization_levels": 256,
        "fractional_bits": 12,
        "precision_trace_bit_step": 4,
        "similarity_threshold": 0.82,
        "min_valid_frames": 12,
        "concurrency": 4,
        "snapshot_interval": 8,
        "silence_energy_threshold": -11.0,
        "feature_significant_digits": 12,
        "precision_tolerance": 0.0001,
        "expected_sample_rate": 16000,
        "expected_channels": 1,
        "expected_sample_width": 2,
        "f0_min_hz": 65.0,
        "f0_max_hz": 420.0,
        "f0_voicing_threshold": 0.3,
        "projection_seed": 20261117,
        "projection_matrix_scale": 128,
        "projection_clip": 4.0,
        "similarity_scale": 1_000_000_000,
        "global_version": 1,
        "quantile_probabilities": [0.1, 0.5, 0.9],
    }


def _manifest_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    speakers = {
        "speaker_a": ["speaker_a/a1.wav", "speaker_a/a2.wav"],
        "speaker_b": ["speaker_b/b1.wav", "speaker_b/b2.wav"],
        "speaker_c": ["speaker_c/c1.wav", "speaker_c/c2.wav"],
    }
    pairs = {
        "speakers": speakers,
        "same_speaker_pairs": [
            {"id": "same-a-01", "left": "speaker_a/a1.wav", "right": "speaker_a/a2.wav"},
            {"id": "same-b-01", "left": "speaker_b/b1.wav", "right": "speaker_b/b2.wav"},
            {"id": "same-c-01", "left": "speaker_c/c1.wav", "right": "speaker_c/c2.wav"},
        ],
        "cross_speaker_pairs": [
            {"id": "cross-ab-01", "left": "speaker_a/a1.wav", "right": "speaker_b/b1.wav"},
            {"id": "cross-ac-01", "left": "speaker_a/a2.wav", "right": "speaker_c/c1.wav"},
            {"id": "cross-bc-01", "left": "speaker_b/b2.wav", "right": "speaker_c/c2.wav"},
        ],
    }
    anomalies = {
        "injections": [
            {
                "id": "INJ-RATE-001",
                "sample": "defects/bad_rate.wav",
                "stage": "audio",
                "kind": "wrong_sample_rate",
                "expected_error": "E_AUDIO_FORMAT",
            },
            {
                "id": "INJ-CHANNELS-001",
                "sample": "defects/bad_channels.wav",
                "stage": "audio",
                "kind": "wrong_channel_count",
                "expected_error": "E_AUDIO_FORMAT",
            },
            {
                "id": "INJ-BITS-001",
                "sample": "defects/bad_bits.wav",
                "stage": "audio",
                "kind": "wrong_bit_depth",
                "expected_error": "E_AUDIO_FORMAT",
            },
            {
                "id": "INJ-TRUNC-001",
                "sample": "defects/bad_truncated.wav",
                "stage": "audio",
                "kind": "declared_data_too_long",
                "expected_error": "E_AUDIO_FORMAT",
            },
            {
                "id": "INJ-SILENCE-001",
                "sample": "defects/short_silence.wav",
                "stage": "aggregation",
                "kind": "insufficient_valid_frames",
                "expected_error": "E_INSUFFICIENT_FRAMES",
            },
        ]
    }
    return pairs, anomalies


def generate_demo(output_dir: str | Path, seed: int = 20261117) -> dict[str, str]:
    output = Path(output_dir)
    samples_root = output / "samples"
    if samples_root.exists():
        for path in sorted(samples_root.rglob("*.wav")):
            path.unlink()
    for speaker_id, samples in {
        "speaker_a": ["a1.wav", "a2.wav"],
        "speaker_b": ["b1.wav", "b2.wav"],
        "speaker_c": ["c1.wav", "c2.wav"],
    }.items():
        for variant, filename in enumerate(samples):
            waveform = synthesise_voice(
                SPEAKER_PROFILES[speaker_id],
                seed=_stable_seed(f"{speaker_id}/{filename}", seed),
                variant=variant,
            )
            write_pcm16_wav(samples_root / speaker_id / filename, waveform)
    a_profile = SPEAKER_PROFILES["speaker_a"]
    bad_rate_wave = synthesise_voice(a_profile, seed=_stable_seed("defect-rate", seed), duration_seconds=0.9)
    write_pcm16_wav(samples_root / "defects" / "bad_rate.wav", bad_rate_wave, sample_rate=8000)
    bad_channels_wave = synthesise_voice(a_profile, seed=_stable_seed("defect-channels", seed), duration_seconds=0.7)
    write_pcm16_wav(samples_root / "defects" / "bad_channels.wav", bad_channels_wave, channels=2)
    bad_bits_wave = synthesise_voice(a_profile, seed=_stable_seed("defect-bits", seed), duration_seconds=0.7)
    write_pcm16_wav(samples_root / "defects" / "bad_bits.wav", bad_bits_wave, bits_per_sample=8)
    truncated_wave = synthesise_voice(a_profile, seed=_stable_seed("defect-truncated", seed), duration_seconds=0.7)
    truncated_path = samples_root / "defects" / "bad_truncated.wav"
    write_pcm16_wav(truncated_path, truncated_wave)
    _corrupt_data_length(truncated_path)
    silence_rng = np.random.default_rng(_stable_seed("defect-silence", seed))
    silence = silence_rng.normal(0.0, 0.00035, int(0.22 * SAMPLE_RATE))
    write_pcm16_wav(samples_root / "defects" / "short_silence.wav", silence)
    pairs, anomalies = _manifest_documents()
    write_json_atomic(output / "config.json", demo_config())
    write_json_atomic(output / "pairs.json", pairs)
    write_json_atomic(output / "anomalies.json", anomalies)
    return {
        "samples": str(samples_root),
        "config": str(output / "config.json"),
        "pairs": str(output / "pairs.json"),
        "anomalies": str(output / "anomalies.json"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate deterministic demo voiceprint inputs")
    parser.add_argument("--output", required=True, help="directory for samples and manifests")
    parser.add_argument("--seed", type=int, default=20261117)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    result = generate_demo(arguments.output, arguments.seed)
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
