from __future__ import annotations

import json
import wave
from pathlib import Path
from typing import Any

import numpy as np


def write_wav(
    path: Path,
    samples: np.ndarray,
    sample_rate: int,
    *,
    channels: int = 1,
    sample_width: int = 2,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if sample_width == 2:
        clipped = np.clip(samples, -1.0, 1.0 - 1.0 / 32768.0)
        pcm = np.rint(clipped * 32767.0).astype("<i2")
        payload = pcm.tobytes()
    elif sample_width == 1:
        pcm = np.rint(np.clip(samples, -1.0, 1.0) * 127.0).astype(np.int8)
        payload = pcm.tobytes()
    else:
        payload = samples.astype("<i2").tobytes()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(sample_rate)
        handle.writeframes(payload)


def make_task(
    path: Path,
    *,
    references: list[str],
    queries: list[str],
    fingerprint: dict[str, Any] | None = None,
    search: dict[str, Any] | None = None,
    concurrency_limit: int = 2,
    memory_budget_bytes: int = 32 * 1024 * 1024,
    chunk_frames: int = 32,
    state_dir: str = "state",
) -> dict[str, Any]:
    task = {
        "schema_version": 1,
        "reference_files": references,
        "query_files": queries,
        "fingerprint": {
            "frame_length": 512,
            "hop_length": 128,
            "band_boundaries_hz": [50, 250, 600, 1200, 2400, 4800, 7000],
            "peak_neighborhood": 3,
            "peaks_per_frame": 4,
            "fan_out_frames": 6,
            "frequency_bucket_hz": 50.0,
            "pair_limit_per_peak": 12,
            "minimum_peak_hz": 50.0,
        },
        "search": {
            "vote_window_frames": 12,
            "min_matches": 20,
            "score_tolerance": 0.01,
            "time_offset_tolerance_samples": 256.0,
            "fixed_scale": 1_000_000,
            "relative_score_floor": 0.0,
        },
        "concurrency_limit": concurrency_limit,
        "memory_budget_bytes": memory_budget_bytes,
        "chunk_frames": chunk_frames,
        "state_dir": state_dir,
        "report_path": "report.json",
        "segments_path": "segments.json",
    }
    if fingerprint:
        task["fingerprint"].update(fingerprint)
    if search:
        task["search"].update(search)
    path.write_text(
        json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return task

