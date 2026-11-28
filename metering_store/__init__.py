"""Deterministic content-addressed storage demo for metering records."""

from .chunker import Chunk, chunk_bytes, chunk_parameters
from .database import ChunkStore
from .models import parse_record_stream, serialize_records

__all__ = [
    "Chunk",
    "ChunkStore",
    "chunk_bytes",
    "chunk_parameters",
    "parse_record_stream",
    "serialize_records",
]

__version__ = "1.0.0"
