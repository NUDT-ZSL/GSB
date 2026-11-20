"""Deterministic artifact serialization.

Artifacts are canonical JSON bytes: sorted keys, tight separators, numpy arrays
encoded as base64 of their C-contiguous ``tobytes()`` payload plus dtype and
shape. The same logical artifact always serializes to the same bytes, which is
what makes content addressing and byte-identical recompute checks meaningful.
"""

from __future__ import annotations

import base64
import hashlib
import json

import numpy as np


class _Encoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.ndarray):
            arr = np.ascontiguousarray(obj)
            return {
                "__ndarray__": base64.b64encode(arr.tobytes()).decode("ascii"),
                "dtype": arr.dtype.str,
                "shape": list(arr.shape),
            }
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        return super().default(obj)


def _hook(obj):
    if "__ndarray__" in obj:
        arr = np.frombuffer(
            base64.b64decode(obj["__ndarray__"]), dtype=np.dtype(obj["dtype"])
        )
        return arr.reshape(obj["shape"])
    return obj


def dumps(obj) -> bytes:
    return json.dumps(
        obj, cls=_Encoder, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def loads(raw: bytes):
    return json.loads(raw.decode("utf-8"), object_hook=_hook)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_obj(obj) -> str:
    """Content hash of any JSON-serializable object (cache keys, checksums)."""
    return sha256_bytes(dumps(obj))
