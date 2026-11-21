"""Deterministic local 16 kHz mono PCM synthesis."""

from __future__ import annotations

import shutil
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .errors import fail
from .util import canonical_json_bytes, read_json, sha256_bytes, write_bytes_atomic


def _clip_seed(global_seed: int, clip: dict[str, Any]) -> int:
    return int(global_seed) + int(clip.get("seed_offset", 0))


def _noise_seed(seed: int, clip: dict[str, Any], noise_cfg: dict[str, Any]) -> int:
    return seed + int(clip.get("noise_seed_offset", 0)) + int(noise_cfg.get("seed_salt", 0))


def _segment_waveform(
    sample_count: int,
    sample_rate: int,
    gain: float,
    f0_hz: float,
    fade_ms: float,
    modulation_hz: float,
) -> np.ndarray:
    t = np.arange(sample_count, dtype=np.float64) / sample_rate
    wave_data = (
        np.sin(2.0 * np.pi * f0_hz * t)
        + 0.38 * np.sin(2.0 * np.pi * 2.0 * f0_hz * t + 0.2)
        + 0.17 * np.sin(2.0 * np.pi * 3.0 * f0_hz * t + 0.7)
    )
    modulation = 0.88 + 0.12 * np.sin(2.0 * np.pi * modulation_hz * t + 0.11)
    peak = float(np.max(np.abs(wave_data))) if sample_count else 0.0
    if peak > 0:
        wave_data = wave_data / peak
    wave_data *= gain * modulation

    fade_samples = min(sample_count // 2, int(round(fade_ms * sample_rate / 1000.0)))
    if fade_samples > 0:
        ramp = np.linspace(0.0, 1.0, fade_samples, endpoint=False, dtype=np.float64)
        wave_data[:fade_samples] *= ramp
        wave_data[-fade_samples:] *= ramp[::-1]
    return wave_data


def _synthesize_clip(
    clip_cfg: dict[str, Any],
    global_seed: int,
    sample_rate: int,
    noise_cfg: dict[str, Any],
    fade_ms: float,
) -> tuple[np.ndarray, list[dict[str, Any]], dict[str, Any]]:
    clip_id = str(clip_cfg["clip_id"])
    duration_ms = float(clip_cfg["duration_ms"])
    total_samples = int(round(duration_ms * sample_rate / 1000.0))
    seed = _clip_seed(global_seed, clip_cfg)
    noise_rng = np.random.default_rng(_noise_seed(seed, clip_cfg, noise_cfg))
    noise_sigma = float(noise_cfg.get("amplitude", 0.0))
    mixed = noise_rng.normal(0.0, noise_sigma, size=total_samples).astype(np.float64)

    annotations: list[dict[str, Any]] = []
    segments = sorted(
        clip_cfg.get("segments", []),
        key=lambda segment: (float(segment["start_ms"]), float(segment["end_ms"])),
    )
    previous_end = 0
    for segment in segments:
        start_ms = float(segment["start_ms"])
        end_ms = float(segment["end_ms"])
        if start_ms < previous_end:
            fail(
                "E_SYNTH_CONFIG",
                "synthetic segments must not overlap",
                clip_id=clip_id,
                start_ms=start_ms,
                previous_end_ms=previous_end,
            )
        if start_ms < 0 or end_ms > duration_ms or end_ms <= start_ms:
            fail(
                "E_SYNTH_CONFIG",
                "synthetic segment lies outside the clip",
                clip_id=clip_id,
                start_ms=start_ms,
                end_ms=end_ms,
                duration_ms=duration_ms,
            )
        start_sample = int(round(start_ms * sample_rate / 1000.0))
        end_sample = int(round(end_ms * sample_rate / 1000.0))
        segment_samples = end_sample - start_sample
        waveform = _segment_waveform(
            segment_samples,
            sample_rate,
            float(segment.get("gain", 0.4)),
            float(segment.get("f0_hz", 180.0)),
            fade_ms,
            float(segment.get("modulation_hz", 4.0)),
        )
        mixed[start_sample:end_sample] += waveform
        annotations.append(
            {
                "clip_id": clip_id,
                "segment_id": str(segment["segment_id"]),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "speaker_label": str(segment["speaker_label"]),
                "label_revision": int(segment.get("label_revision", 1)),
                "confidence": float(segment.get("confidence", 0.9)),
            }
        )
        previous_end = end_ms

    mixed = np.clip(mixed, -1.0, 1.0)
    pcm = np.rint(mixed * 32767.0).astype("<i2")
    metadata = {
        "clip_id": clip_id,
        "duration_ms": duration_ms,
        "seed": seed,
        "total_samples": total_samples,
    }
    return pcm, annotations, metadata


def generate_samples(
    synthesis_config_path: str | Path,
    output_dir: str | Path,
    clean: bool = True,
) -> dict[str, Any]:
    config_path = Path(synthesis_config_path).resolve()
    raw = read_json(config_path)
    if int(raw.get("sample_rate", 0)) != 16_000:
        fail(
            "E_SAMPLE_RATE",
            "synthesis sample_rate must be 16000",
            actual=raw.get("sample_rate"),
        )

    out = Path(output_dir).resolve()
    if clean and out.exists():
        shutil.rmtree(out)
    audio_dir = out / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    global_seed = int(raw["global_seed"])
    sample_rate = int(raw["sample_rate"])
    frame_ms = float(raw.get("frame_ms", 20.0))
    fade_ms = float(raw.get("fade_ms", 8.0))
    noise_cfg = dict(raw.get("noise", {}))
    sample_rows: list[dict[str, Any]] = []
    annotation_rows: list[dict[str, Any]] = []

    for clip_cfg in raw.get("clips", []):
        pcm, annotations, metadata = _synthesize_clip(
            clip_cfg, global_seed, sample_rate, noise_cfg, fade_ms
        )
        clip_id = metadata["clip_id"]
        pcm_path = audio_dir / f"{clip_id}.pcm"
        wav_path = audio_dir / f"{clip_id}.wav"
        pcm_bytes = np.ascontiguousarray(pcm, dtype="<i2").tobytes()
        write_bytes_atomic(pcm_path, pcm_bytes)
        with wave.open(str(wav_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_bytes)
        sample_rows.append(
            {
                "clip_id": clip_id,
                "sample_rate": sample_rate,
                "duration_ms": metadata["duration_ms"],
                "seed": metadata["seed"],
                "frame_ms": frame_ms,
                "channels": 1,
                "sample_width": 2,
                "audio_pcm": f"audio/{clip_id}.pcm",
                "audio_wav": f"audio/{clip_id}.wav",
                "pcm_sha256": sha256_bytes(pcm_bytes),
            }
        )
        annotation_rows.extend(annotations)

    sample_rows.sort(key=lambda row: row["clip_id"])
    annotation_rows.sort(
        key=lambda row: (row["clip_id"], row["start_ms"], row["end_ms"], row["segment_id"])
    )
    samples_bytes = b"".join(canonical_json_bytes(row) + b"\n" for row in sample_rows)
    annotations_bytes = b"".join(
        canonical_json_bytes(row) + b"\n" for row in annotation_rows
    )
    write_bytes_atomic(out / "samples.jsonl", samples_bytes)
    write_bytes_atomic(out / "annotations.jsonl", annotations_bytes)
    summary = {
        "sample_rate": sample_rate,
        "global_seed": global_seed,
        "clip_count": len(sample_rows),
        "segment_count": len(annotation_rows),
        "samples_sha256": sha256_bytes(samples_bytes),
        "annotations_sha256": sha256_bytes(annotations_bytes),
    }
    write_bytes_atomic(
        out / "generation_summary.json", canonical_json_bytes(summary) + b"\n"
    )
    return summary
