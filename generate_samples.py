from __future__ import annotations

import argparse
import json
import shutil
import wave
from pathlib import Path

import numpy as np


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(samples, -1.0, 1.0 - 1.0 / 32768.0)
    pcm = np.rint(clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


def _motif(seed: int, sample_rate: int, seconds: float) -> np.ndarray:
    rng = np.random.default_rng(seed)
    count = int(round(sample_rate * seconds))
    time = np.arange(count, dtype=np.float64) / sample_rate
    signal = np.zeros(count, dtype=np.float64)
    base_freqs = np.array(
        [
            146.8,
            174.6,
            196.0,
            233.1,
            261.6,
            311.1,
            349.2,
            392.0,
            466.2,
            523.3,
            622.3,
            698.5,
            784.0,
            932.3,
            1046.5,
            1244.5,
            1396.9,
            1568.0,
            1864.7,
            2093.0,
            2489.0,
            2793.8,
            3136.0,
            3729.3,
        ],
        dtype=np.float64,
    )
    cursor = int(0.05 * sample_rate)
    while cursor < count:
        note_seconds = float(rng.uniform(0.16, 0.31))
        gap_seconds = float(rng.uniform(0.005, 0.045))
        start = cursor
        end = min(count, start + int(note_seconds * sample_rate))
        if end <= start:
            break
        local_t = time[start:end] - time[start]
        fundamental = float(rng.choice(base_freqs)) * float(rng.uniform(0.995, 1.005))
        envelope_time = max(1, len(local_t))
        envelope = np.sin(
            np.pi * np.minimum(1.0, local_t / (envelope_time / sample_rate))
        ) ** 2
        second_gain = float(rng.uniform(0.13, 0.33))
        third_gain = float(rng.uniform(0.04, 0.16))
        detune = float(rng.uniform(0.997, 1.006))
        tone = (
            float(rng.uniform(0.48, 0.72))
            * np.sin(2 * np.pi * fundamental * local_t + float(rng.uniform(0, 6.28)))
            + second_gain
            * np.sin(
                2 * np.pi * fundamental * 2.0 * detune * local_t
                + float(rng.uniform(0, 6.28))
            )
            + third_gain
            * np.sin(
                2 * np.pi * fundamental * 3.0 * detune * local_t
                + float(rng.uniform(0, 6.28))
            )
        )
        signal[start:end] += envelope * tone
        cursor = end + int(gap_seconds * sample_rate)
    slow = 0.08 * np.sin(2 * np.pi * 0.7 * time + seed * 0.13)
    signal += slow
    signal += rng.normal(0.0, 0.0025, size=count)
    peak = float(np.max(np.abs(signal))) or 1.0
    return 0.82 * signal / peak


def _resample(signal: np.ndarray, factor: float) -> np.ndarray:
    if abs(factor - 1.0) < 1e-12:
        return signal.copy()
    source_positions = np.arange(len(signal), dtype=np.float64)
    target_count = max(1, int(round(len(signal) / factor)))
    target_positions = np.arange(target_count, dtype=np.float64) * factor
    return np.interp(target_positions, source_positions, signal, left=0.0, right=0.0)


def _with_ambient(
    signal: np.ndarray,
    *,
    sample_rate: int,
    lead_seconds: float,
    tail_seconds: float,
    gain: float,
    seed: int,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    lead = rng.normal(0.0, 0.003, size=int(lead_seconds * sample_rate))
    tail = rng.normal(0.0, 0.003, size=int(tail_seconds * sample_rate))
    body = signal * gain
    body += rng.normal(0.0, 0.004, size=len(body))
    return np.concatenate([lead, body, tail])


def _insert_noise(signal: np.ndarray, at: int, length: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    insertion = rng.normal(0.0, 0.015, size=length)
    return np.concatenate([signal[:at], insertion, signal[at:]])


def generate(output_dir: Path, *, seed: int = 20261125) -> dict:
    output_dir = Path(output_dir)
    audio_dir = output_dir / "audio"
    state_dir = output_dir / "state"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    audio_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)

    refs = {
        "ref_a": (48000, 10.0, seed + 101),
        "ref_b": (44100, 9.0, seed + 202),
        "ref_c": (16000, 8.5, seed + 303),
    }
    bases: dict[str, np.ndarray] = {}
    for name, (rate, duration, track_seed) in refs.items():
        signal = _motif(track_seed, rate, duration)
        bases[name] = signal
        _write_wav(audio_dir / f"{name}.wav", signal, rate)

    rate_a = refs["ref_a"][0]
    rate_b = refs["ref_b"][0]
    rate_c = refs["ref_c"][0]

    q1 = bases["ref_a"][int(2.0 * rate_a) : int(7.0 * rate_a)].copy()
    q1 = _insert_noise(q1, int(2.4 * rate_a), int(0.35 * rate_a), seed + 501)
    q1 = _with_ambient(
        q1, sample_rate=rate_a, lead_seconds=0.8, tail_seconds=1.1, gain=0.48, seed=seed + 502
    )
    _write_wav(audio_dir / "query_a_insert_noise.wav", q1, rate_a)

    q2 = _resample(bases["ref_b"][int(1.0 * rate_b) : int(6.0 * rate_b)], 0.985)
    q2 = _with_ambient(
        q2, sample_rate=rate_b, lead_seconds=0.55, tail_seconds=0.9, gain=0.66, seed=seed + 503
    )
    _write_wav(audio_dir / "query_b_speed_noise.wav", q2, rate_b)

    q3 = bases["ref_c"][int(0.5 * rate_c) : int(6.0 * rate_c)].copy()
    q3 = _with_ambient(
        q3, sample_rate=rate_c, lead_seconds=0.35, tail_seconds=0.7, gain=0.72, seed=seed + 504
    )
    _write_wav(audio_dir / "query_c_gain_noise.wav", q3, rate_c)

    rng = np.random.default_rng(seed + 999)
    unrelated = rng.normal(0.0, 0.03, size=int(3.0 * rate_a))
    _write_wav(audio_dir / "query_unrelated.wav", unrelated, rate_a)

    task = {
        "schema_version": 1,
        "reference_files": [
            "audio/ref_a.wav",
            "audio/ref_b.wav",
            "audio/ref_c.wav",
        ],
        "query_files": [
            "audio/query_a_insert_noise.wav",
            "audio/query_b_speed_noise.wav",
            "audio/query_c_gain_noise.wav",
            "audio/query_unrelated.wav",
        ],
        "fingerprint": {
            "frame_length": 2048,
            "hop_length": 512,
            "band_boundaries_hz": [0, 250, 500, 900, 1500, 2400, 4000, 6000, 7800],
            "peak_neighborhood": 3,
            "peaks_per_frame": 4,
            "fan_out_frames": 8,
            "frequency_bucket_hz": 50.0,
            "pair_limit_per_peak": 16,
        },
        "search": {
            "vote_window_frames": 20,
            "min_matches": 4500,
            "score_tolerance": 0.0005,
            "time_offset_tolerance_samples": 1024.0,
            "fixed_scale": 1000000,
            "relative_score_floor": 0.2,
        },
        "concurrency_limit": 4,
        "memory_budget_bytes": 33554432,
        "chunk_frames": 64,
        "state_dir": "state",
        "report_path": "report.json",
        "segments_path": "segments.json",
    }
    task_path = output_dir / "task.json"
    task_path.write_text(
        json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    manifest = {
        "seed": seed,
        "sample_rates": {name: value[0] for name, value in refs.items()},
        "expected": [
            {
                "query": "audio/query_a_insert_noise.wav",
                "reference": "audio/ref_a.wav",
                "reference_start_seconds": 2.0,
                "transform": ["gain", "local_insertion", "background_noise"],
            },
            {
                "query": "audio/query_b_speed_noise.wav",
                "reference": "audio/ref_b.wav",
                "reference_start_seconds": 1.0,
                "transform": ["speed_0.985", "gain", "background_noise"],
            },
            {
                "query": "audio/query_c_gain_noise.wav",
                "reference": "audio/ref_c.wav",
                "reference_start_seconds": 0.5,
                "transform": ["gain", "background_noise"],
            },
            {
                "query": "audio/query_unrelated.wav",
                "reference": None,
                "transform": ["unmatched_noise"],
            },
        ],
    }
    (output_dir / "sample_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="生成确定性的本地 WAV 检索样本")
    parser.add_argument("--out", default="sample_data", help="输出目录")
    parser.add_argument("--seed", type=int, default=20261125, help="固定随机种子")
    args = parser.parse_args()
    manifest = generate(Path(args.out), seed=args.seed)
    print(json.dumps({"ok": True, "output": str(Path(args.out)), "sample_count": 7, "manifest": manifest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
