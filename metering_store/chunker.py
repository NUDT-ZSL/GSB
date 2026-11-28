"""Deterministic record-stream chunking using a gear rolling hash."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache

MASK64 = (1 << 64) - 1
DIGEST_NAME = "sha256"


@dataclass(frozen=True)
class Chunk:
    """A content-defined chunk and its byte span in the source stream."""

    index: int
    offset: int
    length: int
    digest: str
    data: bytes

    @property
    def end_offset(self) -> int:
        return self.offset + self.length


@lru_cache(maxsize=1)
def gear_table() -> tuple[int, ...]:
    """Return the fixed 256-entry gear table used by the public format."""
    values = []
    for byte_value in range(256):
        material = f"metering-store-cdc-gear-v1:{byte_value}".encode("ascii")
        values.append(int.from_bytes(hashlib.sha256(material).digest()[:8], "little"))
    return tuple(values)


def chunk_parameters(target_size: int, mask_bits: int) -> tuple[int, int, int]:
    """Return (minimum_size, maximum_size, boundary_mask)."""
    if isinstance(target_size, bool) or not isinstance(target_size, int):
        raise ValueError("target_size must be an integer")
    if isinstance(mask_bits, bool) or not isinstance(mask_bits, int):
        raise ValueError("mask_bits must be an integer")
    if target_size < 16:
        raise ValueError("target_size must be at least 16 bytes")
    if mask_bits < 1 or mask_bits > 24:
        raise ValueError("mask_bits must be between 1 and 24")

    minimum_size = max(8, target_size // 4)
    maximum_size = max(minimum_size + 1, target_size * 4)
    boundary_mask = (1 << mask_bits) - 1
    return minimum_size, maximum_size, boundary_mask


def rolling_hash_step(value: int, byte_value: int) -> int:
    """Advance the gear hash by one byte."""
    rotated = ((value << 1) | (value >> 63)) & MASK64
    return (rotated ^ gear_table()[byte_value]) & MASK64


def chunk_bytes(data: bytes, target_size: int, mask_bits: int) -> list[Chunk]:
    """
    Split bytes at content-defined boundaries.

    A new chunk starts after a rolling-hash match once the minimum size is
    reached, or after the maximum size. The final chunk is always closed at EOF.
    The rolling hash is reset at each chunk start.
    """
    if not isinstance(data, bytes):
        raise ValueError("data must be bytes")

    minimum_size, maximum_size, boundary_mask = chunk_parameters(target_size, mask_bits)
    if not data:
        return []

    table = gear_table()
    chunks: list[Chunk] = []
    start = 0
    data_length = len(data)

    while start < data_length:
        value = 0
        end = start
        limit = min(data_length, start + maximum_size)

        while end < limit:
            byte_value = data[end]
            rotated = ((value << 1) | (value >> 63)) & MASK64
            value = (rotated ^ table[byte_value]) & MASK64
            end += 1
            size = end - start
            if size >= minimum_size and (value & boundary_mask) == 0:
                break

        chunk_data = data[start:end]
        digest = hashlib.new(DIGEST_NAME, chunk_data).hexdigest()
        chunks.append(
            Chunk(
                index=len(chunks),
                offset=start,
                length=end - start,
                digest=digest,
                data=chunk_data,
            )
        )
        start = end

    return chunks
