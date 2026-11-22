"""Canonical serialization and hashing helpers (deterministic)."""
import hashlib
import json

import numpy as np


def _default(obj):
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, tuple):
        return list(obj)
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    raise TypeError(f"not serializable: {type(obj)!r}")


def canonical(obj):
    """Canonical JSON: sorted keys, tight separators, stable across runs."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def digest_obj(obj):
    return sha256_hex(canonical(obj))


def hash_array(arr):
    arr = np.ascontiguousarray(arr)
    h = hashlib.sha256()
    h.update(str(arr.shape).encode())
    h.update(str(arr.dtype).encode())
    h.update(arr.tobytes())
    return h.hexdigest()
