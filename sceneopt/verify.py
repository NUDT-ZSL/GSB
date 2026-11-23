"""Independent snapshot and artifact verification tooling."""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from .cache import ArtifactCache
from .errors import VerificationFailed
from .models import SceneSpec
from .registration import registration_residual
from .serialization import content_checksum
from .snapshot import Snapshot
from .spectrum import ErrorLevel, ErrorSpectrum
from .texture import compare_texture_paths

STAGES = (
    "ingest_register",
    "geometry_simplify",
    "texture_bake",
    "block_compare",
    "summary_verify",
)


def verify_snapshot(
    scenes: Iterable[SceneSpec],
    snapshot: Snapshot,
    cache: ArtifactCache,
    *,
    expected_config_digest: str | None = None,
    raise_on_error: bool = True,
) -> dict[str, Any]:
    scene_list = list(scenes)
    scene_map = {scene.scene_id: scene for scene in scene_list}
    issues: list[str] = []
    loaded: dict[str, dict[str, dict[str, Any]]] = {}

    if (
        expected_config_digest is not None
        and snapshot.config_digest != expected_config_digest
    ):
        issues.append(
            "snapshot config digest differs from expected: "
            f"snapshot={snapshot.config_digest}, expected={expected_config_digest}"
        )

    if set(snapshot.scenes) != set(scene_map):
        issues.append(
            "snapshot scene set differs from manifest: "
            f"snapshot={sorted(snapshot.scenes)}, manifest={sorted(scene_map)}"
        )

    for scene_id in sorted(snapshot.scenes):
        scene_state = snapshot.scenes[scene_id]
        stage_refs = scene_state.get("stages", {})
        loaded[scene_id] = {}
        for stage in STAGES:
            reference = stage_refs.get(stage)
            if not isinstance(reference, dict):
                issues.append(f"{scene_id}/{stage}: missing snapshot stage reference")
                continue
            key = str(reference.get("cache_key", ""))
            checksum = str(reference.get("checksum", ""))
            try:
                entry, payload = cache.read_entry(stage, key)
            except FileNotFoundError:
                issues.append(f"{scene_id}/{stage}: cache artifact is missing")
                continue
            except Exception as exc:
                issues.append(f"{scene_id}/{stage}: unreadable cache artifact: {exc}")
                continue
            actual = content_checksum(payload)
            if entry.get("checksum") != actual:
                issues.append(f"{scene_id}/{stage}: cache entry checksum mismatch")
            if checksum != actual:
                issues.append(f"{scene_id}/{stage}: snapshot checksum mismatch")
            if payload.get("scene_id") != scene_id:
                issues.append(f"{scene_id}/{stage}: payload scene_id mismatch")
            if payload.get("stage") != stage:
                issues.append(f"{scene_id}/{stage}: payload stage mismatch")
            loaded[scene_id][stage] = payload

    stage_sets: dict[str, set[str]] = {}
    for stage in STAGES:
        stage_sets[stage] = {
            scene_id
            for scene_id, payloads in loaded.items()
            if stage in payloads
        }
        expected = set(scene_map)
        if stage_sets[stage] != expected:
            issues.append(
                f"stage {stage} scene set is inconsistent: "
                f"missing={sorted(expected - stage_sets[stage])}, "
                f"extra={sorted(stage_sets[stage] - expected)}"
            )

    registration_max_delta = 0.0
    face_budget_checked = 0
    texture_checked = 0
    spectra_checked = 0

    for scene_id in sorted(loaded):
        payloads = loaded[scene_id]
        if not all(stage in payloads for stage in STAGES):
            continue
        ingest = payloads["ingest_register"]
        geometry = payloads["geometry_simplify"]
        texture = payloads["texture_bake"]
        compare = payloads["block_compare"]
        summary = payloads["summary_verify"]
        scene = scene_map.get(scene_id)
        if scene is None:
            continue
        scene_state = snapshot.scenes[scene_id]
        expected_state = {
            "scene_revision": int(scene.scene_revision),
            "content_hash": scene.content_hash,
            "camera_pose_digest": scene.camera_pose_digest,
            "geometry_mode": scene.geometry_mode,
            "target_face_budget": int(scene.target_face_budget),
        }
        for field_name, expected_value in expected_state.items():
            if scene_state.get(field_name) != expected_value:
                issues.append(
                    f"{scene_id}: snapshot {field_name} differs from manifest"
                )
        effective = int(
            scene_state.get("effective_face_budget", scene.target_face_budget)
        )
        if effective > int(scene.target_face_budget):
            issues.append(
                f"{scene_id}: effective face budget exceeds manifest budget"
            )

        world_points = ingest.get("world_points")
        if not isinstance(world_points, np.ndarray):
            issues.append(f"{scene_id}: ingest world_points is missing")
            continue
        for observation in ingest.get("observations", []):
            try:
                residual, _distances, _nearest = registration_residual(
                    world_points,
                    observation["source_points_camera"],
                    observation["world_from_camera"],
                )
            except Exception as exc:
                issues.append(
                    f"{scene_id}/{observation.get('camera_id')}: "
                    f"residual recomputation failed: {exc}"
                )
                continue
            delta = abs(float(residual) - float(observation["residual"]))
            registration_max_delta = max(registration_max_delta, delta)
            if delta > 1e-12:
                issues.append(
                    f"{scene_id}/{observation.get('camera_id')}: "
                    f"residual mismatch {delta}"
                )

        stats = geometry.get("stats", {})
        faces_after = int(stats.get("faces_after", -1))
        target = int(stats.get("target_face_budget", -1))
        if faces_after < 0 or target < 0 or faces_after > target:
            issues.append(
                f"{scene_id}: face budget violated ({faces_after} > {target})"
            )
        if target > effective:
            issues.append(f"{scene_id}: stage target exceeds snapshot effective target")
        face_budget_checked += 1

        full = np.asarray(texture["full_texture"], dtype=np.float64)
        block = np.asarray(texture["block_texture"], dtype=np.float64)
        try:
            comparison = compare_texture_paths(full, block, scene_id=scene_id)
        except Exception as exc:
            issues.append(f"{scene_id}: block/full texture mismatch: {exc}")
            comparison = None
        if comparison is not None and not comparison["exact_equal"]:
            issues.append(f"{scene_id}: texture paths are not exact")

        full_compare = np.asarray(compare["reference_texture"], dtype=np.float64)
        block_compare = np.asarray(compare["block_texture"], dtype=np.float64)
        if (
            full.shape != full_compare.shape
            or full.tobytes(order="C") != full_compare.tobytes(order="C")
            or block.tobytes(order="C") != block_compare.tobytes(order="C")
        ):
            issues.append(f"{scene_id}: compare-stage textures differ from bake stage")
        texture_checked += 1

        spectrum_data = compare.get("error_spectrum", {})
        try:
            levels = tuple(
                ErrorLevel(
                    level=str(item["level"]),
                    absolute_error=float(item["absolute_error"]),
                    relative_error=float(item["relative_error"]),
                    error_interval=tuple(item["error_interval"]),
                    amplification_ratio=float(item["amplification_ratio"]),
                    reference_scale=float(item["reference_scale"]),
                )
                for item in spectrum_data["levels"]
            )
            ErrorSpectrum(
                levels=levels,
                dominant_level=str(spectrum_data["dominant_level"]),
                reference=str(spectrum_data.get("reference", "float64_full_path")),
            )
        except Exception as exc:
            issues.append(f"{scene_id}: invalid error spectrum: {exc}")
        spectra_checked += 1

        checks = summary.get("checks", {})
        for key in (
            "registration_residual_recomputable",
            "face_budget_ok",
            "texture_block_exact",
            "error_spectrum_valid",
        ):
            if not bool(checks.get(key, False)):
                issues.append(f"{scene_id}: summary check {key} failed")
        recorded_checksums = summary.get("stage_checksums", {})
        for upstream_stage in STAGES[:-1]:
            expected_checksum = content_checksum(payloads[upstream_stage])
            if recorded_checksums.get(upstream_stage) != expected_checksum:
                issues.append(
                    f"{scene_id}: summary checksum mismatch for {upstream_stage}"
                )
        if bool(summary.get("accepted")) != bool(scene_state.get("accepted")):
            issues.append(f"{scene_id}: snapshot acceptance flag differs from summary")

    report = {
        "format": "sceneopt-verification-report-v1",
        "passed": not issues,
        "issues": issues,
        "scene_ids": sorted(scene_map),
        "stage_scene_sets": {
            stage: sorted(values) for stage, values in stage_sets.items()
        },
        "snapshot_version": int(snapshot.version),
        "snapshot_checksum": snapshot.checksum,
        "registration_max_residual_delta": float(registration_max_delta),
        "face_budget_scenes_checked": int(face_budget_checked),
        "texture_scenes_checked": int(texture_checked),
        "spectra_checked": int(spectra_checked),
    }
    if issues and raise_on_error:
        raise VerificationFailed(issues, report=report)
    return report

