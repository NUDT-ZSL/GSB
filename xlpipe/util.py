"""Deterministic serialization, hashing and atomic writes."""

import hashlib
import json
import os

from .errors import E_IO_WRITE, PipelineError

MICRO = 1_000_000  # fixed-point scale: all scores are integers in micro-units


def canonical_dumps(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(obj):
    return canonical_dumps(obj).encode("utf-8")


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def hash_obj(obj):
    return sha256_hex(canonical_bytes(obj))


def atomic_write(path, data, *, stage=None):
    """Write bytes atomically (tmp file + fsync + rename)."""
    tmp = path + ".tmp." + str(os.getpid())
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise PipelineError(
            E_IO_WRITE, f"write failed for {path}: {exc}", stage=stage
        ) from exc


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
