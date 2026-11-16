"""Training snapshot persistence: whole-state replace on restore.

A snapshot stores the parameter vector, parameter version, random seed,
model dimension and completed step count, protected by a sha256 checksum.
Restore never merges incrementally: when the snapshot version differs
from the live session version the entire state is replaced.
"""

import base64
import hashlib
import json
import os

import numpy as np

from .errors import E_SNAPSHOT, SimError

MAGIC = "ddp-sim-snapshot-v1"


def _checksum(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def save_snapshot(path, state):
    """Persist state dict {params, version, seed, dim, steps_done, lr}."""
    payload = {
        "magic": MAGIC,
        "version": int(state["version"]),
        "seed": int(state["seed"]),
        "dim": int(state["dim"]),
        "steps_done": int(state["steps_done"]),
        "lr": float(state["lr"]),
        "params_b64": base64.b64encode(
            np.ascontiguousarray(state["params"], dtype=np.float64).tobytes()
        ).decode("ascii"),
    }
    payload["checksum"] = _checksum({k: v for k, v in payload.items() if k != "checksum"})
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return payload["checksum"]


def load_snapshot(path):
    """Load and validate a snapshot; corrupt files raise E_SNAPSHOT."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SimError(E_SNAPSHOT, f"snapshot file unreadable: {exc}", path=path)
    try:
        if payload.get("magic") != MAGIC:
            raise ValueError("bad magic")
        checksum = payload.pop("checksum")
        if checksum != _checksum(payload):
            raise ValueError("checksum mismatch")
        params = np.frombuffer(base64.b64decode(payload["params_b64"]), dtype=np.float64).copy()
        if params.shape != (int(payload["dim"]),):
            raise ValueError("param vector length mismatch")
    except (ValueError, KeyError, TypeError) as exc:
        raise SimError(E_SNAPSHOT, f"snapshot integrity check failed: {exc}", path=path)
    return {
        "params": params,
        "version": int(payload["version"]),
        "seed": int(payload["seed"]),
        "dim": int(payload["dim"]),
        "steps_done": int(payload["steps_done"]),
        "lr": float(payload["lr"]),
    }
