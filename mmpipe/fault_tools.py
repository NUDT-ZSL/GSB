"""Local fault injection helpers (recovery / business-code validation only).

Two explicit, local fault modes are supported:
  * hard kill of a worker at a chosen stage/pair via environment variables;
  * snapshot corruption by truncating or tampering a committed embedding.
Nothing here contacts any external service.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .config import STAGES


def corrupt_snapshot(work_dir: Path, stage: str, *, mode: str = "truncate") -> Path:
    """Damage the latest committed snapshot and return the tampered file path."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    stage_dir = Path(work_dir) / "snapshots" / stage
    versions = sorted(
        int(child.name[1:])
        for child in stage_dir.iterdir()
        if child.is_dir() and child.name.startswith("v") and child.name[1:].isdigit()
    )
    if not versions:
        raise FileNotFoundError(f"no committed snapshot for stage {stage!r}")
    version_dir = stage_dir / f"v{versions[-1]}"
    manifest = json.loads((version_dir / "manifest.json").read_text(encoding="utf-8"))
    target = version_dir / "embeddings" / f"{manifest['pair_ids'][0]}.npy"
    if mode == "truncate":
        data = target.read_bytes()
        target.write_bytes(data[: max(8, len(data) // 2)])
    elif mode == "tamper":
        array = np.load(target)
        tampered = array + np.ones_like(array)
        np.save(target, tampered)
    elif mode == "manifest":
        manifest["embeddings_checksum"] = "0" * 64
        (version_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    else:
        raise ValueError(f"unknown corruption mode {mode!r}")
    return target

