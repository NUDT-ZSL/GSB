"""Deterministic local sample synthesis.

Clips are 16 kHz mono PCM generated purely from a fixed seed: alternating
speech-activity and silence spans are concatenated and stationary noise is
added. Per-frame ground truth (10 ms frames by default) and a JSONL manifest
are produced alongside. No external assets or network access are involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def frame_bounds(n_samples, sample_rate, frame_ms, hop_ms):
    """Yield (frame_index, sample_start, sample_end) with exact sample edges."""
    frame = sample_rate * frame_ms // 1000
    hop = sample_rate * hop_ms // 1000
    n_frames = (n_samples + hop - 1) // hop
    for i in range(n_frames):
        start = i * hop
        yield i, start, min(start + frame, n_samples)


def frame_truth(speech_mask, sample_rate, frame_ms, hop_ms):
    frames = []
    for i, start, end in frame_bounds(len(speech_mask), sample_rate, frame_ms, hop_ms):
        frac = float(np.mean(speech_mask[start:end])) if end > start else 0.0
        frames.append(
            {
                "frame_index": i,
                "is_speech": bool(frac >= 0.5),
                "sample_start": int(start),
                "sample_end": int(end),
            }
        )
    return frames


def synth_clip(
    clip_id,
    seed,
    duration_ms,
    sample_rate=16000,
    frame_ms=10,
    hop_ms=10,
    noise_level=0.01,
):
    """Return (pcm_int16 ndarray, truth frames) fully determined by ``seed``."""
    rng = np.random.default_rng(seed)
    n = duration_ms * sample_rate // 1000
    pcm = rng.normal(0.0, noise_level, n)
    speech_mask = np.zeros(n, dtype=bool)

    t = int(rng.uniform(0.10, 0.40) * sample_rate)
    while t < n:
        speech_len = int(rng.uniform(0.20, 0.70) * sample_rate)
        end = min(t + speech_len, n)
        if end - t >= int(0.05 * sample_rate):
            idx = np.arange(end - t, dtype=np.float64)
            f0 = float(rng.uniform(100.0, 300.0))
            env = 0.5 * (1.0 - np.cos(2.0 * np.pi * idx / max(1, len(idx) - 1)))
            sig = (
                env
                * 0.35
                * (
                    np.sin(2.0 * np.pi * f0 * idx / sample_rate)
                    + 0.5 * np.sin(2.0 * np.pi * 2.0 * f0 * idx / sample_rate)
                    + 0.25 * np.sin(2.0 * np.pi * 3.0 * f0 * idx / sample_rate)
                )
            )
            pcm[t:end] += sig
            speech_mask[t:end] = True
        t = end + int(rng.uniform(0.15, 0.60) * sample_rate)

    pcm_i16 = np.clip(pcm * 32767.0, -32768.0, 32767.0).astype(np.int16)
    truth = frame_truth(speech_mask, sample_rate, frame_ms, hop_ms)
    return pcm_i16, truth


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def read_jsonl(path):
    with Path(path).open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def synth_sample_set(samples_dir, clip_specs, frame_ms=10, hop_ms=10, noise_level=0.01):
    """Generate manifest.jsonl + truth/<clip_id>.jsonl; return clip dicts."""
    samples_dir = Path(samples_dir)
    samples_dir.mkdir(parents=True, exist_ok=True)
    manifest_rows = []
    clips = []
    for spec in clip_specs:
        pcm, truth = synth_clip(
            spec["clip_id"],
            spec["seed"],
            spec["duration_ms"],
            sample_rate=spec.get("sample_rate", 16000),
            frame_ms=frame_ms,
            hop_ms=hop_ms,
            noise_level=noise_level,
        )
        manifest_rows.append(
            {
                "clip_id": spec["clip_id"],
                "seed": spec["seed"],
                "duration_ms": spec["duration_ms"],
                "sample_rate": spec.get("sample_rate", 16000),
                "frame_ms": frame_ms,
            }
        )
        write_jsonl(samples_dir / "truth" / f"{spec['clip_id']}.jsonl", truth)
        clips.append(
            {
                "clip_id": spec["clip_id"],
                "seed": spec["seed"],
                "duration_ms": spec["duration_ms"],
                "sample_rate": spec.get("sample_rate", 16000),
                "pcm": pcm,
                "truth": truth,
            }
        )
    write_jsonl(samples_dir / "manifest.jsonl", manifest_rows)
    return clips


def load_sample_set(samples_dir, frame_ms=10, hop_ms=10, noise_level=0.01):
    """Load a manifest and deterministically re-derive PCM + truth from seeds."""
    samples_dir = Path(samples_dir)
    clips = []
    for row in read_jsonl(samples_dir / "manifest.jsonl"):
        pcm, _ = synth_clip(
            row["clip_id"],
            row["seed"],
            row["duration_ms"],
            sample_rate=row["sample_rate"],
            frame_ms=row.get("frame_ms", frame_ms),
            hop_ms=hop_ms,
            noise_level=noise_level,
        )
        truth = read_jsonl(samples_dir / "truth" / f"{row['clip_id']}.jsonl")
        clips.append(
            {
                "clip_id": row["clip_id"],
                "seed": row["seed"],
                "duration_ms": row["duration_ms"],
                "sample_rate": row["sample_rate"],
                "pcm": pcm,
                "truth": truth,
            }
        )
    return clips
