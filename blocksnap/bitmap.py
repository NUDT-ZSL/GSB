"""Changed-block bitmap backed by a packed numpy uint8 byte vector.

Bit ``b`` (block index ``b``) is stored in byte ``b // 8`` at mask
``1 << (b % 8)``. The vector always has ``ceil(block_count / 8)`` bytes so
that bitmap length is provably compatible with the volume geometry.
"""
from __future__ import annotations

import os

import numpy as np

from .errors import SnapshotError, VolumeError


def bitmap_byte_length(block_count: int) -> int:
    if not isinstance(block_count, int) or block_count <= 0:
        raise VolumeError(
            "block_count must be a positive integer",
            location="bitmap.bitmap_byte_length",
            block_count=block_count,
        )
    return (block_count + 7) // 8


class Bitmap:
    def __init__(self, block_count: int, data: np.ndarray | bytes | None = None):
        expected = bitmap_byte_length(block_count)
        if data is None:
            data = np.zeros(expected, dtype=np.uint8)
        if isinstance(data, (bytes, bytearray)):
            data = np.frombuffer(bytes(data), dtype=np.uint8).copy()
        if not isinstance(data, np.ndarray) or data.dtype != np.uint8:
            raise SnapshotError(
                "bitmap backing store must be uint8 ndarray",
                location="bitmap.Bitmap.__init__",
            )
        if data.shape != (expected,):
            raise SnapshotError(
                "bitmap length incompatible with volume block count",
                location="bitmap.Bitmap.__init__",
                expected_bytes=expected,
                actual_bytes=int(data.shape[0]),
                block_count=block_count,
            )
        # Padding bits (beyond block_count) must always be zero.
        tail = block_count % 8
        if tail and int(data[-1] & (0xFF ^ ((1 << tail) - 1))):
            raise SnapshotError(
                "bitmap padding bits must be zero",
                location="bitmap.Bitmap.__init__",
                block_count=block_count,
            )
        self.block_count = block_count
        self.data = data

    @classmethod
    def load(cls, block_count: int, path: str) -> "Bitmap":
        with open(path, "rb") as handle:
            raw = handle.read()
        return cls(block_count, raw)

    def save(self, path: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(self.data.tobytes())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def mark(self, block_index: int) -> None:
        if not 0 <= block_index < self.block_count:
            raise VolumeError(
                "block index out of range",
                location="bitmap.Bitmap.mark",
                block_index=block_index,
                block_count=self.block_count,
            )
        self.data[block_index // 8] |= np.uint8(1 << (block_index % 8))

    def is_set(self, block_index: int) -> bool:
        if not 0 <= block_index < self.block_count:
            raise VolumeError(
                "block index out of range",
                location="bitmap.Bitmap.is_set",
                block_index=block_index,
                block_count=self.block_count,
            )
        return bool(self.data[block_index // 8] & np.uint8(1 << (block_index % 8)))

    def set_indices(self) -> np.ndarray:
        """Return sorted array of marked block indices."""
        if int(self.data.size) == 0:
            return np.empty(0, dtype=np.int64)
        expanded = np.unpackbits(self.data, bitorder="little")[: self.block_count]
        return np.flatnonzero(expanded).astype(np.int64)

    def changed_count(self) -> int:
        return int(self.set_indices().size)

    def clear(self) -> None:
        self.data.fill(0)

    def to_bytes(self) -> bytes:
        return self.data.tobytes()

    def copy(self) -> "Bitmap":
        return Bitmap(self.block_count, self.data.copy())

