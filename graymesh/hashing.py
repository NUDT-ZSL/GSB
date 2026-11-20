"""Stable hashing helpers: identical output on every run, platform and process."""

import hashlib
import json


def stable_hash(text):
    """Deterministic 64-bit non-negative hash of a string."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def canonical_json(obj):
    """Canonical serialization: sorted keys, fixed separators -> byte-stable."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def short_digest(obj):
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()[:16]
