from __future__ import annotations

from typing import Any

import numpy as np

from .config import FingerprintParams
from .util import sha256_bytes, sha256_value
from .wavio import WavInfo, audio_memmap, pcm_bytes_for_range


def frame_count(sample_count: int, frame_length: int, hop_length: int) -> int:
    if sample_count < frame_length:
        return 0
    return 1 + (sample_count - frame_length) // hop_length


def chunk_audio_range(
    info: WavInfo,
    fingerprint: FingerprintParams,
    start_frame: int,
    end_frame: int,
) -> tuple[int, int]:
    total_frames = frame_count(info.sample_count, fingerprint.frame_length, fingerprint.hop_length)
    if start_frame >= total_frames or end_frame <= start_frame:
        return (0, 0)
    first_sample = start_frame * fingerprint.hop_length
    context_last = min(total_frames, end_frame + fingerprint.fan_out_frames) - 1
    last_sample = context_last * fingerprint.hop_length + fingerprint.frame_length
    return (max(0, first_sample), min(info.sample_count, last_sample))


def fingerprint_chunk_version(
    info: WavInfo,
    fingerprint: FingerprintParams,
    start_frame: int,
    end_frame: int,
) -> str:
    sample_start, sample_end = chunk_audio_range(info, fingerprint, start_frame, end_frame)
    audio_sha = sha256_bytes(pcm_bytes_for_range(info, sample_start, sample_end))
    return sha256_value(
        {
            "kind": "fingerprint_chunk_v1",
            "track": info.label,
            "sample_rate": info.sample_rate,
            "frame_interval": [start_frame, end_frame],
            "sample_interval": [sample_start, sample_end],
            "audio_chunk_sha256": audio_sha,
            "fingerprint": fingerprint.canonical(),
        }
    )


def _band_bin_indices(
    frequencies: np.ndarray, band_boundaries: tuple[float, ...]
) -> tuple[list[np.ndarray], np.ndarray]:
    band_bins: list[np.ndarray] = []
    band_for_bin = np.full(len(frequencies), -1, dtype=np.int32)
    last_band = len(band_boundaries) - 2
    for band_index in range(last_band + 1):
        low = band_boundaries[band_index]
        high = band_boundaries[band_index + 1]
        if band_index == last_band:
            indices = np.where((frequencies >= low) & (frequencies <= high))[0]
        else:
            indices = np.where((frequencies >= low) & (frequencies < high))[0]
        indices = indices[indices > 0]
        indices = indices[indices < len(frequencies) - 1]
        band_bins.append(indices)
        band_for_bin[indices] = band_index
    return band_bins, band_for_bin


def pick_peaks(
    magnitude: np.ndarray,
    frequencies: np.ndarray,
    band_bins: list[np.ndarray],
    band_for_bin: np.ndarray,
    fingerprint: FingerprintParams,
) -> list[dict[str, Any]]:
    radius = fingerprint.peak_neighborhood
    candidates: list[tuple[float, int, int]] = []
    bin_count = len(magnitude)
    for band_index, indices in enumerate(band_bins):
        for bin_index in indices:
            if frequencies[bin_index] < fingerprint.minimum_peak_hz:
                continue
            left = max(1, int(bin_index) - radius)
            right = min(bin_count - 1, int(bin_index) + radius + 1)
            value = float(magnitude[bin_index])
            if value <= 0.0:
                continue
            local = magnitude[left:right]
            if value < float(np.max(local)):
                continue
            prior = magnitude[left:bin_index]
            if prior.size and value <= float(np.max(prior)):
                continue
            candidates.append((value, int(bin_index), int(band_index)))
    candidates.sort(key=lambda item: (-item[0], frequencies[item[1]], item[1]))
    selected = candidates[: fingerprint.peaks_per_frame]
    ranked = sorted(selected, key=lambda item: (-item[0], item[1]))
    rank_by_bin = {item[1]: rank for rank, item in enumerate(ranked)}
    selected.sort(key=lambda item: (frequencies[item[1]], item[1]))
    peaks: list[dict[str, Any]] = []
    for strength, bin_index, band_index in selected:
        peaks.append(
            {
                "bin": int(bin_index),
                "band": int(band_index),
                "hz": float(frequencies[bin_index]),
                "strength": float(strength),
                "rank": int(rank_by_bin[bin_index]),
            }
        )
    return peaks


def _pair_fingerprint_items(
    peaks_by_frame: list[list[dict[str, Any]]],
    *,
    block_start: int,
    block_end: int,
    fingerprint: FingerprintParams,
    sample_rate: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    bucket = fingerprint.frequency_bucket_hz
    hop = fingerprint.hop_length
    frame_length = fingerprint.frame_length
    fan_out = fingerprint.fan_out_frames
    pair_limit = fingerprint.pair_limit_per_peak

    for absolute_frame in range(block_start, block_end):
        local_anchor = absolute_frame - block_start
        anchor_peaks = peaks_by_frame[local_anchor]
        if not anchor_peaks:
            continue
        for anchor in anchor_peaks:
            candidates: list[tuple[int, int, dict[str, Any]]] = []
            for delta in range(1, fan_out + 1):
                target_local = local_anchor + delta
                if target_local >= len(peaks_by_frame):
                    break
                for target in peaks_by_frame[target_local]:
                    candidates.append((delta, absolute_frame + delta, target))
            candidates.sort(
                key=lambda item: (item[0], item[2]["hz"], item[2]["bin"], item[1])
            )
            for delta, target_frame, target in candidates[:pair_limit]:
                delta_ms = max(1, int(round(delta * hop * 1000.0 / sample_rate)))
                anchor_bucket = int(round(float(anchor["hz"]) / bucket))
                target_bucket = int(round(float(target["hz"]) / bucket))
                strength_ratio_bucket = int(
                    round((float(target["strength"]) - float(anchor["strength"])) / 0.75)
                )
                key = (
                    f"{anchor['band']}:{target['band']}:{delta_ms}:"
                    f"{anchor_bucket}:{target_bucket}:{strength_ratio_bucket}:"
                    f"{anchor['rank']}:{target['rank']}"
                )
                items.append(
                    {
                        "key": key,
                        "band": int(anchor["band"]),
                        "target_band": int(target["band"]),
                        "delta_frames": int(delta),
                        "delta_ms": int(delta_ms),
                        "anchor_frame": int(absolute_frame),
                        "target_frame": int(target_frame),
                        "anchor_bin": int(anchor["bin"]),
                        "target_bin": int(target["bin"]),
                        "anchor_hz": float(anchor["hz"]),
                        "target_hz": float(target["hz"]),
                        "anchor_freq_bucket": int(anchor_bucket),
                        "target_freq_bucket": int(target_bucket),
                        "strength_ratio_bucket": int(strength_ratio_bucket),
                        "anchor_rank": int(anchor["rank"]),
                        "target_rank": int(target["rank"]),
                        "anchor_sample_start": int(absolute_frame * hop),
                        "anchor_sample_end": int(absolute_frame * hop + frame_length),
                        "target_sample_start": int(target_frame * hop),
                        "target_sample_end": int(target_frame * hop + frame_length),
                    }
                )
    return items


def extract_fingerprint_chunk(
    info: WavInfo,
    fingerprint: FingerprintParams,
    start_frame: int,
    end_frame: int,
) -> list[dict[str, Any]]:
    total_frames = frame_count(info.sample_count, fingerprint.frame_length, fingerprint.hop_length)
    start_frame = max(0, int(start_frame))
    end_frame = min(int(end_frame), total_frames)
    if start_frame >= end_frame:
        return []
    context_end = min(total_frames, end_frame + fingerprint.fan_out_frames)
    context_count = context_end - start_frame
    frame_length = fingerprint.frame_length
    hop = fingerprint.hop_length
    window = np.hanning(frame_length)
    if frame_length == 1:
        window = np.ones(1, dtype=np.float64)
    frequencies = np.fft.rfftfreq(frame_length, d=1.0 / info.sample_rate)
    bins = len(frequencies)
    magnitudes = np.empty((context_count, bins), dtype=np.float64)

    audio = audio_memmap(info)
    for relative_frame in range(context_count):
        absolute_frame = start_frame + relative_frame
        sample_start = absolute_frame * hop
        samples = np.asarray(
            audio[sample_start : sample_start + frame_length], dtype=np.float64
        )
        samples /= 32768.0
        if len(samples) != frame_length:
            raise RuntimeError("内部错误：分帧越界")
        magnitudes[relative_frame] = np.log1p(
            np.abs(np.fft.rfft(samples * window, n=frame_length))
        )

    band_bins, band_for_bin = _band_bin_indices(
        frequencies, fingerprint.band_boundaries_hz
    )
    peaks_by_frame = [
        pick_peaks(
            magnitudes[index],
            frequencies,
            band_bins,
            band_for_bin,
            fingerprint,
        )
        for index in range(context_count)
    ]
    items = _pair_fingerprint_items(
        peaks_by_frame,
        block_start=start_frame,
        block_end=end_frame,
        fingerprint=fingerprint,
        sample_rate=info.sample_rate,
    )
    items.sort(
        key=lambda item: (
            item["anchor_frame"],
            item["band"],
            item["delta_frames"],
            item["target_band"],
            item["anchor_bin"],
            item["target_bin"],
            item["target_frame"],
        )
    )
    return items
