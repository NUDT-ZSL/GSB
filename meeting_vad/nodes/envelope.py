"""Node 1: deterministic framed energy and zero-crossing envelope."""

from __future__ import annotations

from typing import Any

import numpy as np

from ..config import AppConfig
from ..errors import fail
from ..models import AudioInfo
from ..util import quantize_list, sha256_value


NODE_VERSION = "envelope-v1"


def _centered_moving_average(values: np.ndarray, window: int) -> np.ndarray:
    if values.size == 0:
        return values.copy()
    left = window // 2
    right = window - 1 - left
    padded = np.pad(values, (left, right), mode="edge")
    kernel = np.full(window, 1.0 / window, dtype=np.float64)
    return np.convolve(padded, kernel, mode="valid")


def _frame_count(total_samples: int, hop_samples: int) -> int:
    return 0 if total_samples <= 0 else (total_samples + hop_samples - 1) // hop_samples


def compute_envelope(
    audio_by_clip: dict[str, AudioInfo], config: AppConfig
) -> dict[str, Any]:
    frame_samples = max(1, int(round(config.sample_rate * config.frame_ms / 1000.0)))
    hop_samples = max(1, int(round(config.sample_rate * config.frame_hop_ms / 1000.0)))
    if hop_samples > frame_samples:
        fail(
            "E_FRAME_SHIFT",
            "frame hop must not exceed frame length",
            frame_samples=frame_samples,
            hop_samples=hop_samples,
        )
    smoothing_window = config.smoothing.window_frames
    clips_payload: dict[str, Any] = {}
    total_frames = 0

    for clip_id in sorted(audio_by_clip):
        info = audio_by_clip[clip_id]
        clip = info.clip
        expected_samples = clip.expected_samples
        actual_samples = info.total_samples
        expected_frames = _frame_count(expected_samples, hop_samples)
        actual_frames = _frame_count(actual_samples, hop_samples)
        if actual_samples != expected_samples or actual_frames != expected_frames:
            fail(
                "E_FRAME_MISMATCH",
                "PCM frame count does not match duration_ms",
                clip_id=clip_id,
                expected_frame_count=expected_frames,
                actual_frame_count=actual_frames,
                expected_samples=expected_samples,
                actual_samples=actual_samples,
                hop_samples=hop_samples,
            )
        total_frames += actual_frames
        if total_frames > config.resources.max_envelope_frames:
            fail(
                "E_RESOURCE_LIMIT",
                "resident envelope frame limit exceeded",
                resource="max_envelope_frames",
                limit=config.resources.max_envelope_frames,
                clip_id=clip_id,
                frame_count=total_frames,
            )

        starts = np.arange(0, actual_samples, hop_samples, dtype=np.int64)
        frame_count = int(starts.size)
        ends = np.minimum(starts + frame_samples, actual_samples).astype(np.int64)
        valid = (ends - starts).astype(np.int64)
        energy = np.zeros(frame_count, dtype=np.float64)
        zcr = np.zeros(frame_count, dtype=np.float64)
        pcm = info.samples.astype(np.float64) / 32768.0

        for index, (start, end, valid_count) in enumerate(
            zip(starts.tolist(), ends.tolist(), valid.tolist())
        ):
            frame = pcm[start:end]
            if frame.size:
                energy[index] = float(np.dot(frame, frame) / float(frame_samples))
            if valid_count > 1:
                signs = np.signbit(frame)
                crossings = int(np.count_nonzero(signs[1:] != signs[:-1]))
                zcr[index] = crossings / float(valid_count - 1)

        energy_smooth = _centered_moving_average(energy, smoothing_window)
        zcr_smooth = _centered_moving_average(zcr, smoothing_window)
        energy_values = quantize_list(energy_smooth.tolist())
        zcr_values = quantize_list(zcr_smooth.tolist())
        curve_hash = sha256_value(
            {
                "frame_start_samples": starts.tolist(),
                "frame_end_samples": ends.tolist(),
                "energy": energy_values,
                "zcr": zcr_values,
            }
        )
        clips_payload[clip_id] = {
            "clip_id": clip_id,
            "sample_rate": config.sample_rate,
            "duration_ms": clip.duration_ms,
            "total_samples": actual_samples,
            "frame_ms": config.frame_ms,
            "frame_hop_ms": config.frame_hop_ms,
            "samples_per_frame": frame_samples,
            "hop_samples": hop_samples,
            "frame_count": frame_count,
            "expected_frame_count": expected_frames,
            "frame_start_samples": starts.tolist(),
            "frame_end_samples": ends.tolist(),
            "valid_samples": valid.tolist(),
            "energy": energy_values,
            "zcr": zcr_values,
            "curve_sha256": curve_hash,
        }

    return {
        "schema": NODE_VERSION,
        "clip_order": sorted(audio_by_clip),
        "total_frame_count": total_frames,
        "clips": clips_payload,
    }


def envelope_key_details(
    audio_by_clip: dict[str, AudioInfo], config: AppConfig
) -> dict[str, Any]:
    frame_samples = max(1, int(round(config.sample_rate * config.frame_ms / 1000.0)))
    hop_samples = max(1, int(round(config.sample_rate * config.frame_hop_ms / 1000.0)))
    audio_rows: dict[str, Any] = {}
    for clip_id in sorted(audio_by_clip):
        info = audio_by_clip[clip_id]
        audio_rows[clip_id] = {
            "pcm_sha256": info.pcm_sha256,
            "chunk_sample_count": info.chunk_sample_count,
            "chunk_hashes": list(info.chunk_hashes),
            "chunk_manifest_hash": info.chunk_manifest_hash,
            "total_samples": info.total_samples,
            "manifest_duration_ms": info.clip.duration_ms,
            "manifest_seed": info.clip.seed,
        }
    return {
        "audio": audio_rows,
        "frame": {
            "sample_rate": config.sample_rate,
            "frame_ms": config.frame_ms,
            "frame_hop_ms": config.frame_hop_ms,
            "samples_per_frame": frame_samples,
            "hop_samples": hop_samples,
        },
        "smoothing": {
            "method": config.smoothing.method,
            "window_frames": config.smoothing.window_frames,
        },
    }
