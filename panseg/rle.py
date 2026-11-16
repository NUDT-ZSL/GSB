"""Run-length encoding primitives shared by both mask exchange formats.

Two compression levels are supported and both are fully reversible:

* level 0 ``pairs``  - explicit (start, length) varint pairs for every 1-run;
* level 1 ``packed`` - COCO-style alternating zero/one varint counts, always
  beginning with a zero-run count (which may be zero).

Runs are canonical 1-runs stored as ``(start, length)`` integer tuples.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np

from .errors import E_RLE_OUT_OF_RANGE, PipelineError


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint only supports unsigned integers")
    chunks = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            chunks.append(byte | 0x80)
        else:
            chunks.append(byte)
            return bytes(chunks)


def decode_varints(data: bytes) -> list[int]:
    values: list[int] = []
    shift = 0
    current = 0
    for byte in data:
        current |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            values.append(current)
            shift = 0
            current = 0
        else:
            shift += 7
            if shift > 63:
                raise PipelineError(E_RLE_OUT_OF_RANGE, "varint exceeds 64 bits")
    if shift:
        raise PipelineError(E_RLE_OUT_OF_RANGE, "truncated varint stream")
    return values


def normalize_runs(
    runs: Iterable[Sequence[int]], total_pixels: int
) -> tuple[tuple[tuple[int, int], ...], int]:
    """Validate runs and return canonical sorted, non-overlapping 1-runs."""

    normalized: list[tuple[int, int]] = []
    cursor = 0
    for raw in runs:
        if len(raw) != 2:
            raise PipelineError(E_RLE_OUT_OF_RANGE, f"run must be (start, length): {raw!r}")
        start, length = int(raw[0]), int(raw[1])
        if start < cursor or length <= 0:
            raise PipelineError(
                E_RLE_OUT_OF_RANGE,
                f"overlapping or invalid run start={start} length={length} cursor={cursor}",
            )
        end = start + length
        if end > total_pixels:
            raise PipelineError(
                E_RLE_OUT_OF_RANGE,
                f"run ends at {end} but mask only holds {total_pixels} pixels",
                details={"start": start, "length": length, "total_pixels": total_pixels},
            )
        normalized.append((start, length))
        cursor = end
    return tuple(normalized), sum(length for _, length in normalized)


def runs_to_packed_counts(runs: Sequence[tuple[int, int]]) -> list[int]:
    counts: list[int] = []
    cursor = 0
    for start, length in runs:
        counts.append(start - cursor)
        counts.append(length)
        cursor = start + length
    return counts


def packed_counts_to_runs(counts: Sequence[int], total_pixels: int) -> tuple[tuple[tuple[int, int], ...], int]:
    if len(counts) % 2 != 0:
        raise PipelineError(E_RLE_OUT_OF_RANGE, "packed RLE must contain an even number of counts")
    runs: list[tuple[int, int]] = []
    position = 0
    for index, count in enumerate(counts):
        if count < 0:
            raise PipelineError(E_RLE_OUT_OF_RANGE, "negative RLE count")
        position += count
        if position > total_pixels:
            raise PipelineError(
                E_RLE_OUT_OF_RANGE,
                f"packed RLE counts exceed {total_pixels} pixels",
                details={"position": position, "total_pixels": total_pixels},
            )
        if index % 2 == 1 and count:
            start = position - count
            runs.append((start, count))
    return tuple(runs), sum(length for _, length in runs)


def encode_runs(runs: Sequence[tuple[int, int]], compression: int) -> bytes:
    if compression == 0:
        payload = bytearray()
        for start, length in runs:
            payload.extend(encode_varint(start))
            payload.extend(encode_varint(length))
        return bytes(payload)
    if compression == 1:
        payload = bytearray()
        for count in runs_to_packed_counts(runs):
            payload.extend(encode_varint(count))
        return bytes(payload)
    raise ValueError(f"unsupported compression level {compression}")


def decode_runs(data: bytes, compression: int, total_pixels: int) -> tuple[tuple[tuple[int, int], ...], int]:
    values = decode_varints(data)
    if compression == 0:
        if len(values) % 2 != 0:
            raise PipelineError(E_RLE_OUT_OF_RANGE, "pair RLE must contain an even number of values")
        pairs = [(values[i], values[i + 1]) for i in range(0, len(values), 2)]
        return normalize_runs(pairs, total_pixels)
    if compression == 1:
        return packed_counts_to_runs(values, total_pixels)
    raise ValueError(f"unsupported compression level {compression}")


def runs_to_bitmap(runs: Sequence[tuple[int, int]], height: int, width: int) -> np.ndarray:
    total = height * width
    bitmap = np.zeros(total, dtype=np.uint8)
    for start, length in runs:
        bitmap[start : start + length] = 1
    return bitmap.reshape(height, width)


def bitmap_to_runs(bitmap: np.ndarray) -> tuple[tuple[tuple[int, int], ...], int]:
    flat = np.asarray(bitmap, dtype=np.uint8).reshape(-1)
    if flat.size == 0:
        return (), 0
    padded = np.concatenate(([0], flat.view(np.uint8), [0]))
    changes = np.flatnonzero(padded[1:] != padded[:-1])
    runs: list[tuple[int, int]] = []
    for index in range(0, changes.size - 1, 2):
        start = int(changes[index])
        end = int(changes[index + 1])
        runs.append((start, end - start))
    return tuple(runs), int(flat.sum())


def run_content_fingerprint(runs: Sequence[tuple[int, int]], height: int, width: int) -> str:
    import hashlib

    digest = hashlib.sha256()
    digest.update(f"{height}x{width}:".encode("ascii"))
    digest.update(encode_runs(runs, 1))
    return digest.hexdigest()
