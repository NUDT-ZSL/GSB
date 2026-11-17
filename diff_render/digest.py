"""Canonical version identifiers built from description and target content.

The digest is a SHA-256 hash over a canonical JSON serialization with sorted
keys and fixed float formatting.  It covers the target image bytes, scene
parameters and optimization hyper-parameters, but deliberately excludes
scheduler concurrency (which must not change results) and output directories.
"""

from __future__ import annotations
import hashlib
import json
from typing import Any, Dict

import numpy as np


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_subset(config: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the digest-covered subset of the description."""
    return {
        "version": config["version"],
        "camera": config["camera"],
        "material": {
            "truth": config["material"]["truth"],
            "f0": config["material"].get("f0", 0.04),
            "ambient": config["material"].get("ambient", 0.02),
            "learning_rate": config["material"].get(
                "learning_rate", {"albedo": 0.05, "roughness": 0.05}
            ),
            "projection": config["material"].get("projection", {"mode": "clamp"}),
        },
        "lights": config["lights"],
        "optimization": _optimization_digest(config["optimization"]),
        "gradient_check": config["gradient_check"],
    }


def _optimization_digest(optimization: Dict[str, Any]) -> Dict[str, Any]:
    """Keep trajectory-shaping hyper-parameters; drop budget/scheduling fields.

    The iteration budget and checkpoint cadence do not change the math of a
    single step, so a run resumed with a different budget must still recognize
    the same scenario version and continue the identical trajectory.
    """
    excluded = {"iterations", "checkpoint_every", "tail_size"}
    return {key: value for key, value in optimization.items() if key not in excluded}


def compute_digest(config: Dict[str, Any], target: np.ndarray) -> str:
    """Return the canonical 64-character hex version identifier."""
    hasher = hashlib.sha256()
    hasher.update(b"diff-render-v1|")
    hasher.update(_canonical_json(digest_subset(config)).encode("utf-8"))
    hasher.update(b"|target:float64:")
    # np.ascontiguousarray gives stable raw bytes regardless of view layout.
    hasher.update(np.ascontiguousarray(target, dtype=np.float64).tobytes())
    return hasher.hexdigest()


def state_payload_digest(payload: Dict[str, Any]) -> str:
    """Hash the mutable iteration payload for tamper / partial-write detection."""
    hasher = sha = hashlib.sha256()
    hasher.update(b"state-payload-v1|")
    hasher.update(_canonical_json(payload).encode("utf-8"))
    return sha.hexdigest()
