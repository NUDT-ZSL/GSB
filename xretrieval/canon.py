"""Canonical JSON serialization and content checksums.

Every persisted artifact is serialized through these helpers so identical
logical content produces byte-identical files and stable checksums.
"""

import hashlib
import json


def canonical_bytes(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_text(obj):
    return canonical_bytes(obj).decode("utf-8")


def checksum(obj):
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def short_checksum(obj, length=16):
    return checksum(obj)[:length]
