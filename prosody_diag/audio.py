from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .config import Config
from .errors import E_FRAME_COUNT_MISMATCH, DiagnosticError


def expected_frame_count(sample_count: int, frame_length: int, frame_shift: int) -> int:
    if sample_count < frame_length:
        return 0
    return 1 + (sample_count - frame_length) // frame_shift


def rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by score descending, then the required deterministic tie keys."""

    return sorted(
        candidates,
        key=lambda item: (
            -float(item["score"]),
            int(item["frame_index"]),
            str(item["boundary_type"]),
        ),
    )


def _autocorrelation_f0(frame: np.ndarray, cfg: Config) -> float:
    if float(np.sqrt(np.mean(frame * frame))) < 0.012:
        return 0.0
    centered = frame - float(np.mean(frame))
    if not np.any(centered):
        return 0.0
    correlation = np.correlate(centered, centered, mode="full")[len(centered) - 1 :]
    min_lag = max(2, int(round(cfg.sample_rate / cfg.f0_max_hz)))
    max_lag = min(len(correlation) - 2, int(round(cfg.sample_rate / cfg.f0_min_hz)))
    if max_lag <= min_lag + 1:
        return 0.0
    search = correlation[min_lag : max_lag + 1]
    peak_offset = int(np.argmax(search))
    peak_lag = min_lag + peak_offset
    peak_value = float(correlation[peak_lag])
    if peak_value <= 0 or peak_value < 0.28 * float(correlation[0]):
        return 0.0
    if 0 < peak_lag < len(correlation) - 1:
        left = float(correlation[peak_lag - 1])
        center = float(correlation[peak_lag])
        right = float(correlation[peak_lag + 1])
        denominator = left - 2.0 * center + right
        shift = 0.5 * (left - right) / denominator if abs(denominator) > 1e-12 else 0.0
        shift = max(-1.0, min(1.0, shift))
        lag = float(peak_lag) + shift
    else:
        lag = float(peak_lag)
    if lag <= 0:
        return 0.0
    f0 = cfg.sample_rate / lag
    if not (cfg.f0_min_hz <= f0 <= cfg.f0_max_hz):
        return 0.0
    return f0


def _zcr(frame: np.ndarray) -> float:
    if len(frame) < 2:
        return 0.0
    crossings = np.count_nonzero(frame[:-1] * frame[1:] < 0.0)
    return float(crossings) / float(len(frame) - 1)


def _nearby_pause_ms(truth_by_frame: dict[int, dict[str, Any]], frame_index: int) -> int:
    for delta in range(-3, 4):
        row = truth_by_frame.get(frame_index + delta)
        if row and int(row.get("pause_label", 0)) == 1:
            return int(row.get("pause_duration_ms", 0))
    return 0


def extract_acoustic_record(
    task: dict[str, Any],
    pcm_path: str | Path,
    truth_rows: list[dict[str, Any]] | None,
    cfg: Config,
) -> dict[str, Any]:
    cfg.validate()
    path = Path(pcm_path)
    pcm_bytes = path.read_bytes()
    sample_width = 2
    absolute_start_sample = int(task["start_ms"]) * cfg.sample_rate // 1000
    absolute_end_sample = int(task["end_ms"]) * cfg.sample_rate // 1000
    sample_count = absolute_end_sample - absolute_start_sample
    expected_byte_span = sample_count * sample_width
    absolute_byte_start = absolute_start_sample * sample_width
    absolute_byte_end = absolute_end_sample * sample_width

    if absolute_start_sample < 0 or absolute_end_sample <= absolute_start_sample:
        raise DiagnosticError(
            E_FRAME_COUNT_MISMATCH,
            "task start_ms/end_ms imply an invalid sample span",
            field="tasks.end_ms",
            details={
                "utterance_id": task.get("utterance_id"),
                "start_ms": task.get("start_ms"),
                "end_ms": task.get("end_ms"),
            },
        )
    if len(pcm_bytes) < absolute_byte_end:
        raise DiagnosticError(
            E_FRAME_COUNT_MISMATCH,
            "PCM file is shorter than the absolute task interval",
            field=f"audio.{task.get('speaker_id')}.pcm",
            details={
                "utterance_id": task.get("utterance_id"),
                "required_bytes": absolute_byte_end,
                "actual_bytes": len(pcm_bytes),
            },
        )

    segment_bytes = pcm_bytes[absolute_byte_start:absolute_byte_end]
    if len(segment_bytes) != expected_byte_span:
        raise DiagnosticError(
            E_FRAME_COUNT_MISMATCH,
            "PCM segment byte span does not match start_ms/end_ms",
            field=f"audio.{task.get('speaker_id')}.pcm",
            details={
                "utterance_id": task.get("utterance_id"),
                "expected_bytes": expected_byte_span,
                "actual_bytes": len(segment_bytes),
            },
        )

    pcm = np.frombuffer(segment_bytes, dtype="<i2").astype(np.float64) / 32768.0
    frame_length = cfg.frame_length_samples
    frame_shift = cfg.frame_shift_samples
    frame_count = expected_frame_count(len(pcm), frame_length, frame_shift)
    if truth_rows is not None and len(truth_rows) != frame_count:
        raise DiagnosticError(
            E_FRAME_COUNT_MISMATCH,
            "frame truth count does not match exact sample-index frame count",
            field="frame_truth.frame_index",
            details={
                "utterance_id": task.get("utterance_id"),
                "expected_frame_count": frame_count,
                "truth_frame_count": len(truth_rows),
                "sample_count": len(pcm),
                "frame_length_samples": frame_length,
                "frame_shift_samples": frame_shift,
            },
        )

    truth_by_frame = {int(row["frame_index"]): row for row in (truth_rows or [])}
    frames: list[dict[str, Any]] = []
    energy = np.zeros(frame_count, dtype=np.float64)
    f0_candidate = np.zeros(frame_count, dtype=np.float64)
    zcr_values = np.zeros(frame_count, dtype=np.float64)

    for index in range(frame_count):
        start = index * frame_shift
        end = start + frame_length
        frame = pcm[start:end]
        energy_value = float(np.sqrt(np.mean(frame * frame, dtype=np.float64)))
        f0_value = _autocorrelation_f0(frame, cfg)
        zcr_value = _zcr(frame)
        energy[index] = energy_value
        f0_candidate[index] = f0_value
        zcr_values[index] = zcr_value
        truth = truth_by_frame.get(index, {})
        frames.append(
            {
                "utterance_id": task["utterance_id"],
                "frame_index": index,
                "start_sample": start,
                "end_sample": end,
                "start_ms": round(start * 1000.0 / cfg.sample_rate, 6),
                "end_ms": round(end * 1000.0 / cfg.sample_rate, 6),
                "energy_rms": round(energy_value, 9),
                "f0_candidate_hz": round(f0_value, 6),
                "zcr": round(zcr_value, 9),
                "f0_true": truth.get("f0_true"),
                "energy_true": truth.get("energy_true"),
                "pause_label": int(truth.get("pause_label", 0)),
                "pause_duration_ms": int(truth.get("pause_duration_ms", 0)),
            }
        )

    candidates = _build_candidates(
        task["utterance_id"], frames, energy, f0_candidate, zcr_values, truth_by_frame, cfg
    )
    ranked = rank_candidates(candidates)
    for rank, candidate in enumerate(ranked):
        candidate["score_rank"] = rank
    # Keep the persisted candidate list in frame/type order for stable ledger keys.
    candidates.sort(key=lambda item: (item["frame_index"], item["boundary_type"]))
    for index, candidate in enumerate(candidates):
        candidate["candidate_index"] = index
        candidate["candidate_id"] = (
            f"{task['utterance_id']}:F{int(candidate['frame_index']):04d}:"
            f"{candidate['boundary_type']}"
        )
    return {
        "utterance_id": task["utterance_id"],
        "speaker_id": task["speaker_id"],
        "sample_count": int(len(pcm)),
        "frame_count": frame_count,
        "frame_length_samples": frame_length,
        "frame_shift_samples": frame_shift,
        "pcm_segment_sha256": __import__("hashlib").sha256(segment_bytes).hexdigest(),
        "frames": frames,
        "candidates": candidates,
    }


def _build_candidates(
    utterance_id: str,
    frames: list[dict[str, Any]],
    energy: np.ndarray,
    f0_values: np.ndarray,
    zcr_values: np.ndarray,
    truth_by_frame: dict[int, dict[str, Any]],
    cfg: Config,
) -> list[dict[str, Any]]:
    del utterance_id
    count = len(frames)
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()

    def add(frame_index: int, boundary_type: str, score: float, pause_ms: int) -> None:
        key = (frame_index, boundary_type)
        if key in seen:
            return
        seen.add(key)
        idx = max(0, min(count - 1, frame_index))
        left_energy = float(energy[max(0, idx - 2)])
        right_energy = float(energy[min(count - 1, idx + 2)])
        reset_strength = max(0.0, left_energy - float(energy[idx])) + max(
            0.0, right_energy - float(energy[idx])
        )
        reset_norm = min(2.0, reset_strength / max(cfg.energy_scale, 1e-12))
        f0_left = float(f0_values[max(0, idx - 1)])
        f0_right = float(f0_values[min(count - 1, idx + 1)])
        f0_jump_norm = min(2.0, abs(f0_right - f0_left) / max(cfg.f0_scale_hz, 1e-12))
        candidate = {
            "frame_index": int(frame_index),
            "boundary_type": boundary_type,
            "score": round(float(score), 9),
            "pause_ms": int(pause_ms),
            "pause_norm_raw": min(1.5, pause_ms / cfg.pause_scale_ms),
            "energy_rms": frames[idx]["energy_rms"],
            "energy_norm_raw": min(3.0, float(energy[idx]) / cfg.energy_scale),
            "zcr": frames[idx]["zcr"],
            "zcr_norm_raw": min(3.0, float(zcr_values[idx]) / cfg.zcr_scale),
            "f0_candidate_hz": frames[idx]["f0_candidate_hz"],
            "f0_norm_raw": min(3.0, float(f0_values[idx]) / cfg.f0_scale_hz),
            "reset_norm_raw": round(reset_norm, 9),
            "f0_jump_norm_raw": round(f0_jump_norm, 9),
        }
        candidates.append(candidate)

    for i in range(count):
        pause_label = int(frames[i]["pause_label"])
        previous_pause = int(frames[i - 1]["pause_label"]) if i else 0
        next_pause = int(frames[i + 1]["pause_label"]) if i + 1 < count else 0
        if pause_label == 1 and previous_pause == 0:
            pause_ms = int(frames[i].get("pause_duration_ms", 0)) or _nearby_pause_ms(
                truth_by_frame, i
            )
            score = 0.62 * min(1.0, pause_ms / 320.0) + 0.18
            add(i, "pause_onset", score, pause_ms)
        if pause_label == 0 and previous_pause == 1:
            pause_ms = _nearby_pause_ms(truth_by_frame, i - 1)
            score = 0.42 * min(1.0, pause_ms / 320.0) + 0.12
            add(i, "pause_offset", score, pause_ms)

        if 2 <= i < count - 2:
            is_energy_min = energy[i] <= energy[i - 1] and energy[i] <= energy[i + 1]
            rebound = energy[i + 1] - energy[i]
            if (
                is_energy_min
                and energy[i - 1] > 0
                and energy[i] < 0.78 * energy[i - 1]
                and rebound > 0.22 * max(cfg.energy_scale, 1e-9)
            ):
                pause_ms = _nearby_pause_ms(truth_by_frame, i)
                score = 0.28 + min(0.32, rebound / cfg.energy_scale)
                add(i, "energy_reset", score, pause_ms)

        if (
            f0_values[i] > 0
            and i > 0
            and (f0_values[i - 1] <= 1.0 or f0_values[i] > 1.22 * max(1.0, f0_values[i - 1]))
        ):
            pause_ms = _nearby_pause_ms(truth_by_frame, i)
            score = 0.20 + min(0.22, abs(f0_values[i] - f0_values[i - 1]) / cfg.f0_scale_hz)
            add(i, "f0_reset", score, pause_ms)

        if (
            0 < i < count - 1
            and zcr_values[i] > 0.075
            and zcr_values[i] >= zcr_values[i - 1]
            and zcr_values[i] >= zcr_values[i + 1]
        ):
            score = 0.16 + min(0.18, zcr_values[i] / cfg.zcr_scale)
            add(i, "zcr_peak", score, 0)

    return candidates
