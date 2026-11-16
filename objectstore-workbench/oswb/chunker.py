"""Deterministic file chunking and chunk manifest generation.

The chunker is a pure, side-effect-free component: it never writes to the
persistent store, so an illegal chunk size fails with E_CHUNK before any
persistence happens. Re-splitting the same file with the same chunk size
always yields byte-identical chunks and an identical digest sequence.
"""

import os
from dataclasses import dataclass

from .digest import digest_bytes, normalize_algorithm
from .errors import BusinessError, E_CHUNK


@dataclass(frozen=True)
class ChunkSpec:
    index: int
    offset: int
    length: int
    digest: str

    def to_dict(self):
        return {
            "index": self.index,
            "offset": self.offset,
            "length": self.length,
            "digest": self.digest,
        }


def validate_chunk_size(chunk_size, file_size):
    """Validate the configured chunk size against the deterministic sample."""
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
        raise BusinessError(
            E_CHUNK,
            "chunk size must be a positive integer",
            {"chunk_size": chunk_size},
        )
    if chunk_size <= 0:
        raise BusinessError(
            E_CHUNK,
            "chunk size must be greater than zero",
            {"chunk_size": chunk_size},
        )
    if file_size > 0 and chunk_size > file_size:
        raise BusinessError(
            E_CHUNK,
            "chunk size must not exceed the file length",
            {"chunk_size": chunk_size, "file_size": file_size},
        )


def plan_chunks(file_size, chunk_size, algorithm):
    """Return ordered (index, offset, length) tuples without reading bytes."""
    validate_chunk_size(chunk_size, file_size)
    if file_size == 0:
        return [(0, 0, 0)]
    plan = []
    offset = 0
    index = 0
    while offset < file_size:
        length = min(chunk_size, file_size - offset)
        plan.append((index, offset, length))
        offset += length
        index += 1
    return plan


def build_manifest(file_path, chunk_size, algorithm=None):
    """Read the file once and produce the ordered, deterministic manifest."""
    algo = normalize_algorithm(algorithm)
    file_size = os.path.getsize(file_path)
    plan = plan_chunks(file_size, chunk_size, algo)
    specs = []
    with open(file_path, "rb") as handle:
        for index, offset, length in plan:
            data = handle.read(length)
            if len(data) != length:
                raise BusinessError(
                    E_CHUNK,
                    "file shrunk while building manifest",
                    {"expected": length, "actual": len(data), "index": index},
                )
            specs.append(
                ChunkSpec(
                    index=index,
                    offset=offset,
                    length=length,
                    digest=digest_bytes(data, algo),
                )
            )
    return {
        "object_size": file_size,
        "chunk_size": chunk_size,
        "algorithm": algo,
        "total_chunks": len(specs),
        "chunks": [spec.to_dict() for spec in specs],
    }


def read_chunk_bytes(file_path, offset, length):
    with open(file_path, "rb") as handle:
        handle.seek(offset)
        data = handle.read(length)
    if len(data) != length:
        raise BusinessError(
            E_CHUNK,
            "short read while slicing chunk",
            {"offset": offset, "expected": length, "actual": len(data)},
        )
    return data
