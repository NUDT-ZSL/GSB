"""Cross-module consistency self-check.

Verifies, for every sampled clip:

1. block generation and one-shot generation produce identical keyframe pixels
   and identical boundary phase/normalisation/fusion records;
2. every committed keyframe can be independently recomputed from its recorded
   parameters (phase seed, prompt, schedule, strength, config);
3. every interpolation segment replays identically from its endpoint
   keyframes;
4. snapshot / artifact checksums verify.

Any failure is aggregated and raised as ``E_INCONSISTENT``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from .clipmodel import interpolate_segment
from .diffusion import build_canonical_sources, generate_keyframe, noise_phase_seed
from .errors import E_INCONSISTENT, KFError
from .hashing import array_digest, hash_bytes


def run_selfcheck(studio: Any, clip_ids: Optional[List[str]] = None,
                  *, raise_on_error: bool = True) -> Dict[str, Any]:
    clip_ids = clip_ids or sorted(studio.tasks)
    checks: List[Dict[str, Any]] = []

    for clip_id in clip_ids:
        task = studio.require_task(clip_id)

        # 1. block vs one-shot pixel + boundary consistency
        try:
            verdict = studio.adjudicate_consistency(clip_id, raise_on_mismatch=False)
            checks.append({
                "check": "block_vs_whole_keyframes",
                "clip_id": clip_id,
                "status": "pass" if verdict["exact_match"] else "fail",
                "detail": {
                    "selected_block_grid": verdict["selected_block_grid"],
                    "compared_keyframes": verdict["pixel_verdict"].get("compared_keyframes"),
                    "boundaries_compared": verdict["boundary_verdict"].get("compared_boundaries"),
                    "first_mismatch": verdict["pixel_verdict"].get("first_mismatch"),
                },
            })
        except KFError as exc:
            checks.append({"check": "block_vs_whole_keyframes", "clip_id": clip_id,
                           "status": "fail", "detail": exc.to_envelope()["error"]})

        manifest = studio.snapshots.current_manifest(clip_id)
        if manifest is None:
            checks.append({"check": "committed_artifacts", "clip_id": clip_id,
                           "status": "skipped", "detail": "clip has no snapshot yet"})
            continue

        # 2. independent keyframe recomputation from recorded provenance
        prov_by_ordinal = _load_provenance(studio, manifest)
        strength = float(manifest.get("selected_strength", 1.0))
        sources = build_canonical_sources(task, studio.config, strength=strength)
        recompute_failures: List[Dict[str, Any]] = []
        recomputed = 0
        for keyframe in manifest.get("keyframes", []):
            ordinal = int(keyframe["ordinal"])
            provenance = prov_by_ordinal.get(ordinal)
            produced = generate_keyframe(sources, ordinal)
            recomputed += 1
            produced_byte_hash = hash_bytes(np.ascontiguousarray(produced["pixels"]).tobytes())
            if produced_byte_hash != keyframe["content_hash"]:
                recompute_failures.append({"ordinal": ordinal, "reason": "pixels_hash_differs"})
                continue
            if provenance is not None:
                seed = noise_phase_seed(task, ordinal)
                if seed != provenance.get("noise_phase_seed"):
                    recompute_failures.append({"ordinal": ordinal, "reason": "phase_seed_differs"})
                    continue
                noise = np.random.default_rng(seed).standard_normal(task.frame_shape)
                if array_digest(noise) != provenance.get("noise_hash"):
                    recompute_failures.append({"ordinal": ordinal, "reason": "noise_phase_bytes_differ"})
            else:
                recompute_failures.append({"ordinal": ordinal, "reason": "missing_provenance"})
        checks.append({
            "check": "independent_keyframe_recompute",
            "clip_id": clip_id,
            "status": "pass" if not recompute_failures else "fail",
            "detail": {"recomputed": recomputed, "failures": recompute_failures[:8]},
        })

        # 3. interpolation replay from endpoint keyframes
        pixel_by_hash = {k["content_hash"]: k for k in manifest.get("keyframes", [])}
        segment_failures: List[Dict[str, Any]] = []
        replayed = 0
        pixels_by_ordinal: Dict[int, np.ndarray] = {}
        for keyframe in manifest.get("keyframes", []):
            pixels_by_ordinal[int(keyframe["ordinal"])] = studio._load_pixels(
                keyframe["content_hash"], tuple(task.frame_shape)
            )
        for segment in manifest.get("segments", []):
            ordinal = int(segment["ordinal"])
            frames = interpolate_segment(
                pixels_by_ordinal[ordinal], pixels_by_ordinal[ordinal + 1],
                segment["left_frame"], segment["right_frame"],
            )
            raw = studio.store.read_bytes(segment["content_hash"])
            stored = np.frombuffer(raw, dtype=np.uint8).reshape(frames.shape)
            replayed += 1
            if not np.array_equal(frames, stored):
                segment_failures.append({"ordinal": ordinal, "reason": "interpolation_bytes_differ"})
        checks.append({
            "check": "interpolation_replay",
            "clip_id": clip_id,
            "status": "pass" if not segment_failures else "fail",
            "detail": {"segments_replayed": replayed, "failures": segment_failures[:8]},
        })

        # 4. checksums
        try:
            verification = studio.snapshots.verify_current(clip_id)
            checks.append({"check": "snapshot_checksums", "clip_id": clip_id,
                           "status": "pass", "detail": verification})
        except KFError as exc:
            checks.append({"check": "snapshot_checksums", "clip_id": clip_id,
                           "status": "fail", "detail": exc.to_envelope()["error"]})

    failed = [c for c in checks if c["status"] == "fail"]
    skipped = [c for c in checks if c["status"] == "skipped"]
    report = {
        "ok": not failed,
        "clips_checked": len(clip_ids),
        "checks_total": len(checks),
        "checks_passed": sum(1 for c in checks if c["status"] == "pass"),
        "checks_failed": len(failed),
        "checks_skipped": len(skipped),
        "checks": checks,
    }
    if failed and raise_on_error:
        raise KFError(
            E_INCONSISTENT,
            "cross-module self-check detected inconsistency",
            {"failed_checks": failed, "summary": {k: report[k] for k in
             ("clips_checked", "checks_total", "checks_passed", "checks_failed")}},
        )
    return report


def _load_provenance(studio: Any, manifest: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    result: Dict[int, Dict[str, Any]] = {}
    for artifact in manifest.get("artifacts", []):
        if artifact.get("kind") != "keyframe_provenance":
            continue
        ordinal = int(artifact["ordinal"])
        result[ordinal] = studio.store.read_json(artifact["content_hash"])
    return result
