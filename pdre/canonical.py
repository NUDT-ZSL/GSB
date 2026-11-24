"""Deterministic canonicalization and digest helpers."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

import numpy as np


DIGEST_BITS = {
    "md5": 128,
    "sha1": 160,
    "sha256": 256,
    "sha384": 384,
    "sha512": 512,
}

_GUID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HEX_RE_TEMPLATE = r"^[0-9a-f]{%d}$"


def canonical_text(value: Any) -> str:
    """NFKC/casefold/whitespace canonical form."""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.strip().casefold()
    text = re.sub(r"\s+", "_", text)
    return text


def canonical_policy_key(value: Any) -> str:
    key = canonical_text(value)
    key = re.sub(r"_+", "_", key)
    return key


def canonical_action_code(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).strip().upper()
    return re.sub(r"\s+", "_", text)


def is_legal_guid(value: Any) -> bool:
    return isinstance(value, str) and bool(_GUID_RE.fullmatch(value.strip()))


def is_legal_identifier(value: Any) -> bool:
    return is_legal_guid(value)


def normalize_digest(
    value: Any, declared_algorithm: str | None = "sha256"
) -> str:
    """Return ``algorithm:lowercase_hex`` and validate bit width."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("digest must be a non-empty string")

    raw = value.strip()
    algorithm = declared_algorithm or "sha256"
    digest = raw
    if ":" in raw:
        prefix, digest = raw.split(":", 1)
        algorithm = prefix.strip().lower()

    algorithm = algorithm.lower()
    if algorithm not in DIGEST_BITS:
        raise ValueError(f"unsupported digest algorithm: {algorithm}")

    expected_len = DIGEST_BITS[algorithm] // 4
    digest = digest.strip().lower()
    if not re.fullmatch(_HEX_RE_TEMPLATE % expected_len, digest):
        raise ValueError(
            f"{algorithm} digest must contain exactly {expected_len} hex chars"
        )

    digest_bytes = bytes.fromhex(digest)
    vector = np.frombuffer(digest_bytes, dtype=np.uint8)
    if vector.size != expected_len // 2:
        raise ValueError("digest byte width does not match declared algorithm")
    return f"{algorithm}:{digest}"


def digest_fingerprint(normalized_digest: str) -> int:
    """Stable numpy-derived 64-bit fingerprint used in indexes/summaries."""
    digest_hex = normalized_digest.split(":", 1)[1]
    vector = np.frombuffer(bytes.fromhex(digest_hex), dtype=np.uint8)
    if vector.size == 0:
        return 0
    mixed = np.bitwise_xor.reduce(vector.astype(np.uint64) * np.uint64(0x9E3779B97F4A7C15))
    return int(mixed) & 0xFFFFFFFFFFFFFFFF


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def short_stable_hash(value: Any, length: int = 16) -> str:
    return stable_hash(value)[:length]
