"""Canonical derived-state digest shared by platform and node simulator."""

import hashlib
import json


def canonical_digest(items):
    """sha256 over the canonical JSON of a config-item mapping."""
    blob = json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
