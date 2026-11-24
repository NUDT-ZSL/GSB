"""Rule-pattern normalization shared by both rule snapshot formats."""

from __future__ import annotations

from .constants import CORE_PATTERNS

_ALIASES = {
    "CL_TE": "CL_TE",
    "CONTENT_LENGTH_TRANSFER_ENCODING": "CL_TE",
    "CONTENT_LENGTH+TRANSFER_ENCODING": "CL_TE",
    "CL+TE": "CL_TE",
    "DUP_HEADER_CONFLICT": "DUP_HEADER_CONFLICT",
    "DUPLICATE_HEADER_CONFLICT": "DUP_HEADER_CONFLICT",
    "DUPLICATE_HEADER": "DUP_HEADER_CONFLICT",
    "DUP_HEADER": "DUP_HEADER_CONFLICT",
    "CHUNK_SIZE": "CHUNK_SIZE",
    "CHUNK_SIZE_MALFORMED": "CHUNK_SIZE",
    "MALFORMED_CHUNK_SIZE": "CHUNK_SIZE",
    "CHUNK_TRAILER": "CHUNK_TRAILER",
    "CHUNK_TRAILER_INVALID": "CHUNK_TRAILER",
    "INVALID_CHUNK_TRAILER": "CHUNK_TRAILER",
    "BOUNDARY_RESIDUE": "BOUNDARY_RESIDUE",
    "BOUNDARY_UNASSIGNED": "BOUNDARY_RESIDUE",
    "UNASSIGNED_BYTES": "BOUNDARY_RESIDUE",
    "UNASSIGNED_BOUNDARY_BYTES": "BOUNDARY_RESIDUE",
}


def normalize_pattern(pattern: str) -> str:
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("pattern must be a non-empty string")
    key = pattern.strip().upper().replace(" ", "_").replace("-", "_")
    normalized = _ALIASES.get(key)
    if normalized is None:
        raise ValueError(f"unsupported rule pattern: {pattern!r}")
    return normalized


def is_core_pattern(pattern: str) -> bool:
    return pattern in CORE_PATTERNS

