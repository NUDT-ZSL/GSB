"""Impulse / click detection using robust two-sided differential envelopes."""

from __future__ import annotations

import math

import numpy as np

from .metrics import local_diff_reference
from .signal_utils import EPS


def _spectral_flatness(signal: np.ndarray, index: int, sample_rate: int) -> float:
    """Broadbandness of a short window; clicks are flat, music is tonal."""
    width = max(32, int(round(0.004 * sample_rate)))
    lo = max(0, int(index) - width // 2)
    hi = min(int(signal.size), lo + width)
    lo = max(0, hi - width)
    segment = np.asarray(signal[lo:hi], dtype=np.float64)
    if segment.size < 8:
        return 0.0
    segment = segment - float(np.mean(segment))
    window = np.hanning(segment.size)
    magnitude = np.abs(np.fft.rfft(segment * window))[1:] + EPS
    geometric = float(np.exp(np.mean(np.log(magnitude))))
    arithmetic = float(np.mean(magnitude))
    return geometric / max(arithmetic, EPS)


def smoothed_diff_energy(signal: np.ndarray, sample_rate: int) -> np.ndarray:
    if signal.size == 0:
        return np.zeros(0, dtype=np.float64)
    diff = np.diff(signal, prepend=signal[0])
    energy = diff * diff
    width = max(2, int(round(0.00025 * sample_rate)))
    if signal.size < width:
        return np.full(signal.shape, float(np.mean(energy)), dtype=np.float64)
    kernel = np.ones(width, dtype=np.float64) / float(width)
    return np.convolve(energy, kernel, mode="same")


def detect_impulses(
    signal: np.ndarray,
    sample_rate: int,
    suppress_mask: np.ndarray | None = None,
    ratio_threshold: float = 4.5,
    absolute_jump_floor: float = 0.09,
    flatness_threshold: float = 0.30,
    absolute_jump_flatness: float = 0.45,
    min_duration_ms: float = 0.30,
    max_duration_ms: float = 12.0,
    merge_gap_ms: float = 2.0,
) -> tuple[list[dict], np.ndarray, np.ndarray]:
    """Detect transient clicks.

    Returns ``(events, per_frame_ratio_hint, per_frame_duration_hint)``.  The
    frame hints are keyed by sample index and are sparse (zero except where a
    sample-level event exists).
    """
    n = int(signal.size)
    if n < 8:
        return [], np.zeros(n, dtype=np.float64), np.zeros(n, dtype=np.int32)

    e_s = smoothed_diff_energy(signal, sample_rate)
    abs_floor = 6.0e-5
    base = float(np.percentile(e_s, 50.0))
    candidate_floor = max(abs_floor, base * 1.2)

    left = e_s[:-2]
    center = e_s[1:-1]
    right = e_s[2:]
    local_max = np.flatnonzero((center > left) & (center >= right) & (center > candidate_floor)) + 1

    accepted: list[dict] = []
    for index in local_max:
        if suppress_mask is not None and bool(suppress_mask[index]):
            continue
        ref = local_diff_reference(e_s, int(index), sample_rate)
        energy = float(center[index - 1])
        ratio = math.sqrt(energy / max(ref, EPS))
        jump = math.sqrt(energy)
        flatness = _spectral_flatness(signal, int(index), sample_rate)
        relative_hit = ratio >= ratio_threshold and flatness >= flatness_threshold
        absolute_hit = jump >= absolute_jump_floor and flatness >= absolute_jump_flatness
        if relative_hit or absolute_hit:
            accepted.append(
                {
                    "index": int(index),
                    "ratio": float(ratio),
                    "ref": float(ref),
                    "energy": energy,
                    "flatness": float(flatness),
                }
            )

    if not accepted:
        return [], np.zeros(n, dtype=np.float64), np.zeros(n, dtype=np.int32)

    merge_gap = max(1, int(round(merge_gap_ms * sample_rate / 1000.0)))
    groups: list[list[dict]] = []
    for item in accepted:
        if groups and item["index"] - groups[-1][-1]["index"] <= merge_gap:
            groups[-1].append(item)
        else:
            groups.append([item])

    min_duration = max(1, int(round(min_duration_ms * sample_rate / 1000.0)))
    max_duration = max(min_duration, int(round(max_duration_ms * sample_rate / 1000.0)))
    min_samples = max(1, int(round(0.0002 * sample_rate)))

    events: list[dict] = []
    ratio_hint = np.zeros(n, dtype=np.float64)
    duration_hint = np.zeros(n, dtype=np.int32)
    for group in groups:
        winner = max(group, key=lambda item: item["ratio"])
        center_idx = winner["index"]
        ref = winner["ref"]
        threshold = max(abs_floor, ref * 3.0)

        start = center_idx
        travelled = 0
        while start > 0 and travelled < max_duration:
            if e_s[start - 1] >= threshold:
                start -= 1
                travelled += 1
            elif start > 1 and e_s[start - 2] >= threshold:
                start -= 2
                travelled += 2
            else:
                break
        end = center_idx + 1
        travelled = 0
        while end < n and travelled < max_duration:
            if e_s[end] >= threshold:
                end += 1
                travelled += 1
            elif end + 1 < n and e_s[end + 1] >= threshold:
                end += 2
                travelled += 2
            else:
                break
        if end - start < min_duration:
            end = min(n, start + min_duration)
        if end - start > max_duration:
            end = start + max_duration
        if end - start < min_samples:
            continue

        segment = signal[start:end]
        peak_amplitude = float(np.max(np.abs(segment))) if segment.size else 0.0
        local_floor = float(np.median(np.abs(signal[max(0, start - 32) : start]))) if start >= 2 else 0.0
        events.append(
            {
                "start_sample": int(start),
                "end_sample": int(end),
                "duration_samples": int(end - start),
                "peak_amplitude": peak_amplitude,
                "local_floor_amplitude": local_floor,
                "jump_ratio": float(max(item["ratio"] for item in group)),
                "jump_amplitude": math.sqrt(float(max(item["energy"] for item in group))),
                "spectral_flatness": float(max(item["flatness"] for item in group)),
                "center_sample": int(center_idx),
            }
        )
        ratio_hint[start:end] = max(ratio_hint[start:end].max(initial=0.0), winner["ratio"])
        duration_hint[start:end] = max(int(duration_hint[start:end].max(initial=0)), end - start)

    events.sort(key=lambda item: item["start_sample"])
    return events, ratio_hint, duration_hint


def assign_impulses_to_frames(frames: list[dict], events: list[dict], ratio_hint: np.ndarray, duration_hint: np.ndarray, offset: int) -> None:
    """Attach per-frame impulse evidence, using global sample coordinates."""
    for frame in frames:
        lo = int(frame["start_sample"]) - offset
        hi = int(frame["end_sample"]) - offset
        if hi <= lo:
            continue
        ratio_local = float(np.max(ratio_hint[lo:hi])) if hi <= ratio_hint.size else 0.0
        duration_local = int(np.max(duration_hint[lo:hi])) if hi <= duration_hint.size else 0
        if ratio_local > 0.0:
            frame["diff_jump_ratio"] = max(float(frame["diff_jump_ratio"]), ratio_local)
            frame["jump_duration_samples"] = max(int(frame["jump_duration_samples"]), duration_local)
        frame["impulse_candidate"] = any(
            int(event["start_sample"]) < hi and int(event["end_sample"]) > lo for event in events
        )
        if frame["impulse_candidate"]:
            overlapping = [
                event
                for event in events
                if int(event["start_sample"]) < hi and int(event["end_sample"]) > lo
            ]
            frame["impulse_flatness"] = max(
                float(event.get("spectral_flatness", 0.0)) for event in overlapping
            )
