"""Deterministic multichannel PCM synthesis from rendered gain curves."""

from __future__ import annotations

import hashlib
import io
import math
import os
import tempfile
import wave
from pathlib import Path

import numpy as np

from .io_utils import stable_int


def _source_waveform(
    source_type: str,
    object_id: str,
    sample_count: int,
    sample_rate_hz: int,
    seed: int,
) -> np.ndarray:
    rng = np.random.default_rng(stable_int(seed, object_id, source_type))
    time_s = np.arange(sample_count, dtype=float) / sample_rate_hz
    phase = rng.uniform(0.0, 2.0 * math.pi)
    noise = rng.standard_normal(sample_count)

    if source_type == "arc":
        signal = (
            0.72 * noise
            + 0.22 * np.sin(2 * math.pi * 1180.0 * time_s + phase)
            + 0.11 * np.sin(2 * math.pi * 2360.0 * time_s + 0.7 * phase)
            + 0.06 * np.sin(2 * math.pi * 3550.0 * time_s + 1.2 * phase)
        )
    elif source_type == "pool_boil":
        brown = np.cumsum(noise)
        brown -= np.linspace(brown[0], brown[-1], sample_count)
        brown /= max(float(np.std(brown)), 1e-9)
        signal = (
            0.52 * np.sin(2 * math.pi * 82.0 * time_s + phase)
            + 0.25 * np.sin(2 * math.pi * 126.0 * time_s + 0.6 * phase)
            + 0.35 * brown
        )
    elif source_type == "droplet":
        pulse = 0.38 + 0.62 * np.maximum(
            0.0, np.sin(2 * math.pi * 5.1 * time_s + phase)
        ) ** 3
        signal = pulse * (
            0.35 * np.sin(2 * math.pi * 610.0 * time_s + phase)
            + 0.22 * np.sin(2 * math.pi * 1440.0 * time_s + 0.2 * phase)
            + 0.22 * noise
        )
    elif source_type == "spatter":
        signal = (
            0.55 * noise
            + 0.28 * np.sin(2 * math.pi * 1900.0 * time_s + phase)
            + 0.18 * np.sin(2 * math.pi * 3100.0 * time_s + 0.5 * phase)
        )
    else:
        raise ValueError(f"unknown source type: {source_type}")

    signal -= float(np.mean(signal))
    rms = float(np.sqrt(np.mean(signal * signal)))
    if rms > 1e-12:
        signal /= rms
    return signal


def _write_pcm_wav(data: np.ndarray, sample_rate_hz: int) -> bytes:
    pcm = np.clip(data, -1.0, 1.0)
    pcm_int16 = np.round(pcm * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(int(data.shape[1]))
        handle.setsampwidth(2)
        handle.setframerate(sample_rate_hz)
        handle.writeframes(pcm_int16.tobytes(order="C"))
    return buffer.getvalue()


def synthesize_segment_audio(
    segment: dict,
    analysis: dict,
    layout: dict,
    seed: int,
    output_dir: str | Path,
) -> tuple[str, dict]:
    """Render the whole segment and persist a content-addressed 8-channel WAV."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sample_rate = int(segment["sample_rate_hz"])
    samples_per_frame = int(segment["samples_per_frame"])
    frame_count = int(segment["frame_count"])
    channel_count = int(layout["channel_count"])
    sample_count = frame_count * samples_per_frame
    mix = np.zeros((sample_count, channel_count), dtype=float)

    object_curves = analysis["render_output"]["object_gain_curves"]
    for source_object in analysis["source_objects"]:
        object_id = source_object["object_id"]
        source_type = source_object["source_type"]
        gain_entries = object_curves[object_id]
        if not gain_entries:
            continue
        start_frame = int(source_object["valid_from_frame"])
        end_frame = int(source_object["valid_to_frame"])
        active_frames = end_frame - start_frame + 1
        block_samples = active_frames * samples_per_frame
        source_signal = _source_waveform(
            source_type=source_type,
            object_id=object_id,
            sample_count=block_samples,
            sample_rate_hz=sample_rate,
            seed=seed,
        )

        # Fade object edges to avoid sample-boundary clicks.
        fade_samples = min(int(sample_rate * 0.004), block_samples // 4)
        if fade_samples > 0:
            fade = np.ones(block_samples, dtype=float)
            rise = 0.5 - 0.5 * np.cos(
                np.linspace(0.0, math.pi, fade_samples, dtype=float)
            )
            fade[:fade_samples] = rise
            fade[-fade_samples:] = rise[::-1]
            source_signal *= fade

        amplitudes = np.asarray(
            [entry["channel_amplitudes"] for entry in gain_entries],
            dtype=float,
        )
        if amplitudes.shape[0] != active_frames:
            raise ValueError(
                f"gain curve length mismatch for {object_id}: "
                f"{amplitudes.shape[0]} != {active_frames}"
            )
        sample_positions = np.arange(block_samples, dtype=float)
        frame_knots = np.arange(active_frames, dtype=float) * samples_per_frame
        envelope = np.empty((block_samples, channel_count), dtype=float)
        for channel in range(channel_count):
            envelope[:, channel] = np.interp(
                sample_positions,
                frame_knots,
                amplitudes[:, channel],
            )
        start_sample = start_frame * samples_per_frame
        stop_sample = start_sample + block_samples
        mix[start_sample:stop_sample] += source_signal[:, None] * envelope

    peak_before_master = float(np.max(np.abs(mix))) if mix.size else 0.0
    master_scale = 1.0
    if peak_before_master > 0.92:
        master_scale = 0.92 / peak_before_master
    mastered = mix * master_scale
    wav_bytes = _write_pcm_wav(mastered, sample_rate)
    sha256 = hashlib.sha256(wav_bytes).hexdigest()
    filename = f"{sha256}.wav"
    final_path = output_dir / filename
    if not final_path.exists():
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{filename}.", suffix=".tmp", dir=output_dir
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(wav_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, final_path)
            except FileExistsError:
                pass
        finally:
            if temporary.exists():
                temporary.unlink()

    try:
        relative_path = final_path.relative_to(output_dir.parent.parent)
    except ValueError:
        relative_path = final_path
    metadata = {
        "path": str(relative_path),
        "sha256": sha256,
        "format": "PCM16 WAV",
        "sample_rate_hz": sample_rate,
        "channels": channel_count,
        "channel_order": [
            channel["name"] for channel in layout["channels"]
        ],
        "samples": sample_count,
        "duration_s": sample_count / sample_rate,
        "samples_per_frame": samples_per_frame,
        "peak_before_master": peak_before_master,
        "master_scale": master_scale,
        "energy_note": (
            "Energy-conservation checks use pre-master analytic source "
            "amplitude gains; master_scale is applied once to prevent PCM clipping."
        ),
    }
    return str(final_path), metadata
