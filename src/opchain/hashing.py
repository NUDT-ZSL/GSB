"""Deterministic content hashing helpers.

Everything is SHA-256 over canonical bytes so that identical logical content
always yields identical keys, independent of dict iteration order or platform.
"""

import hashlib
import json

import numpy as np


def stable_json(obj):
    """Canonical JSON text: sorted keys, fixed separators, no ASCII escaping."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def hash_json(obj):
    return hash_bytes(stable_json(obj).encode("utf-8"))


def hash_array(array):
    """Hash an ndarray from dtype/shape and its raw contiguous bytes."""
    contiguous = np.ascontiguousarray(array)
    digest = hashlib.sha256()
    digest.update(str(contiguous.dtype).encode("utf-8"))
    digest.update(b"|")
    digest.update(json.dumps(list(contiguous.shape)).encode("utf-8"))
    digest.update(b"|")
    digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()
