"""Canonical serialization and content-addressing helpers."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Mapping


SCHEMA_VERSION = "digit-ocr/v1"


def stable_float(value: float) -> float:
    """Return a JSON-safe, deterministic representation of a finite float."""

    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite float cannot be content-addressed")
    if number == 0.0:
        return 0.0
    return number


def canonical_bytes(value: Any) -> bytes:
    """Serialize a JSON value deterministically (sorted keys, compact form)."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def derive_cache_key(key_parts: Mapping[str, Any]) -> str:
    """Derive a node cache key from the independently verifiable key parts."""

    return sha256_json(dict(key_parts))


def seal_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Attach a content hash to a node payload without self-reference."""

    body = dict(payload)
    body.pop("content_hash", None)
    sealed = dict(body)
    sealed["content_hash"] = sha256_json(body)
    return sealed


def verify_sealed(payload: Mapping[str, Any]) -> bool:
    body = dict(payload)
    claimed = body.pop("content_hash", None)
    if not isinstance(claimed, str):
        return False
    return claimed == sha256_json(body)


def digest_payloads(payloads: Mapping[str, Mapping[str, Any]]) -> str:
    """Digest a named set of sealed payloads in a stable order."""

    framed = bytearray()
    for name in sorted(payloads):
        framed.extend(name.encode("utf-8"))
        framed.extend(b"\x00")
        framed.extend(canonical_bytes(payloads[name]))
        framed.extend(b"\x00")
    return sha256_bytes(bytes(framed))
