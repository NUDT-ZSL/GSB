"""Filesystem helpers, canonical serialisation and stable hashing.

Snapshots and caches live under a run directory that is wiped before every
run, guaranteeing that old artefacts can never contaminate a new run.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict

import numpy as np


def prepare_run_dir(run_dir: os.PathLike | str, *, clean: bool = True) -> Path:
    """Create (or recreate) the run directory used for snapshots/caches."""
    path = Path(run_dir)
    if clean and path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    (path / "snapshots").mkdir(exist_ok=True)
    (path / "cache").mkdir(exist_ok=True)
    return path


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canonical(value[k]) for k in sorted(value.keys())}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, np.ndarray):
        return {
            "__ndarray__": True,
            "dtype": str(value.dtype),
            "shape": list(value.shape),
            "bytes": value.astype(value.dtype).tobytes().hex(),
        }
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, float):
        return value
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return str(value)


def canonical_bytes(value: Any) -> bytes:
    """Deterministic byte representation shared by hashing and snapshots."""
    return json.dumps(
        _canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def stable_hash(value: Any) -> str:
    """SHA-256 hex digest of :func:`canonical_bytes`."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def write_json(path: os.PathLike | str, payload: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(_canonical(payload), fh, sort_keys=True, indent=2)
        fh.write("\n")
    return target


def snapshot(run_dir: os.PathLike | str, name: str, payload: Any) -> Dict[str, str]:
    """Persist a JSON snapshot and return its name/hash metadata."""
    target = Path(run_dir) / "snapshots" / f"{name}.json"
    write_json(target, payload)
    return {"name": name, "path": str(target), "sha256": stable_hash(payload)}


def cache_array(run_dir: os.PathLike | str, name: str, array: np.ndarray) -> str:
    """Cache a raw numpy array and return its cache path."""
    target = Path(run_dir) / "cache" / f"{name}.npy"
    target.parent.mkdir(parents=True, exist_ok=True)
    np.save(target, np.ascontiguousarray(array))
    return str(target)
