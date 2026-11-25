"""Deterministic local PCM and threshold sample generation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .dsp import FRAME_HOP, FRAME_LENGTH, SAMPLE_RATE, encode_pcm_s16
from .hashing import canonical_hash, sha256_bytes

DEFAULT_SEED = 20261126


@dataclass(frozen=True)
class SegmentSpec:
    segment_id: str
    start_sample: int
    end_sample: int
    kind: str
    amplitude: float = 0.0
    fundamental_hz: float = 140.0
    brightness: float = 1.0
    noise_level: float = 0.0

    @property
    def duration_seconds(self) -> float:
        return (self.end_sample - self.start_sample) / SAMPLE_RATE


def default_segments() -> list[SegmentSpec]:
    sr = SAMPLE_RATE
    return [
        SegmentSpec("silence_intro", 0, sr, "silence"),
        SegmentSpec("speech_normal", sr, 3 * sr, "voiced", amplitude=0.16, fundamental_hz=132.0, brightness=0.8, noise_level=0.006),
        SegmentSpec("gain_jump", 3 * sr, 5 * sr, "voiced", amplitude=0.46, fundamental_hz=132.0, brightness=0.8, noise_level=0.006),
        SegmentSpec("silence_mid", 5 * sr, 6 * sr, "silence"),
        SegmentSpec(
            "spectral_shift",
            6 * sr,
            9 * sr,
            "voiced",
            amplitude=0.16,
            fundamental_hz=264.0,
            brightness=2.4,
            noise_level=0.025,
        ),
        SegmentSpec("speech_recovery", 9 * sr, 12 * sr, "voiced", amplitude=0.16, fundamental_hz=132.0, brightness=0.8, noise_level=0.006),
    ]


def _voiced_segment(spec: SegmentSpec, seed: int) -> np.ndarray:
    count = spec.end_sample - spec.start_sample
    sample_index = np.arange(count, dtype=np.float64)
    time = sample_index / SAMPLE_RATE
    rng = np.random.default_rng(seed + spec.start_sample)

    signal = np.zeros(count, dtype=np.float64)
    weights = np.array([1.0, 0.58, 0.34, 0.22, 0.13, 0.08, 0.045, 0.025], dtype=np.float64)
    for harmonic, weight in enumerate(weights, start=1):
        frequency = spec.fundamental_hz * harmonic
        phase = rng.uniform(0.0, 2.0 * np.pi)
        brightness_weight = weight * (spec.brightness ** max(0, harmonic - 2) * 0.35 if harmonic > 2 else 1.0)
        signal += brightness_weight * np.sin(2.0 * np.pi * frequency * time + phase)

    white = rng.standard_normal(count)
    # A short cumulative smoothing gives a stable breath/noise component while
    # retaining reproducible high-frequency energy.
    kernel = np.ones(5, dtype=np.float64) / 5.0
    shaped_noise = np.convolve(white, kernel, mode="same")
    signal += spec.noise_level * (1.0 + spec.brightness) * white + 0.4 * spec.noise_level * shaped_noise

    normalization = float(np.sqrt(np.mean(signal * signal))) or 1.0
    signal = spec.amplitude * signal / normalization

    ramp = min(80, count // 4)
    if ramp > 0:
        signal[:ramp] *= np.linspace(0.0, 1.0, ramp, dtype=np.float64)
        signal[-ramp:] *= np.linspace(1.0, 0.0, ramp, dtype=np.float64)
    return signal


def generate_pcm(seed: int = DEFAULT_SEED, segments: list[SegmentSpec] | None = None) -> np.ndarray:
    segments = segments or default_segments()
    total_samples = max(segment.end_sample for segment in segments)
    pcm = np.zeros(total_samples, dtype=np.float64)
    for index, spec in enumerate(segments):
        if spec.kind == "silence":
            continue
        pcm[spec.start_sample : spec.end_sample] = _voiced_segment(spec, seed + index * 1009)
    peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
    if peak > 0.98:
        pcm *= 0.98 / peak
    return pcm


def _threshold_rows(segments: list[SegmentSpec]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    normal_fractions = {
        "d1_expected": 0.0015,
        "d2_expected": 0.0050,
        "d3_expected": 0.0195,
        "a3_expected": 0.9740,
        "d1_tolerance": 0.010,
        "d2_tolerance": 0.015,
        "d3_tolerance": 0.025,
        "a3_tolerance": 0.050,
    }
    silence_fractions = {
        "d1_expected": 0.0,
        "d2_expected": 0.0,
        "d3_expected": 0.0,
        "a3_expected": 0.0,
        "d1_tolerance": 0.010,
        "d2_tolerance": 0.010,
        "d3_tolerance": 0.010,
        "a3_tolerance": 0.010,
    }
    normal_limits = {
        "d1_limit": 0.025,
        "d2_limit": 0.060,
        "d3_limit": 0.120,
        "a3_limit": 0.750,
    }
    silence_limits = {
        "d1_limit": 0.010,
        "d2_limit": 0.010,
        "d3_limit": 0.010,
        "a3_limit": 0.010,
    }
    for spec in segments:
        if spec.kind == "silence":
            rows.append(
                {
                    "segment_id": spec.segment_id,
                    "segment_class": "silence",
                    "start_sample": spec.start_sample,
                    "end_sample": spec.end_sample,
                    "energy_gate_db": -45.0,
                    "target_rms_db": -120.0,
                    "energy_tolerance_db": 2.0,
                    "distribution_tolerance": 0.04,
                    "subband_tolerance": 0.01,
                    **silence_limits,
                    **silence_fractions,
                }
            )
        elif spec.segment_id == "gain_jump":
            rows.append(
                {
                    "segment_id": spec.segment_id,
                    "segment_class": "voiced",
                    "start_sample": spec.start_sample,
                    "end_sample": spec.end_sample,
                    "energy_gate_db": -45.0,
                    "target_rms_db": -16.0,
                    "energy_tolerance_db": 5.0,
                    "distribution_tolerance": 0.12,
                    "subband_tolerance": 0.10,
                    **normal_limits,
                    **normal_fractions,
                }
            )
        elif spec.segment_id == "spectral_shift":
            rows.append(
                {
                    "segment_id": spec.segment_id,
                    "segment_class": "voiced",
                    "start_sample": spec.start_sample,
                    "end_sample": spec.end_sample,
                    "energy_gate_db": -45.0,
                    "target_rms_db": -16.0,
                    "energy_tolerance_db": 5.0,
                    "distribution_tolerance": 0.12,
                    "subband_tolerance": 0.02,
                    **normal_limits,
                    **normal_fractions,
                }
            )
        else:
            rows.append(
                {
                    "segment_id": spec.segment_id,
                    "segment_class": "voiced",
                    "start_sample": spec.start_sample,
                    "end_sample": spec.end_sample,
                    "energy_gate_db": -45.0,
                    "target_rms_db": -16.0,
                    "energy_tolerance_db": 5.0,
                    "distribution_tolerance": 0.12,
                    "subband_tolerance": 0.10,
                    **normal_limits,
                    **normal_fractions,
                }
            )
    return rows


def build_v1_config(seed: int = DEFAULT_SEED, segments: list[SegmentSpec] | None = None) -> dict[str, Any]:
    segments = segments or default_segments()
    return {
        "schema": "audio-qc-flat-thresholds/v1",
        "version": 1,
        "curve_id": "deterministic-local-qc",
        "revision": f"seed-{seed}-v1",
        "sample_rate": SAMPLE_RATE,
        "frame_length": FRAME_LENGTH,
        "frame_hop": FRAME_HOP,
        "table": _threshold_rows(segments),
    }


def build_v2_config(seed: int = DEFAULT_SEED, segments: list[SegmentSpec] | None = None) -> dict[str, Any]:
    v1 = build_v1_config(seed, segments)
    shards: list[dict[str, Any]] = []
    for row in v1["table"]:
        shards.append(
            {
                "schema": "audio-qc-shard/v2",
                "segment_id": row["segment_id"],
                "segment_class": row["segment_class"],
                "range": {
                    "start_sample": row["start_sample"],
                    "end_sample": row["end_sample"],
                },
                "energy": {
                    "gate_db": row["energy_gate_db"],
                    "target_db": row["target_rms_db"],
                    "tolerance_db": row["energy_tolerance_db"],
                    "distribution_tolerance": row["distribution_tolerance"],
                },
                "wavelet": {
                    "family": "haar",
                    "levels": 3,
                    "distribution_tolerance": row["distribution_tolerance"],
                    "bands": {
                        name: {
                            "expected_fraction": row.get(f"{name}_expected", {
                                "d1": 0.14,
                                "d2": 0.22,
                                "d3": 0.28,
                                "a3": 0.36,
                            }[name]),
                            "fraction_tolerance": row.get(f"{name}_tolerance", row["subband_tolerance"]),
                            "rms_limit": row[f"{name}_limit"],
                            "weight": {"d1": 1.2, "d2": 1.0, "d3": 0.9, "a3": 0.8}[name],
                        }
                        for name in ("d1", "d2", "d3", "a3")
                    },
                },
                "adjudication": {
                    "profile": "weighted-subband-v2",
                    "aggregation": "max_normalized_contribution",
                    "priority": ["energy", "subband", "distribution"],
                },
            }
        )
    return {
        "schema": "audio-qc-sharded-thresholds/v2",
        "version": 2,
        "curve_id": v1["curve_id"],
        "revision": f"seed-{seed}-v2",
        "sample_rate": SAMPLE_RATE,
        "frame_length": FRAME_LENGTH,
        "frame_hop": FRAME_HOP,
        "shards": shards,
    }


def generate_bundle(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    segments = default_segments()
    pcm = generate_pcm(seed, segments)
    pcm_bytes = encode_pcm_s16(pcm)
    v1 = build_v1_config(seed, segments)
    v2 = build_v2_config(seed, segments)
    metadata = {
        "seed": seed,
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "sample_format": "signed-int16-le",
        "frame_length": FRAME_LENGTH,
        "frame_hop": FRAME_HOP,
        "total_samples": int(pcm.size),
        "pcm_sha256": sha256_bytes(pcm_bytes),
        "v1_sha256": canonical_hash(v1),
        "v2_sha256": canonical_hash(v2),
        "segments": [asdict(spec) for spec in segments],
    }
    return {
        "pcm": pcm,
        "pcm_bytes": pcm_bytes,
        "metadata": metadata,
        "v1": v1,
        "v2": v2,
    }


def write_sample_bundle(out_dir: str | Path, seed: int = DEFAULT_SEED) -> dict[str, Path]:
    bundle = generate_bundle(seed)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "pcm": out_dir / "sample_16k_mono_s16.pcm",
        "metadata": out_dir / "sample_metadata.json",
        "v1": out_dir / "thresholds_v1.json",
        "v2": out_dir / "thresholds_v2.json",
    }
    paths["pcm"].write_bytes(bundle["pcm_bytes"])
    paths["metadata"].write_text(json.dumps(bundle["metadata"], ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["v1"].write_text(json.dumps(bundle["v1"], ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["v2"].write_text(json.dumps(bundle["v2"], ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return paths
