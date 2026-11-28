"""Versioned persistence, resume and input-version verification."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Optional, Tuple

import numpy as np

from .config import CoreConfig
from .io_stack import FocusStack
from .pipeline import PipelineResult


class CheckpointError(RuntimeError):
    """Raised when a checkpoint is missing or incompatible with inputs."""


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _array_fingerprint(stack: FocusStack) -> Dict[str, str]:
    fingerprints = {}
    for index, frame in enumerate(stack.frames):
        digest = hashlib.sha256(np.ascontiguousarray(frame).tobytes()).hexdigest()
        fingerprints[str(index)] = digest
    return fingerprints


def build_state_manifest(
    stack: FocusStack,
    config: CoreConfig,
    version: int,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    manifest = {
        "version": int(version),
        "config": config.to_dict(),
        "metadata_signature": stack.metadata_signature(),
        "frame_fingerprints": _array_fingerprint(stack),
    }
    if extra:
        manifest.update(extra)
    return manifest


def _result_arrays(result: PipelineResult) -> Dict[str, np.ndarray]:
    arrays = {
        "sharpness": result.sharpness,
        "valid": result.valid,
        "initial_map": result.initial_map,
        "confidence": result.confidence,
        "selected_map": result.selected_map,
        "boundary_band": result.boundary_band,
        "vote_entropy": result.vote_entropy,
        "fused": result.fused,
        "ledger": result.ledger,
        "participant_mask": result.participant_mask,
    }
    if result.ledger_recompute_mask is not None:
        arrays["ledger_recompute_mask"] = result.ledger_recompute_mask
    return arrays


def save_result(
    base_dir: str,
    sequence_id: str,
    version: int,
    stack: FocusStack,
    config: CoreConfig,
    result: PipelineResult,
    *,
    incremental: Optional[Dict[str, Any]] = None,
) -> str:
    """Persist all outputs under base/sequence/version and return the path."""
    version_dir = os.path.join(base_dir, sequence_id, f"v{int(version):04d}")
    _ensure_dir(version_dir)
    manifest = build_state_manifest(stack, config, version, incremental)
    with open(os.path.join(version_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    np.savez_compressed(
        os.path.join(version_dir, "arrays.npz"),
        **_result_arrays(result),
        radii=np.asarray(result.radii, dtype=np.int64),
        noise_floor=np.asarray(result.noise_floor, dtype=np.float64),
        isolated_corrections=np.asarray(result.isolated_corrections, dtype=np.int64),
        ledger_version=np.asarray(result.ledger_version, dtype=np.int64),
    )
    # Latest-version pointer for easy resume.
    pointer = os.path.join(base_dir, sequence_id, "latest.json")
    with open(pointer, "w", encoding="utf-8") as fh:
        json.dump({"version": int(version), "version_dir": version_dir}, fh,
                  indent=2, sort_keys=True)
    return version_dir


def _verify_manifest(
    manifest: Dict[str, Any], stack: FocusStack, config: CoreConfig
) -> None:
    expected_meta = stack.metadata_signature()
    if manifest.get("metadata_signature") != expected_meta:
        raise CheckpointError(
            "checkpoint metadata signature does not match current stack metadata"
        )
    if manifest.get("config") != config.to_dict():
        raise CheckpointError("checkpoint configuration does not match current config")
    expected_fingerprints = _array_fingerprint(stack)
    if manifest.get("frame_fingerprints") != expected_fingerprints:
        changed = sorted(
            set(expected_fingerprints)
            ^ set(manifest.get("frame_fingerprints", {}))
        )
        mismatched = [
            index
            for index in expected_fingerprints
            if manifest.get("frame_fingerprints", {}).get(index)
            != expected_fingerprints[index]
        ]
        raise CheckpointError(
            "input frames changed since checkpoint; "
            f"mismatched planes={mismatched}, missing_or_extra={changed}"
        )


def load_result(
    base_dir: str,
    sequence_id: str,
    version: int,
    stack: FocusStack,
    config: CoreConfig,
) -> Tuple[PipelineResult, Dict[str, Any]]:
    version_dir = os.path.join(base_dir, sequence_id, f"v{int(version):04d}")
    manifest_path = os.path.join(version_dir, "manifest.json")
    arrays_path = os.path.join(version_dir, "arrays.npz")
    if not os.path.exists(manifest_path) or not os.path.exists(arrays_path):
        raise CheckpointError(f"checkpoint not found in {version_dir}")
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    _verify_manifest(manifest, stack, config)
    data = np.load(arrays_path)
    result = PipelineResult(
        sharpness=data["sharpness"],
        valid=data["valid"],
        initial_map=data["initial_map"],
        confidence=data["confidence"],
        selected_map=data["selected_map"],
        boundary_band=data["boundary_band"],
        vote_entropy=data["vote_entropy"],
        isolated_corrections=int(data["isolated_corrections"]),
        fused=data["fused"],
        ledger=data["ledger"],
        participant_mask=data["participant_mask"],
        radii=tuple(int(x) for x in data["radii"]),
        noise_floor=float(data["noise_floor"]),
        ledger_recompute_mask=(
            data["ledger_recompute_mask"] if "ledger_recompute_mask" in data else None
        ),
        ledger_version=int(data["ledger_version"]) if "ledger_version" in data else 1,
    )
    return result, manifest


def latest_version(base_dir: str, sequence_id: str) -> Optional[int]:
    pointer = os.path.join(base_dir, sequence_id, "latest.json")
    if not os.path.exists(pointer):
        return None
    with open(pointer, "r", encoding="utf-8") as fh:
        return int(json.load(fh)["version"])


def resume_or_compute(
    base_dir: str,
    sequence_id: str,
    stack: FocusStack,
    config: CoreConfig,
    compute_fn,
) -> Tuple[PipelineResult, int, bool]:
    """Load a matching checkpoint or compute and persist a new version.

    Returns ``(result, version, resumed)``. Incompatible checkpoints raise
    :class:`CheckpointError`; callers decide whether to recompute as a new
    version.
    """
    version = latest_version(base_dir, sequence_id)
    if version is not None:
        result, _ = load_result(base_dir, sequence_id, version, stack, config)
        return result, version, True
    result = compute_fn()
    new_version = 1
    save_result(base_dir, sequence_id, new_version, stack, config, result)
    return result, new_version, False
