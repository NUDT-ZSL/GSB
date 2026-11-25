"""Sample-exact, deterministic chunked short-time energy envelope estimation."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from .config import EngineConfig
from .errors import E_FORMAT_CORRUPT, EngineError
from .util import object_hash, quantize, sha256_bytes


def load_pcm(path: str | Path) -> tuple[np.ndarray, str, bytes]:
    raw = Path(path).read_bytes()
    if not raw:
        raise EngineError(E_FORMAT_CORRUPT, "PCM file is empty", {"path": str(path)})
    if len(raw) % 2:
        raise EngineError(
            E_FORMAT_CORRUPT,
            "PCM byte length is not aligned to 16-bit samples",
            {"path": str(path), "byte_length": len(raw)},
        )
    samples = np.frombuffer(raw, dtype="<i2")
    return samples, sha256_bytes(raw), raw


def block_count(sample_count: int, config: EngineConfig) -> int:
    length = config.frame_length_samples
    shift = config.frame_shift_samples
    if sample_count < length:
        return 0
    return 1 + (sample_count - length) // shift


def block_bounds(sample_count: int, config: EngineConfig) -> list[tuple[int, int, int]]:
    length = config.frame_length_samples
    shift = config.frame_shift_samples
    bounds: list[tuple[int, int, int]] = []
    for block_index in range(block_count(sample_count, config)):
        start = block_index * shift
        end = start + length
        if end > sample_count:
            break
        bounds.append((block_index, start, end))
    return bounds


def _fixed_rms_micro(sum_sq: int, sample_count: int, scale: int) -> int:
    numerator = sum_sq * scale * scale
    denominator = sample_count * 32768 * 32768
    return math.isqrt(numerator // denominator)


def compute_block_statistics(
    samples: np.ndarray,
    *,
    block_index: int,
    sample_start: int,
    sample_end: int,
    pcm_sha256: str,
    fixed_scale: int,
) -> dict[str, Any]:
    block = np.asarray(samples, dtype=np.int64)
    sample_count = int(block.size)
    if sample_count <= 0:
        raise EngineError(E_FORMAT_CORRUPT, "cannot estimate an empty envelope block")
    sum_sq = int(np.dot(block, block))
    energy = sum_sq / (sample_count * 32768.0 * 32768.0)
    rms = math.sqrt(energy)
    nonnegative = block >= 0
    zero_crossings = int(np.count_nonzero(nonnegative[1:] != nonnegative[:-1]))
    zcr = zero_crossings / float(sample_count - 1) if sample_count > 1 else 0.0
    peak = int(np.max(np.abs(block))) / 32768.0
    fixed_rms = _fixed_rms_micro(sum_sq, sample_count, fixed_scale)
    signature = {
        "block_index": block_index,
        "sample_start": sample_start,
        "sample_end": sample_end,
        "sample_count": sample_count,
        "energy": quantize(energy),
        "rms": quantize(rms),
        "zcr": quantize(zcr),
        "zero_crossings": zero_crossings,
        "peak": quantize(peak),
        "fixed_rms_micro": fixed_rms,
        "pcm_sha256": pcm_sha256,
    }
    signature["block_sha256"] = object_hash(signature)
    return signature


def compute_envelope_block(
    samples: np.ndarray,
    config: EngineConfig,
    *,
    pcm_sha256: str,
    block_index: int,
) -> dict[str, Any]:
    start = block_index * config.frame_shift_samples
    end = start + config.frame_length_samples
    if end > len(samples):
        raise EngineError(
            E_FORMAT_CORRUPT,
            "envelope block exceeds PCM sample range",
            {"block_index": block_index, "sample_end": end, "sample_count": len(samples)},
        )
    return compute_block_statistics(
        samples[start:end],
        block_index=block_index,
        sample_start=start,
        sample_end=end,
        pcm_sha256=pcm_sha256,
        fixed_scale=config.fixed_scale,
    )


def compute_envelope_serial(
    samples: np.ndarray, config: EngineConfig, *, pcm_sha256: str
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for index in range(block_count(len(samples), config)):
        start = index * config.frame_shift_samples
        end = start + config.frame_length_samples
        block = samples[start:end]
        blocks.append(
            compute_block_statistics(
                block,
                block_index=index,
                sample_start=start,
                sample_end=end,
                pcm_sha256=pcm_sha256,
                fixed_scale=config.fixed_scale,
            )
        )
    return blocks


def compute_envelope_whole(
    samples: np.ndarray, config: EngineConfig, *, pcm_sha256: str
) -> list[dict[str, Any]]:
    count = block_count(len(samples), config)
    if count == 0:
        return []
    length = config.frame_length_samples
    shift = config.frame_shift_samples
    windows = np.lib.stride_tricks.sliding_window_view(samples, length)[::shift][:count]
    integer_windows = windows.astype(np.int64)
    sum_sq = np.einsum("ij,ij->i", integer_windows, integer_windows, dtype=np.int64)
    nonnegative = integer_windows >= 0
    crossings = np.count_nonzero(nonnegative[:, 1:] != nonnegative[:, :-1], axis=1)
    peaks = np.max(np.abs(integer_windows), axis=1)
    blocks: list[dict[str, Any]] = []
    scale = config.fixed_scale
    for index in range(count):
        start = index * shift
        end = start + length
        squared = int(sum_sq[index])
        energy = squared / (length * 32768.0 * 32768.0)
        rms = math.sqrt(energy)
        zero_crossings = int(crossings[index])
        signature = {
            "block_index": index,
            "sample_start": start,
            "sample_end": end,
            "sample_count": length,
            "energy": quantize(energy),
            "rms": quantize(rms),
            "zcr": quantize(zero_crossings / float(length - 1)),
            "zero_crossings": zero_crossings,
            "peak": quantize(int(peaks[index]) / 32768.0),
            "fixed_rms_micro": _fixed_rms_micro(squared, length, scale),
            "pcm_sha256": pcm_sha256,
        }
        signature["block_sha256"] = object_hash(signature)
        blocks.append(signature)
    return blocks


def estimate_envelope_memory(block_total: int) -> int:
    return block_total * 768 + 64 * 1024


def subband_energies(
    samples: np.ndarray, config: EngineConfig
) -> list[dict[str, Any]]:
    block = np.asarray(samples, dtype=np.int16).astype(np.float64) / 32768.0
    count = len(block)
    if count < 2:
        return []
    spectrum = np.fft.rfft(block)
    power = np.abs(spectrum) ** 2
    frequencies = np.fft.rfftfreq(count, d=1.0 / config.sample_rate)
    band_edges = (0, 1000, 2000, 4000, 8000)
    result: list[dict[str, Any]] = []
    normalization = float(count * count)
    for band_index, (low_hz, high_hz) in enumerate(zip(band_edges, band_edges[1:])):
        mask = (frequencies >= low_hz) & (frequencies < high_hz)
        band_energy = float(np.sum(power[mask]) / normalization) if np.any(mask) else 0.0
        result.append(
            {
                "band_index": band_index,
                "low_hz": low_hz,
                "high_hz": high_hz,
                "energy": quantize(band_energy),
            }
        )
    return result


def select_contributing_subband(
    current: list[dict[str, Any]], previous: list[dict[str, Any]] | None
) -> dict[str, Any]:
    if previous is not None and len(previous) == len(current):
        deltas = [
            abs(float(item["energy"]) - float(before["energy"]))
            for item, before in zip(current, previous)
        ]
    else:
        deltas = [float(item["energy"]) for item in current]
    selected_index = max(range(len(current)), key=lambda index: (deltas[index], -index))
    selected = dict(current[selected_index])
    selected["delta_energy"] = quantize(deltas[selected_index])
    return selected

