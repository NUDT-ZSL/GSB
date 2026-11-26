"""Block compression, content hashing, selective decode, and cache invalidation."""

from __future__ import annotations

import base64
import gzip
import os
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import numpy as np

from .config import NormalizedConfig
from .errors import (
    DiagnosticError,
    E_COMPRESS_PARAM,
    E_SNAPSHOT_CORRUPT,
)
from .postings import Posting, postings_to_sequence, term_postings_hash
from .util import (
    atomic_write_json,
    canonical_json,
    read_json,
    seal_checksum,
    sha256_bytes,
    sha256_json,
    sha256_text,
    verify_checksum,
)


CODEC_MODES = frozenset(
    {"raw", "zlib", "gzip", "delta", "delta_zlib", "delta_gzip"}
)
CODEC_ALIASES = {
    "none": "raw",
    "plain": "raw",
    "deflate": "zlib",
    "delta_raw": "delta",
    "gzip_delta": "delta_gzip",
}
INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1


def _compress_param(message: str, **details: Any) -> DiagnosticError:
    return DiagnosticError(E_COMPRESS_PARAM, message, details)


@dataclass(frozen=True)
class CompressionParams:
    chunk_size: int
    compression: str
    delta_step: int

    def __post_init__(self) -> None:
        if isinstance(self.compression, str):
            normalized = CODEC_ALIASES.get(self.compression.lower(), self.compression)
            if normalized != self.compression:
                object.__setattr__(self, "compression", normalized)

    @classmethod
    def from_config(cls, config: NormalizedConfig) -> "CompressionParams":
        result = cls(
            chunk_size=config.chunk_size,
            compression=config.compression,
            delta_step=config.delta_step,
        )
        result.validate()
        return result

    def validate(self) -> None:
        if (
            isinstance(self.chunk_size, bool)
            or not isinstance(self.chunk_size, int)
            or self.chunk_size <= 0
        ):
            raise _compress_param(
                "chunk_size must be a positive integer",
                chunk_size=self.chunk_size,
            )
        if (
            isinstance(self.delta_step, bool)
            or not isinstance(self.delta_step, int)
            or self.delta_step <= 0
        ):
            raise _compress_param(
                "delta_step must be a positive integer",
                delta_step=self.delta_step,
            )
        if self.compression not in CODEC_MODES:
            raise _compress_param(
                "unknown compression mode",
                compression=self.compression,
                supported=sorted(CODEC_MODES),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_size": self.chunk_size,
            "compression": self.compression,
            "delta_step": self.delta_step,
        }


def _validate_integer_sequence(values: Iterable[int]) -> list[int]:
    result: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _compress_param("integer stream contains a non-integer")
        if value < INT64_MIN or value > INT64_MAX:
            raise _compress_param("integer stream value is outside signed int64")
        result.append(value)
    return result


def _delta_forward(values: list[int], step: int) -> list[int]:
    if not values or step >= len(values):
        return list(values)
    if max(values) - min(values) <= INT64_MAX:
        array = np.asarray(values, dtype=np.int64)
        differences = array.copy()
        differences[step:] = array[step:] - array[:-step]
        return [int(value) for value in differences]
    differences = list(values)
    for index in range(step, len(values)):
        differences[index] = values[index] - values[index - step]
    return differences


def _delta_inverse(values: list[int], step: int) -> list[int]:
    if not values or step >= len(values):
        return list(values)
    decoded = [0] * len(values)
    for residue in range(min(step, len(values))):
        total = 0
        for index in range(residue, len(values), step):
            total += values[index]
            decoded[index] = total
    return decoded


def _zigzag_encode(value: int) -> int:
    return value << 1 if value >= 0 else ((-value) << 1) - 1


def _zigzag_decode(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def _uvarint_encode(value: int) -> bytes:
    if value < 0:
        raise _compress_param("varint input must be non-negative")
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _uvarint_decode(payload: bytes) -> list[int]:
    values: list[int] = []
    current = 0
    shift = 0
    for byte in payload:
        current |= (byte & 0x7F) << shift
        if byte & 0x80:
            shift += 7
            if shift > 70:
                raise _compress_param("varint is too long")
        else:
            values.append(current)
            current = 0
            shift = 0
    if shift:
        raise _compress_param("truncated varint stream")
    return values


def _codec_flags(mode: str) -> tuple[bool, str | None]:
    if mode not in CODEC_MODES:
        raise _compress_param(
            "unknown compression mode",
            compression=mode,
            supported=sorted(CODEC_MODES),
        )
    delta = mode in {"delta", "delta_zlib", "delta_gzip"}
    if mode in {"zlib", "delta_zlib"}:
        return delta, "zlib"
    if mode in {"gzip", "delta_gzip"}:
        return delta, "gzip"
    return delta, None


def encode_integer_stream(
    values: Iterable[int],
    params: CompressionParams,
) -> bytes:
    params.validate()
    integers = _validate_integer_sequence(values)
    delta, wrapper = _codec_flags(params.compression)
    transformed = _delta_forward(integers, params.delta_step) if delta else integers
    payload = b"".join(_uvarint_encode(_zigzag_encode(value)) for value in transformed)
    if wrapper == "zlib":
        return zlib.compress(payload, level=9)
    if wrapper == "gzip":
        return gzip.compress(payload, compresslevel=9, mtime=0)
    return payload


def decode_integer_stream(
    payload: bytes,
    params: CompressionParams,
    *,
    expected_length: int | None = None,
) -> list[int]:
    params.validate()
    delta, wrapper = _codec_flags(params.compression)
    try:
        if wrapper == "zlib":
            raw = zlib.decompress(payload)
        elif wrapper == "gzip":
            raw = gzip.decompress(payload)
        else:
            raw = payload
    except (OSError, EOFError, zlib.error) as exc:
        raise DiagnosticError(
            E_SNAPSHOT_CORRUPT,
            f"compressed payload cannot be decoded: {exc}",
        ) from exc
    unsigned = _uvarint_decode(raw)
    signed = [_zigzag_decode(value) for value in unsigned]
    values = _delta_inverse(signed, params.delta_step) if delta else signed
    if any(value < INT64_MIN or value > INT64_MAX for value in values):
        raise DiagnosticError(
            E_SNAPSHOT_CORRUPT,
            "decoded integer is outside signed int64",
        )
    if expected_length is not None and len(values) != expected_length:
        raise DiagnosticError(
            E_SNAPSHOT_CORRUPT,
            "decoded integer stream length mismatch",
            {"expected": expected_length, "actual": len(values)},
        )
    return values


def _content_hash(
    payload: bytes,
    *,
    mode: str,
    delta_step: int,
    count: int,
) -> str:
    header = canonical_json(
        {"count": count, "delta_step": delta_step, "mode": mode}
    ).encode("utf-8")
    return sha256_bytes(header + b"\0" + payload)


def _raw_hash(values: Iterable[int]) -> str:
    return sha256_json(list(values))


@dataclass(frozen=True)
class CompressedBlock:
    index: int
    start: int
    end: int
    count: int
    payload: bytes
    content_hash: str
    raw_hash: str
    encoded_size: int
    mode: str
    delta_step: int

    def verify(self) -> None:
        actual = _content_hash(
            self.payload,
            mode=self.mode,
            delta_step=self.delta_step,
            count=self.count,
        )
        if actual != self.content_hash:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "compressed block content hash mismatch",
                {"block": self.index},
            )
        if self.encoded_size != len(self.payload):
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "compressed block size mismatch",
                {"block": self.index},
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_hash": self.content_hash,
            "count": self.count,
            "delta_step": self.delta_step,
            "encoded_size": self.encoded_size,
            "end": self.end,
            "index": self.index,
            "mode": self.mode,
            "payload": base64.b64encode(self.payload).decode("ascii"),
            "raw_hash": self.raw_hash,
            "start": self.start,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CompressedBlock":
        try:
            block = cls(
                index=int(value["index"]),
                start=int(value["start"]),
                end=int(value["end"]),
                count=int(value["count"]),
                payload=base64.b64decode(value["payload"], validate=True),
                content_hash=str(value["content_hash"]),
                raw_hash=str(value["raw_hash"]),
                encoded_size=int(value["encoded_size"]),
                mode=str(value["mode"]),
                delta_step=int(value["delta_step"]),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "compressed block metadata is invalid",
                {"error": str(exc)},
            ) from exc
        block.verify()
        return block


@dataclass
class TermCompressed:
    term: str
    term_digest: str
    config_version: int
    config_generation: int
    params: CompressionParams
    posting_hash: str
    sequence_hash: str
    sequence_length: int
    cache_key: str
    blocks: list[CompressedBlock] = field(default_factory=list)

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    def expected_cache_key(self) -> str:
        return cache_key_for(
            term=self.term,
            posting_hash=self.posting_hash,
            config_version=self.config_version,
            config_generation=self.config_generation,
            params=self.params,
        )

    def verify_invariants(self) -> None:
        if self.term_digest != sha256_text(self.term):
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "term digest mismatch in compression cache",
                {"term": self.term},
            )
        if self.cache_key != self.expected_cache_key():
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "compression cache key mismatch",
                {"term": self.term},
            )
        start = 0
        for index, block in enumerate(self.blocks):
            block.verify()
            if block.index != index or block.start != start or block.end <= block.start:
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "compressed block ranges are not contiguous",
                    {"term": self.term, "block": index},
                )
            if block.end - block.start != block.count:
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "compressed block count does not match range",
                    {"term": self.term, "block": index},
                )
            start = block.end
        if start != self.sequence_length:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "compressed blocks do not cover the term sequence",
                {"term": self.term, "covered": start, "length": self.sequence_length},
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocks": [block.to_dict() for block in self.blocks],
            "cache_key": self.cache_key,
            "config_generation": self.config_generation,
            "config_version": self.config_version,
            "params": self.params.to_dict(),
            "posting_hash": self.posting_hash,
            "sequence_hash": self.sequence_hash,
            "sequence_length": self.sequence_length,
            "term": self.term,
            "term_digest": self.term_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TermCompressed":
        try:
            entry = cls(
                term=str(value["term"]),
                term_digest=str(value["term_digest"]),
                config_version=int(value["config_version"]),
                config_generation=int(value["config_generation"]),
                params=CompressionParams(
                    chunk_size=int(value["params"]["chunk_size"]),
                    compression=str(value["params"]["compression"]),
                    delta_step=int(value["params"]["delta_step"]),
                ),
                posting_hash=str(value["posting_hash"]),
                sequence_hash=str(value["sequence_hash"]),
                sequence_length=int(value["sequence_length"]),
                cache_key=str(value["cache_key"]),
                blocks=[CompressedBlock.from_dict(block) for block in value["blocks"]],
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "term compression cache entry is invalid",
                {"error": str(exc)},
            ) from exc
        entry.params.validate()
        entry.verify_invariants()
        return entry


def cache_key_for(
    *,
    term: str,
    posting_hash: str,
    config_version: int,
    config_generation: int,
    params: CompressionParams,
) -> str:
    return sha256_json(
        {
            "config_generation": config_generation,
            "config_version": config_version,
            "params": params.to_dict(),
            "posting_hash": posting_hash,
            "term_digest": sha256_text(term),
        }
    )


@dataclass
class CompressionCache:
    config_version: int
    config_generation: int
    params: CompressionParams
    entries: dict[str, TermCompressed] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        body = {
            "config_generation": self.config_generation,
            "config_version": self.config_version,
            "entries": [
                self.entries[term].to_dict() for term in sorted(self.entries)
            ],
            "format": "index-diag-compression-cache",
            "format_version": 1,
            "params": self.params.to_dict(),
        }
        return seal_checksum(body)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CompressionCache":
        if not isinstance(value, Mapping):
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "compression cache root must be an object",
            )
        verify_checksum(dict(value))
        if value.get("format") != "index-diag-compression-cache":
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "compression cache format marker is invalid",
            )
        try:
            params = CompressionParams(
                chunk_size=int(value["params"]["chunk_size"]),
                compression=str(value["params"]["compression"]),
                delta_step=int(value["params"]["delta_step"]),
            )
            params.validate()
            cache = cls(
                config_version=int(value["config_version"]),
                config_generation=int(value["config_generation"]),
                params=params,
                entries={
                    entry.term: entry
                    for entry in (
                        TermCompressed.from_dict(item) for item in value["entries"]
                    )
                },
            )
        except DiagnosticError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "compression cache envelope is invalid",
                {"error": str(exc)},
            ) from exc
        return cache

    def save(self, path: str | os.PathLike[str]) -> None:
        atomic_write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "CompressionCache":
        try:
            value = read_json(path)
        except FileNotFoundError:
            raise
        except DiagnosticError:
            raise
        return cls.from_dict(value)

    def _entry(self, term: str) -> TermCompressed:
        try:
            entry = self.entries[term]
        except KeyError as exc:
            raise DiagnosticError(
                E_COMPRESS_PARAM,
                "term is not present in compression cache",
                {"term": term},
            ) from exc
        entry.verify_invariants()
        return entry

    def decode_block(self, term: str, block_index: int) -> list[int]:
        entry = self._entry(term)
        if block_index < 0 or block_index >= len(entry.blocks):
            raise _compress_param(
                "compressed block index is out of range",
                term=term,
                block=block_index,
            )
        block = entry.blocks[block_index]
        block.verify()
        params = CompressionParams(
            chunk_size=entry.params.chunk_size,
            compression=block.mode,
            delta_step=block.delta_step,
        )
        return decode_integer_stream(
            block.payload,
            params,
            expected_length=block.count,
        )

    def decode_term(self, term: str) -> list[int]:
        entry = self._entry(term)
        values: list[int] = []
        for block in entry.blocks:
            params = CompressionParams(
                chunk_size=entry.params.chunk_size,
                compression=block.mode,
                delta_step=block.delta_step,
            )
            values.extend(
                decode_integer_stream(
                    block.payload,
                    params,
                    expected_length=block.count,
                )
            )
        if len(values) != entry.sequence_length:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "decoded term length differs from cache metadata",
                {"term": term},
            )
        if sha256_json(values) != entry.sequence_hash:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "decoded term sequence hash differs from cache metadata",
                {"term": term},
            )
        return values

    def block_status(self, term: str) -> list[dict[str, Any]]:
        entry = self._entry(term)
        return [
            {
                "content_hash": block.content_hash,
                "count": block.count,
                "delta_step": block.delta_step,
                "encoded_size": block.encoded_size,
                "end": block.end,
                "index": block.index,
                "mode": block.mode,
                "start": block.start,
            }
            for block in entry.blocks
        ]


def _make_block(
    values: list[int],
    *,
    index: int,
    start: int,
    params: CompressionParams,
) -> CompressedBlock:
    payload = encode_integer_stream(values, params)
    decoded = decode_integer_stream(
        payload,
        params,
        expected_length=len(values),
    )
    if decoded != values:
        raise DiagnosticError(
            E_COMPRESS_PARAM,
            "codec round-trip failed during block creation",
            {"block": index},
        )
    return CompressedBlock(
        index=index,
        start=start,
        end=start + len(values),
        count=len(values),
        payload=payload,
        content_hash=_content_hash(
            payload,
            mode=params.compression,
            delta_step=params.delta_step,
            count=len(values),
        ),
        raw_hash=_raw_hash(values),
        encoded_size=len(payload),
        mode=params.compression,
        delta_step=params.delta_step,
    )


def _reusable_prefix(
    values: list[int],
    previous: TermCompressed | None,
    params: CompressionParams,
) -> tuple[list[CompressedBlock], int]:
    if previous is None or previous.params != params:
        return [], 0
    reused: list[CompressedBlock] = []
    for index in range(0, len(values), params.chunk_size):
        block_index = index // params.chunk_size
        if block_index >= len(previous.blocks):
            break
        chunk = values[index : index + params.chunk_size]
        old_block = previous.blocks[block_index]
        if old_block.raw_hash != _raw_hash(chunk):
            break
        reused.append(old_block)
    return reused, len(reused)


def refresh_compression_cache(
    postings: Mapping[str, list[Posting]],
    config: NormalizedConfig,
    *,
    previous: CompressionCache | None = None,
    affected_terms: Iterable[str] | None = None,
    reason: str = "build",
    report_extra: Mapping[str, Any] | None = None,
) -> tuple[CompressionCache, dict[str, Any]]:
    """Reuse unchanged cache entries and recompute only affected block suffixes."""

    params = CompressionParams.from_config(config)
    all_terms = sorted(postings)
    requested_affected = set(
        all_terms if affected_terms is None else affected_terms
    )
    affected = requested_affected.intersection(all_terms)
    untouched = set(all_terms) - affected
    cache = CompressionCache(
        config_version=config.schema_version,
        config_generation=config.generation,
        params=params,
    )
    fully_reused: list[str] = []
    rekeyed: list[str] = []
    prefix_trimmed: list[str] = []
    suffix_recomputed: dict[str, int] = {}
    new_terms: list[str] = []
    blocks_reused = 0
    blocks_recomputed = 0
    reused_blocks_by_term: dict[str, list[int]] = {}
    recomputed_blocks_by_term: dict[str, list[int]] = {}

    for term in all_terms:
        sequence = postings_to_sequence(postings[term])
        sequence_hash = sha256_json(sequence)
        posting_hash = term_postings_hash(postings[term])
        key = cache_key_for(
            term=term,
            posting_hash=posting_hash,
            config_version=config.schema_version,
            config_generation=config.generation,
            params=params,
        )
        previous_entry = previous.entries.get(term) if previous else None
        prefix, reused_count = _reusable_prefix(sequence, previous_entry, params)
        blocks = list(prefix)
        new_block_count = (
            (len(sequence) + params.chunk_size - 1) // params.chunk_size
            if sequence
            else 0
        )
        if prefix:
            reused_blocks_by_term[term] = [block.index for block in prefix]
        if previous_entry is not None and len(prefix) == new_block_count:
            # Every block needed by the new sequence can be reused. The entry may
            # still need a new namespace key or may have discarded an old suffix.
            if (
                previous_entry.cache_key == key
                and previous_entry.sequence_length == len(sequence)
            ):
                fully_reused.append(term)
            else:
                rekeyed.append(term)
                if len(sequence) < previous_entry.sequence_length:
                    prefix_trimmed.append(term)
        elif prefix:
            suffix_recomputed[term] = len(prefix)
        elif previous_entry is None:
            new_terms.append(term)
        else:
            suffix_recomputed[term] = 0

        start = len(blocks) * params.chunk_size
        recomputed_indices: list[int] = []
        for offset in range(start, len(sequence), params.chunk_size):
            block_index = offset // params.chunk_size
            block = _make_block(
                sequence[offset : offset + params.chunk_size],
                index=block_index,
                start=offset,
                params=params,
            )
            blocks.append(block)
            recomputed_indices.append(block_index)
            blocks_recomputed += 1
        if recomputed_indices:
            recomputed_blocks_by_term[term] = recomputed_indices
        blocks_reused += len(prefix)
        entry = TermCompressed(
            term=term,
            term_digest=sha256_text(term),
            config_version=config.schema_version,
            config_generation=config.generation,
            params=params,
            posting_hash=posting_hash,
            sequence_hash=sequence_hash,
            sequence_length=len(sequence),
            cache_key=key,
            blocks=blocks,
        )
        entry.verify_invariants()
        cache.entries[term] = entry
        if cache.decode_term(term) != sequence:
            raise DiagnosticError(
                E_COMPRESS_PARAM,
                "cache refresh produced a different integer stream",
                {"term": term},
            )

    removed_terms = (
        sorted(set(previous.entries) - set(all_terms)) if previous else []
    )
    report = {
        "affected_present_terms": sorted(affected),
        "affected_terms": sorted(requested_affected),
        "blocks_recomputed": blocks_recomputed,
        "blocks_recomputed_by_term": recomputed_blocks_by_term,
        "blocks_reused": blocks_reused,
        "blocks_reused_by_term": reused_blocks_by_term,
        "first_recomputed_block": dict(sorted(suffix_recomputed.items())),
        "fully_reused_terms": fully_reused,
        "new_terms": new_terms,
        "prefix_trimmed_terms": prefix_trimmed,
        "reason": reason,
        "rekeyed_terms": rekeyed,
        "removed_terms": removed_terms,
        "reused_terms": sorted(fully_reused + rekeyed),
        "suffix_recomputed_terms": suffix_recomputed,
        "untouched_terms": sorted(untouched),
    }
    if report_extra:
        report.update(report_extra)
    return cache, report
