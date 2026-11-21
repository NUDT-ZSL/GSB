"""Deterministic hashing helpers (content addressing and digests).

All hashes are SHA-256 hexadecimal digests. Canonical JSON serialization uses
sorted keys and a stable separator so that semantically identical structures
always produce identical bytes regardless of insertion order.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(obj: Any) -> bytes:
    """Serialize ``obj`` to deterministic, sort-key canonical JSON bytes."""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Return the SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def content_hash(obj: Any) -> str:
    """Hash a JSON-serializable object via its canonical bytes."""
    return sha256_hex(canonical_bytes(obj))


def raw_content_hash(text: str) -> str:
    """Hash the *raw textual* content of a stored result file.

    Line endings are normalized to ``\\n`` first so that CRLF/LF differences in
    the local repository never produce a spurious invalidation.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return sha256_hex(normalized.encode("utf-8"))


def combine_hashes(*parts: Any) -> str:
    """Hash an ordered sequence of parts (strings are used verbatim).

Strings are treated as already-computed digests; every other part is first
canonically serialized. Order matters, which is what we want for cache keys
that fold in upstream product hashes.
    """
    tokens = []
    for part in parts:
        tokens.append(part if isinstance(part, str) else content_hash(part))
    return sha256_hex("|".join(tokens).encode("utf-8"))
