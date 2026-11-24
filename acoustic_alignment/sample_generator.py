"""Deterministic local multi-track WAV and measurement-task generator."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from scipy import signal

from .task_model import ms_to_samples


SAMPLE_RATES = (16000, 44100, 48000)
SWEEP_START_MS = 300
SWEEP_DURATION_MS = 1200
SWEEP_END_MS = SWEEP_START_MS + SWEEP_DURATION_MS
PULSE_MS = 1750
TOTAL_MS = 2000


def write_wav(path: str | Path, audio: np.ndarray, sample_rate: int, bit_depth: int = 16) -> None:
    import wave

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim == 1:
        audio = audio[:, None]
    channels = int(audio.shape[1])
    limited = np.clip(audio, -1.0, 1.0)

    if bit_depth == 16:
        integers = np.rint(limited * 32767.0).astype("<i2")
        raw = integers.tobytes()
        sample_width = 2
    elif bit_depth == 24:
        integers = np.rint(limited * 8388607.0).astype(np.int32)
        packed = np.empty((integers.size, 3), dtype=np.uint8)
        flat = integers.reshape(-1)
        packed[:, 0] = flat & 0xFF
        packed[:, 1] = (flat >> 8) & 0xFF
        packed[:, 2] = (flat >> 16) & 0xFF
        raw = packed.tobytes()
        sample_width = 3
    elif bit_depth == 8:
        integers = np.rint(np.clip((limited + 1.0) * 127.5, 0, 255)).astype(np.uint8)
        raw = integers.tobytes()
        sample_width = 1
    else:
        raise ValueError(f"unsupported deterministic fixture bit depth: {bit_depth}")

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(raw)


def fractional_delay(values: np.ndarray, delay_samples: float) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if delay_samples == 0:
        return values.copy()
    spectrum = np.fft.rfft(values)
    frequencies = np.fft.rfftfreq(values.size, d=1.0)
    delayed = spectrum * np.exp(-2j * np.pi * frequencies * float(delay_samples))
    return np.fft.irfft(delayed, n=values.size).astype(np.float64)


def make_chirp(sample_rate: int, rng: np.random.Generator, high_band_extra_delay: float = 0.0) -> np.ndarray:
    sweep_samples = ms_to_samples(SWEEP_DURATION_MS, sample_rate)
    time_axis = np.arange(sweep_samples, dtype=np.float64) / sample_rate
    chirp = signal.chirp(time_axis, f0=220.0, t1=SWEEP_DURATION_MS / 1000.0, f1=6400.0, method="logarithmic")
    fade_samples = max(1, ms_to_samples(10, sample_rate))
    fade_in = np.linspace(0.0, 1.0, fade_samples, dtype=np.float64)
    fade_out = np.linspace(1.0, 0.0, fade_samples, dtype=np.float64)
    chirp[:fade_samples] *= fade_in
    chirp[-fade_samples:] *= fade_out

    if high_band_extra_delay:
        spectrum = np.fft.rfft(chirp)
        frequencies = np.fft.rfftfreq(chirp.size, d=1.0 / sample_rate)
        mask = (frequencies >= 2600.0) & (frequencies <= 6400.0)
        spectrum[mask] *= 0.03 * np.exp(
            -2j * np.pi * frequencies[mask] * high_band_extra_delay / sample_rate
        )
        chirp = np.fft.irfft(spectrum, n=chirp.size)

    chirp *= 0.42
    chirp += rng.normal(0.0, 0.0025, size=chirp.size)
    return chirp


def make_track_audio(
    sample_rate: int,
    seed: int,
    delay_at_48k: float = 0.0,
    channels: int = 1,
    silent: bool = False,
    phase_bad: bool = False,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    total_samples = ms_to_samples(TOTAL_MS, sample_rate)
    audio = np.zeros(total_samples, dtype=np.float64)
    noise_amplitude = 1.2e-5 if silent else 0.0032
    audio += rng.normal(0.0, noise_amplitude, size=total_samples)

    if not silent:
        chirp = make_chirp(sample_rate, rng, high_band_extra_delay=3.0 if phase_bad else 0.0)
        native_delay = delay_at_48k * sample_rate / 48000.0
        chirp = fractional_delay(chirp, native_delay)
        start = ms_to_samples(SWEEP_START_MS, sample_rate)
        end = min(total_samples, start + chirp.size)
        audio[start:end] += chirp[: end - start]

        pulse_index = ms_to_samples(PULSE_MS, sample_rate)
        pulse_length = min(sample_rate // 200, total_samples - pulse_index)
        if pulse_length > 0:
            decay = np.exp(-np.arange(pulse_length) / max(1.0, sample_rate * 0.004))
            audio[pulse_index : pulse_index + pulse_length] += 0.48 * decay

    audio = np.clip(audio, -0.98, 0.98)
    if channels == 2:
        right = audio * 0.965
        right += rng.normal(0.0, 0.0003, size=right.size)
        audio = np.column_stack([audio, right])
    return audio


def _track_entry(
    track_id: str,
    filename: str,
    sample_rate: int,
    bit_depth: int,
    channels: int,
    expected_start_ms: int = SWEEP_START_MS,
    is_reference: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "track_id": track_id,
        "path": f"wav/{filename}",
        "enabled": True,
        "role": "reference" if is_reference else "candidate",
        "sample_rate": sample_rate,
        "bit_depth": bit_depth,
        "channels": channels,
        "expected_start_ms": expected_start_ms,
    }
    entry.update(extra)
    return entry


def generate_sample_set(output_dir: str | Path, seed: int = 380) -> tuple[Path, Path]:
    root = Path(output_dir)
    wav_dir = root / "wav"
    wav_dir.mkdir(parents=True, exist_ok=True)

    tracks: list[dict[str, Any]] = []

    ref_audio = make_track_audio(48000, seed + 1, delay_at_48k=0.0)
    write_wav(wav_dir / "ref_main_24_mono.wav", ref_audio, 48000, 24)
    tracks.append(_track_entry("ref_main", "ref_main_24_mono.wav", 48000, 24, 1, is_reference=True))

    delay_audio = make_track_audio(48000, seed + 2, delay_at_48k=3.0, channels=2)
    write_wav(wav_dir / "cand_delay3_16_stereo.wav", delay_audio, 48000, 16)
    tracks.append(_track_entry("cand_delay3", "cand_delay3_16_stereo.wav", 48000, 16, 2))

    rate44_audio = make_track_audio(44100, seed + 3, delay_at_48k=4.0, channels=2)
    write_wav(wav_dir / "cand_44100_24_stereo.wav", rate44_audio, 44100, 24)
    tracks.append(_track_entry("cand_44100", "cand_44100_24_stereo.wav", 44100, 24, 2))

    rate16_audio = make_track_audio(16000, seed + 4, delay_at_48k=6.0)
    write_wav(wav_dir / "cand_16k_16_mono.wav", rate16_audio, 16000, 16)
    tracks.append(_track_entry("cand_16k", "cand_16k_16_mono.wav", 16000, 16, 1))

    fractional_audio = make_track_audio(48000, seed + 5, delay_at_48k=4.4)
    write_wav(wav_dir / "cand_fractional_delay.wav", fractional_audio, 48000, 24)
    tracks.append(_track_entry("cand_fractional", "cand_fractional_delay.wav", 48000, 24, 1))

    phase_bad_audio = make_track_audio(48000, seed + 6, delay_at_48k=5.0, phase_bad=True)
    write_wav(wav_dir / "cand_phase_bad.wav", phase_bad_audio, 48000, 24)
    tracks.append(_track_entry("cand_phase_bad", "cand_phase_bad.wav", 48000, 24, 1))

    silent_audio = make_track_audio(48000, seed + 7, silent=True)
    write_wav(wav_dir / "cand_peak_silent.wav", silent_audio, 48000, 16)
    tracks.append(_track_entry("cand_peak_silent", "cand_peak_silent.wav", 48000, 16, 1))

    bad_rate_audio = make_track_audio(32000, seed + 8, delay_at_48k=2.0)
    write_wav(wav_dir / "bad_rate_32k.wav", bad_rate_audio, 32000, 16)
    tracks.append(_track_entry("bad_rate", "bad_rate_32k.wav", 48000, 16, 1))

    bad_bits_audio = make_track_audio(48000, seed + 9, delay_at_48k=1.0)
    write_wav(wav_dir / "bad_bits_8bit.wav", bad_bits_audio, 48000, 8)
    tracks.append(_track_entry("bad_bits", "bad_bits_8bit.wav", 48000, 8, 1))

    bad_channels_audio = make_track_audio(48000, seed + 10, delay_at_48k=2.0, channels=2)
    write_wav(wav_dir / "bad_channels_actual_stereo.wav", bad_channels_audio, 48000, 16)
    tracks.append(_track_entry("bad_channels", "bad_channels_actual_stereo.wav", 48000, 16, 1))

    tracks.append(
        _track_entry(
            "missing_file",
            "does_not_exist.wav",
            48000,
            24,
            1,
        )
    )
    tracks.append(
        {
            "track_id": "bad_start",
            "path": "wav/ref_main_24_mono.wav",
            "enabled": True,
            "role": "candidate",
            "sample_rate": 48000,
            "bit_depth": 24,
            "channels": 1,
            "expected_start_ms": -7,
        }
    )

    task: dict[str, Any] = {
        "schema_version": "1.0",
        "task_id": f"gsb-deterministic-{seed}",
        "seed": seed,
        "reference_track": "ref_main",
        "allowed_sample_rates": list(SAMPLE_RATES),
        "allowed_bit_depths": [16, 24],
        "sweep_start_ms": SWEEP_START_MS,
        "sweep_end_ms": SWEEP_END_MS,
        "silence_start_ms": 0,
        "silence_end_ms": SWEEP_START_MS,
        "search_window_ms": 2.0,
        "phase_unwrap_limit": 100.0,
        "fixed_point_scale": 1_000_000,
        "delay_tolerance_samples": 0.25,
        "phase_tolerance_samples": 0.75,
        "noise_gate_factor": 6.0,
        "correlation_threshold": 0.18,
        "phase_energy_floor": 0.02,
        "concurrency": 3,
        "memory_budget_bytes": 64 * 1024 * 1024,
        "bands": [
            {"band_id": "low_180_850", "band_index": 0, "min_hz": 180.0, "max_hz": 850.0},
            {"band_id": "mid_850_2600", "band_index": 1, "min_hz": 850.0, "max_hz": 2600.0},
            {"band_id": "high_2600_6400", "band_index": 2, "min_hz": 2600.0, "max_hz": 6400.0},
        ],
        "tracks": tracks,
    }

    task_path = root / "measurement_task.json"
    task_path.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return root, task_path


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="samples", help="sample output directory")
    parser.add_argument("--seed", type=int, default=380)
    args = parser.parse_args()
    root, task_path = generate_sample_set(args.out, args.seed)
    print(json.dumps({"sample_dir": str(root), "task": str(task_path), "seed": args.seed}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
