"""Deterministic JSON serialization: sorted keys, fixed indent, UTF-8."""

import hashlib
import json


def dumps(obj):
    """Stable, byte-reproducible JSON text (trailing newline included)."""
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def digest(obj):
    """SHA-256 hex digest of the stable JSON encoding of ``obj``."""
    return hashlib.sha256(dumps(obj).encode("utf-8")).hexdigest()


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def bytes_sha256(data):
    return hashlib.sha256(data).hexdigest()
