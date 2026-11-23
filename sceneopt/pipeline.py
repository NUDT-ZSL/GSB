"""Five-stage deterministic scene optimization pipeline."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Any

import numpy as np

from .cache import ArtifactCache, build_stage_cache_key
from .config import PipelineConfig
from .errors import (
    CacheCorrupt,
    IncrementalInconsistent,
    ResourceLimitExceeded,
    SceneRevisionRollback,
    SelfCheckFailed,
    SnapshotCorrupt,
)
from .geometry import (
    apply_geometry_mode,
    generate_observations,
    generate_world_geometry,
)
from .mesh import approximation_distances, fuse_meshes, simplify_mesh
from .models import SceneSpec, load_scene_manifest
from .registration import register_point_cloud, registration_residual, transform_points
from .serialization import content_checksum
from .snapshot import Snapshot, SnapshotCommitResult, SnapshotStore
from .spectrum import ErrorLevel, ErrorSpectrum, build_error_spectrum
from .texture import bake_texture_paths, compare_texture_paths
from .thresholds import evaluate_threshold

STAGE_NAMES = (
    "ingest_register",
    "geometry_simplify",
    "texture_bake",
    "block_compare",
    "summary_verify",
)
STAGE_INDEX = {stage: index for index, stage in enumerate(STAGE_NAMES)}


@dataclass
class StageArtifact:
    stage: str
    scene_id: str
    payload: dict[str, Any]
    checksum: str
    cache_key: str
    source: str


@dataclass
class SceneRun:
    scene: SceneSpec
    effective_face_budget: int
    change_type: str
    change_reasons: tuple[str, ...]
    affected_stages: tuple[str, ...]
    affected_start: int
    artifacts: dict[str, StageArtifact]
    resource_degraded: bool
    accepted: bool


@dataclass
class RunResult:
    snapshot: Snapshot
    commit_result: SnapshotCommitResult
    report: dict[str, Any]
    scene_runs: dict[str, SceneRun]
    cache: ArtifactCache
    snapshot_store: SnapshotStore

    def write_report(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        from .serialization import canonical_bytes

        with open(path, "wb") as handle:
            handle.write(canonical_bytes(self.report))


@dataclass(frozen=True)
class _ChangeInfo:
    change_type: str
    reasons: tuple[str, ...]
    affected_start: int

    @property
    def affected_stages(self) -> tuple[str, ...]:
        return tuple(STAGE_NAMES[self.affected_start :])


def _snapshot_scene_state(
    scene: SceneSpec,
    *,
    effective_face_budget: int,
    resource_degraded: bool,
    accepted: bool,
    artifacts: dict[str, StageArtifact],
) -> dict[str, Any]:
    return {
        "scene_revision": int(scene.scene_revision),
        "content_hash": scene.content_hash,
        "camera_pose_digest": scene.camera_pose_digest,
        "geometry_mode": scene.geometry_mode,
        "target_face_budget": int(scene.target_face_budget),
        "effective_face_budget": int(effective_face_budget),
        "resource_degraded": bool(resource_degraded),
        "accepted": bool(accepted),
        "stages": {
            stage: {
                "cache_key": artifacts[stage].cache_key,
                "checksum": artifacts[stage].checksum,
            }
            for stage in STAGE_NAMES
        },
    }


class Pipeline:
    def __init__(
        self,
        manifest_path: str,
        cache_dir: str = "cache",
        snapshot_path: str | None = None,
        *,
        config: PipelineConfig | None = None,
        verify_incremental: bool = True,
    ) -> None:
        self.manifest_path = os.path.abspath(manifest_path)
        self.scenes = load_scene_manifest(self.manifest_path)
        self.scene_map = {scene.scene_id: scene for scene in self.scenes}
        self.config = config or PipelineConfig()
        self.cache = ArtifactCache(cache_dir)
        path = snapshot_path or os.path.join(cache_dir, "snapshot.json")
        self.snapshot_store = SnapshotStore(path)
        self.verify_incremental = bool(verify_incremental)

    def run(self) -> RunResult:
        with self.snapshot_store.gate:
            current = self.snapshot_store.load()
            return self._run_locked(current)

    def _classify_change(
        self,
        scene: SceneSpec,
        old_state: dict[str, Any] | None,
    ) -> _ChangeInfo:
        if old_state is None:
            return _ChangeInfo("added_scene", ("scene_added",), 0)
        old_revision = int(old_state.get("scene_revision", 0))
        if int(scene.scene_revision) < old_revision:
            raise SceneRevisionRollback(
                scene.scene_id, scene.scene_revision, old_revision
            )

        reasons: list[str] = []
        if old_state.get("content_hash") != scene.content_hash:
            reasons.append("scene_content_changed")
        if old_state.get("camera_pose_digest") != scene.camera_pose_digest:
            reasons.append("camera_observation_replaced")
        if old_state.get("geometry_mode") != scene.geometry_mode:
            reasons.append("geometry_mode_switched")
        if int(old_state.get("target_face_budget", -1)) != scene.target_face_budget:
            reasons.append("target_face_budget_changed")

        old_stages = old_state.get("stages", {})
        missing_stage_index = len(STAGE_NAMES)
        for index, stage in enumerate(STAGE_NAMES):
            reference = old_stages.get(stage)
            if (
                not isinstance(reference, dict)
                or "cache_key" not in reference
                or "checksum" not in reference
            ):
                missing_stage_index = min(missing_stage_index, index)
        if missing_stage_index < len(STAGE_NAMES):
            reasons.append("snapshot_stage_reference_missing")

        affected_start = len(STAGE_NAMES)
        if reasons:
            affected_start = min(affected_start, missing_stage_index)
            if (
                old_state.get("content_hash") != scene.content_hash
                or old_state.get("camera_pose_digest") != scene.camera_pose_digest
            ):
                affected_start = 0
            if old_state.get("geometry_mode") != scene.geometry_mode:
                affected_start = min(affected_start, STAGE_INDEX["geometry_simplify"])
            if int(old_state.get("target_face_budget", -1)) != scene.target_face_budget:
                affected_start = min(affected_start, STAGE_INDEX["geometry_simplify"])

        if not reasons:
            return _ChangeInfo("unchanged", (), len(STAGE_NAMES))
        if (
            old_state.get("content_hash") != scene.content_hash
            or old_state.get("camera_pose_digest") != scene.camera_pose_digest
        ):
            change_type = "camera_replacement"
        elif old_state.get("geometry_mode") != scene.geometry_mode:
            change_type = "geometry_mode_switch"
        elif int(old_state.get("target_face_budget", -1)) != scene.target_face_budget:
            change_type = "target_face_budget_change"
        else:
            change_type = "metadata_only"
        return _ChangeInfo(change_type, tuple(reasons), affected_start)

    def _effective_face_budget(
        self,
        scene: SceneSpec,
        resource_flags: list[dict[str, Any]],
    ) -> tuple[int, bool]:
        limit = int(self.config.limits.max_faces_per_scene)
        if scene.target_face_budget <= limit:
            return scene.target_face_budget, False
        if not self.config.limits.allow_degrade:
            raise ResourceLimitExceeded(
                resource="faces_per_scene",
                limit=limit,
                actual=scene.target_face_budget,
                scene_id=scene.scene_id,
            )
        resource_flags.append(
            {
                "scene_id": scene.scene_id,
                "resource": "faces_per_scene",
                "degraded": True,
                "requested": int(scene.target_face_budget),
                "effective": limit,
            }
        )
        return limit, True

    def _cache_key(
        self,
        stage: str,
        scene: SceneSpec,
        upstream_checksum: str,
        effective_face_budget: int,
    ) -> str:
        return build_stage_cache_key(
            stage,
            scene_content_hash=scene.content_hash,
            camera_pose_digest=scene.camera_pose_digest,
            geometry_mode=scene.geometry_mode,
            target_face_budget=scene.target_face_budget,
            upstream_artifact_hash=upstream_checksum,
            config_digest=self.config.digest,
            effective_face_budget=effective_face_budget,
        )

    def _read_reused_snapshot_stage(
        self,
        scene: SceneSpec,
        stage: str,
        reference: dict[str, Any],
    ) -> StageArtifact | None:
        key = str(reference["cache_key"])
        expected_checksum = str(reference["checksum"])
        try:
            payload = self.cache.get(stage, key)
        except CacheCorrupt as exc:
            raise SnapshotCorrupt(
                "referenced stage artifact is corrupt",
                scene_id=scene.scene_id,
                stage=stage,
                cache_key=key,
                cache_error=exc.as_dict(),
            ) from exc
        if payload is None:
            return None
        checksum = content_checksum(payload)
        if checksum != expected_checksum:
            raise SnapshotCorrupt(
                "referenced stage artifact checksum changed",
                scene_id=scene.scene_id,
                stage=stage,
                expected_checksum=expected_checksum,
                actual_checksum=checksum,
            )
        return StageArtifact(
            stage=stage,
            scene_id=scene.scene_id,
            payload=payload,
            checksum=checksum,
            cache_key=key,
            source="snapshot_reuse",
        )

    def _compute_stage(
        self,
        stage: str,
        scene: SceneSpec,
        effective_face_budget: int,
        upstream_checksum: str,
        previous: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if stage == "ingest_register":
            return self._stage_ingest(scene)
        if stage == "geometry_simplify":
            return self._stage_geometry(
                scene,
                previous["ingest_register"],
                effective_face_budget,
                upstream_checksum,
            )
        if stage == "texture_bake":
            return self._stage_texture(
                scene,
                previous["ingest_register"],
                previous["geometry_simplify"],
                upstream_checksum,
            )
        if stage == "block_compare":
            return self._stage_compare(
                scene,
                previous["ingest_register"],
                previous["geometry_simplify"],
                previous["texture_bake"],
                upstream_checksum,
            )
        if stage == "summary_verify":
            return self._stage_summary(
                scene,
                previous["ingest_register"],
                previous["geometry_simplify"],
                previous["texture_bake"],
                previous["block_compare"],
                upstream_checksum,
            )
        raise ValueError(f"unknown stage: {stage}")

    def _stage_ingest(self, scene: SceneSpec) -> dict[str, Any]:
        geometry = generate_world_geometry(scene, self.config)
        observations = generate_observations(scene, geometry, self.config)
        observation_payloads: list[dict[str, Any]] = []
        all_distances: list[np.ndarray] = []
        camera_residuals: list[float] = []
        independent_deltas: list[float] = []

        for observation in observations:
            result = register_point_cloud(
                geometry.points,
                observation.source_points_camera,
                observation.camera.world_from_camera,
                max_iterations=self.config.icp_max_iterations,
                tolerance=self.config.icp_tolerance,
            )
            recomputed, recomputed_distances, _nearest = registration_residual(
                geometry.points,
                observation.source_points_camera,
                result.world_from_camera,
            )
            delta = abs(float(result.residual) - float(recomputed))
            independent_deltas.append(delta)
            all_distances.append(result.distances)
            camera_residuals.append(float(result.residual))
            observation_payloads.append(
                {
                    "camera_id": observation.camera.camera_id,
                    "source_indices": observation.source_indices,
                    "source_points_camera": observation.source_points_camera,
                    "camera_image": observation.camera_image,
                    "initial_world_from_camera": observation.camera.world_from_camera,
                    "world_from_camera": result.world_from_camera,
                    "residual": float(result.residual),
                    "iterations": int(result.iterations),
                    "nearest_indices": result.nearest_indices,
                    "distances": result.distances,
                    "min_distance": float(np.min(result.distances)),
                    "max_distance": float(np.max(result.distances)),
                    "independent_residual_delta": float(delta),
                }
            )

        distances = np.concatenate(all_distances)
        if distances.size == 0:
            raise ValueError(f"{scene.scene_id}: registration produced no residuals")
        aggregate_residual = float(np.sqrt(np.mean(distances * distances)))
        registration_interval = (
            float(np.min(distances)),
            float(np.max(distances)),
        )
        registration_gate = evaluate_threshold(
            max(camera_residuals),
            self.config.thresholds.registration_limit,
            policy=self.config.thresholds.policy,
            tolerance_abs=self.config.thresholds.absolute_tolerance,
            tolerance_rel=self.config.thresholds.relative_tolerance,
            direction="le",
            field=f"{scene.scene_id}.registration_residual",
            protect_subnormal=self.config.thresholds.protect_subnormal,
        )
        observation_digest = content_checksum(
            [
                {
                    "camera_id": item["camera_id"],
                    "source_indices": item["source_indices"],
                    "source_points_camera": item["source_points_camera"],
                    "camera_image": item["camera_image"],
                    "world_from_camera": item["world_from_camera"],
                }
                for item in observation_payloads
            ]
        )
        return {
            "format": "sceneopt-stage-ingest-v1",
            "stage": "ingest_register",
            "scene_id": scene.scene_id,
            "content_hash": scene.content_hash,
            "camera_pose_digest": scene.camera_pose_digest,
            "upstream_checksum": "ROOT",
            "world_points": geometry.points,
            "world_faces": geometry.faces,
            "world_uv": geometry.uv,
            "world_rows": int(geometry.rows),
            "world_cols": int(geometry.cols),
            "world_scale": float(geometry.scale),
            "world_diagonal": float(geometry.diagonal),
            "observations": observation_payloads,
            "registration_residual": aggregate_residual,
            "registration_interval": registration_interval,
            "registration_scale": float(geometry.scale),
            "max_camera_residual": float(max(camera_residuals)),
            "camera_residuals": camera_residuals,
            "independent_residual_deltas": independent_deltas,
            "camera_observation_digest": observation_digest,
            "threshold_decision": registration_gate.to_dict(),
        }

    def _stage_geometry(
        self,
        scene: SceneSpec,
        ingest: dict[str, Any],
        effective_face_budget: int,
        upstream_checksum: str,
    ) -> dict[str, Any]:
        geometry_points = ingest["world_points"]
        geometry_faces = ingest["world_faces"]
        geometry_uv = ingest["world_uv"]
        from .geometry import WorldGeometry

        geometry = WorldGeometry(
            points=geometry_points,
            faces=geometry_faces,
            uv=geometry_uv,
            rows=int(ingest["world_rows"]),
            cols=int(ingest["world_cols"]),
            geometry_mode=scene.geometry_mode,
        )
        transformed: list[tuple[str, np.ndarray]] = []
        for observation in ingest["observations"]:
            transformed.append(
                (
                    observation["camera_id"],
                    transform_points(
                        observation["source_points_camera"],
                        observation["world_from_camera"],
                    ),
                )
            )
        fused = fuse_meshes(
            geometry,
            transformed,
            fusion_strength=self.config.fusion_strength,
            sigma=float(ingest["world_scale"])
            * float(self.config.fusion_sigma_scale),
        )
        fused_vertices = apply_geometry_mode(
            fused.vertices,
            geometry.points,
            fused.uv,
            scene.geometry_mode,
            scene.point_cloud_seed,
        )
        simplified = simplify_mesh(
            fused_vertices,
            fused.faces,
            fused.uv,
            int(effective_face_budget),
            scene_id=scene.scene_id,
        )
        approximation = approximation_distances(
            fused_vertices, simplified.vertices
        )
        simplification_abs = float(
            np.sqrt(np.mean(approximation * approximation))
        )
        simplification_interval = (
            float(np.min(approximation)),
            float(np.max(approximation)),
        )
        simplification_gate = evaluate_threshold(
            simplified.simplification_degree,
            self.config.thresholds.simplification_min_degree,
            policy=self.config.thresholds.policy,
            tolerance_abs=self.config.thresholds.absolute_tolerance,
            tolerance_rel=self.config.thresholds.relative_tolerance,
            direction="ge",
            field=f"{scene.scene_id}.simplification_degree",
            protect_subnormal=self.config.thresholds.protect_subnormal,
        )
        fusion_interval = fused.normalization_interval
        lower, upper = np.min(fused_vertices, axis=0), np.max(fused_vertices, axis=0)
        mode_diagonal = max(
            float(np.linalg.norm(upper - lower)),
            float(np.finfo(np.float64).tiny),
        )
        return {
            "format": "sceneopt-stage-geometry-v1",
            "stage": "geometry_simplify",
            "scene_id": scene.scene_id,
            "upstream_checksum": upstream_checksum,
            "fused_vertices": np.ascontiguousarray(fused_vertices, dtype=np.float64),
            "fused_faces": fused.faces,
            "fused_uv": fused.uv,
            "simplified_vertices": simplified.vertices,
            "simplified_faces": simplified.faces,
            "simplified_uv": simplified.uv,
            "fusion": {
                "normalization_interval": list(fusion_interval),
                "absolute_error": float(fusion_interval[1]),
                "contributed_vertices": int(fused.contributed_vertices),
            },
            "simplification": {
                "absolute_error": float(simplification_abs),
                "error_interval": list(simplification_interval),
                "reference_scale": float(mode_diagonal),
            },
            "stats": {
                "faces_before": int(simplified.faces_before),
                "faces_after": int(simplified.faces_after),
                "target_face_budget": int(effective_face_budget),
                "requested_target_face_budget": int(scene.target_face_budget),
                "simplification_degree": float(simplified.simplification_degree),
                "edge_collapses": len(simplified.history),
                "collapse_history": list(simplified.history),
            },
            "threshold_decision": simplification_gate.to_dict(),
        }

    def _stage_texture(
        self,
        scene: SceneSpec,
        ingest: dict[str, Any],
        geometry: dict[str, Any],
        upstream_checksum: str,
    ) -> dict[str, Any]:
        camera_images = {
            observation["camera_id"]: observation["camera_image"]
            for observation in ingest["observations"]
        }
        textured = bake_texture_paths(
            geometry["simplified_vertices"],
            geometry["simplified_faces"],
            geometry["simplified_uv"],
            scene.camera_poses,
            camera_images,
            texture_seed=scene.texture_seed,
            height=self.config.texture_height,
            width=self.config.texture_width,
            block_height=self.config.texture_block_height,
            block_width=self.config.texture_block_width,
            multi_view_tolerance=self.config.multi_view_tolerance,
            scene_id=scene.scene_id,
        )
        return {
            "format": "sceneopt-stage-texture-v1",
            "stage": "texture_bake",
            "scene_id": scene.scene_id,
            "upstream_checksum": upstream_checksum,
            "full_texture": textured.full_texture,
            "block_texture": textured.block_texture,
            "weight_sums": textured.weight_sums,
            "visible_camera_counts": textured.visible_camera_counts,
            "base_colors": textured.base_colors,
            "comparison": textured.comparison,
            "multi_view": textured.multi_view,
            "blocks": list(textured.blocks),
            "texture_scale": float(textured.texture_scale),
        }

    def _stage_compare(
        self,
        scene: SceneSpec,
        ingest: dict[str, Any],
        geometry: dict[str, Any],
        texture: dict[str, Any],
        upstream_checksum: str,
    ) -> dict[str, Any]:
        comparison = compare_texture_paths(
            texture["full_texture"],
            texture["block_texture"],
            scene_id=scene.scene_id,
        )
        full = np.asarray(texture["full_texture"], dtype=np.float64)
        block = np.asarray(texture["block_texture"], dtype=np.float64)
        texture_interval = (
            float(comparison["deviation_spectrum"]["min"]),
            float(comparison["deviation_spectrum"]["max"]),
        )
        spectrum = build_error_spectrum(
            registration_absolute_error=float(ingest["registration_residual"]),
            registration_error_interval=tuple(ingest["registration_interval"]),
            registration_scale=float(ingest["registration_scale"]),
            fusion_absolute_error=float(geometry["fusion"]["absolute_error"]),
            fusion_error_interval=tuple(geometry["fusion"]["normalization_interval"]),
            simplification_absolute_error=float(
                geometry["simplification"]["absolute_error"]
            ),
            simplification_error_interval=tuple(
                geometry["simplification"]["error_interval"]
            ),
            simplification_scale=float(
                geometry["simplification"]["reference_scale"]
            ),
            texture_absolute_error=float(comparison["max_abs_diff"]),
            texture_error_interval=texture_interval,
            texture_scale=float(texture["texture_scale"]),
        )
        return {
            "format": "sceneopt-stage-compare-v1",
            "stage": "block_compare",
            "scene_id": scene.scene_id,
            "upstream_checksum": upstream_checksum,
            "reference_texture": np.ascontiguousarray(full),
            "block_texture": np.ascontiguousarray(block),
            "reference_texture_checksum": content_checksum(full),
            "block_texture_checksum": content_checksum(block),
            "comparison": comparison,
            "error_spectrum": spectrum.to_dict(),
        }

    def _stage_summary(
        self,
        scene: SceneSpec,
        ingest: dict[str, Any],
        geometry: dict[str, Any],
        texture: dict[str, Any],
        compare: dict[str, Any],
        upstream_checksum: str,
    ) -> dict[str, Any]:
        independent_ok = True
        max_independent_delta = 0.0
        for observation in ingest["observations"]:
            residual, _distances, _nearest = registration_residual(
                ingest["world_points"],
                observation["source_points_camera"],
                observation["world_from_camera"],
            )
            delta = abs(float(residual) - float(observation["residual"]))
            max_independent_delta = max(max_independent_delta, delta)
            if delta > 1e-12:
                independent_ok = False
        face_budget_ok = int(geometry["stats"]["faces_after"]) <= int(
            geometry["stats"]["target_face_budget"]
        )
        full = np.asarray(texture["full_texture"], dtype=np.float64)
        block = np.asarray(texture["block_texture"], dtype=np.float64)
        texture_exact = full.shape == block.shape and (
            full.tobytes(order="C") == block.tobytes(order="C")
        )
        spectrum_data = compare["error_spectrum"]
        levels = tuple(
            ErrorLevel(
                level=item["level"],
                absolute_error=float(item["absolute_error"]),
                relative_error=float(item["relative_error"]),
                error_interval=tuple(item["error_interval"]),
                amplification_ratio=float(item["amplification_ratio"]),
                reference_scale=float(item["reference_scale"]),
            )
            for item in spectrum_data["levels"]
        )
        spectrum_valid = True
        try:
            ErrorSpectrum(
                levels=levels,
                dominant_level=str(spectrum_data["dominant_level"]),
                reference=str(spectrum_data["reference"]),
            )
        except Exception:
            spectrum_valid = False
        registration_gate = bool(ingest["threshold_decision"]["passed"])
        simplification_gate = bool(geometry["threshold_decision"]["passed"])
        checks = {
            "registration_residual_recomputable": independent_ok,
            "max_independent_residual_delta": float(max_independent_delta),
            "face_budget_ok": bool(face_budget_ok),
            "texture_block_exact": bool(texture_exact),
            "error_spectrum_valid": bool(spectrum_valid),
            "registration_threshold_passed": registration_gate,
            "simplification_threshold_passed": simplification_gate,
        }
        accepted = all(
            (
                checks["registration_residual_recomputable"],
                checks["face_budget_ok"],
                checks["texture_block_exact"],
                checks["error_spectrum_valid"],
                checks["registration_threshold_passed"],
                checks["simplification_threshold_passed"],
            )
        )
        stage_checksums = {
            "ingest_register": content_checksum(ingest),
            "geometry_simplify": content_checksum(geometry),
            "texture_bake": content_checksum(texture),
            "block_compare": content_checksum(compare),
        }
        return {
            "format": "sceneopt-stage-summary-v1",
            "stage": "summary_verify",
            "scene_id": scene.scene_id,
            "upstream_checksum": upstream_checksum,
            "checks": checks,
            "stage_checksums": stage_checksums,
            "accepted": bool(accepted),
            "summary": {
                "registration_residual": float(ingest["registration_residual"]),
                "faces_before": int(geometry["stats"]["faces_before"]),
                "faces_after": int(geometry["stats"]["faces_after"]),
                "simplification_degree": float(
                    geometry["stats"]["simplification_degree"]
                ),
                "texture_exact": bool(texture_exact),
                "max_abs_texture_diff": float(
                    compare["comparison"]["max_abs_diff"]
                ),
                "dominant_error_level": str(
                    compare["error_spectrum"]["dominant_level"]
                ),
                "multi_view_consistent": bool(texture["multi_view"]["consistent"]),
                "registration_threshold": ingest["threshold_decision"],
                "simplification_threshold": geometry["threshold_decision"],
            },
        }

    def _compute_scene(
        self,
        scene: SceneSpec,
        change: _ChangeInfo,
        old_state: dict[str, Any] | None,
        effective_face_budget: int,
    ) -> SceneRun:
        artifacts: dict[str, StageArtifact] = {}
        payloads: dict[str, dict[str, Any]] = {}
        upstream_checksum = "ROOT"
        old_stages = {} if old_state is None else old_state.get("stages", {})

        for index, stage in enumerate(STAGE_NAMES):
            cache_key = self._cache_key(
                stage, scene, upstream_checksum, effective_face_budget
            )
            artifact: StageArtifact | None = None
            if index < change.affected_start:
                reference = old_stages.get(stage)
                if isinstance(reference, dict):
                    artifact = self._read_reused_snapshot_stage(
                        scene, stage, reference
                    )
                    if artifact is not None and artifact.cache_key != cache_key:
                        # Preserve the committed bytes without recomputation,
                        # but expose them under the current content key.
                        if not self.cache.exists(stage, cache_key):
                            materialized_checksum = self.cache.put(
                                stage, cache_key, artifact.payload
                            )
                            if materialized_checksum != artifact.checksum:
                                raise IncrementalInconsistent(
                                    scene.scene_id,
                                    stage=stage,
                                    expected_checksum=artifact.checksum,
                                    actual_checksum=materialized_checksum,
                                )
                        artifact.cache_key = cache_key
            if artifact is None:
                payload = None
                try:
                    payload = self.cache.get(stage, cache_key)
                except CacheCorrupt:
                    payload = None
                if payload is not None:
                    artifact = StageArtifact(
                        stage=stage,
                        scene_id=scene.scene_id,
                        payload=payload,
                        checksum=content_checksum(payload),
                        cache_key=cache_key,
                        source="cache_reuse",
                    )
                else:
                    payload = self._compute_stage(
                        stage,
                        scene,
                        effective_face_budget,
                        upstream_checksum,
                        payloads,
                    )
                    checksum = self.cache.put(stage, cache_key, payload)
                    artifact = StageArtifact(
                        stage=stage,
                        scene_id=scene.scene_id,
                        payload=payload,
                        checksum=checksum,
                        cache_key=cache_key,
                        source="computed",
                    )
            artifacts[stage] = artifact
            payloads[stage] = artifact.payload
            upstream_checksum = artifact.checksum

        summary_payload = artifacts["summary_verify"].payload
        return SceneRun(
            scene=scene,
            effective_face_budget=effective_face_budget,
            change_type=change.change_type,
            change_reasons=change.reasons,
            affected_stages=change.affected_stages,
            affected_start=change.affected_start,
            artifacts=artifacts,
            resource_degraded=False,
            accepted=bool(summary_payload["accepted"]),
        )

    def _fresh_scene_payloads(
        self,
        scene: SceneSpec,
        effective_face_budget: int,
        cache: ArtifactCache,
    ) -> dict[str, dict[str, Any]]:
        payloads: dict[str, dict[str, Any]] = {}
        upstream_checksum = "ROOT"
        for stage in STAGE_NAMES:
            cache_key = self._cache_key(
                stage, scene, upstream_checksum, effective_face_budget
            )
            payload = cache.get(stage, cache_key)
            if payload is None:
                payload = self._compute_stage(
                    stage,
                    scene,
                    effective_face_budget,
                    upstream_checksum,
                    payloads,
                )
                cache.put(stage, cache_key, payload)
            payloads[stage] = payload
            upstream_checksum = content_checksum(payload)
        return payloads

    def _verify_full_replay(
        self,
        scene_runs: dict[str, SceneRun],
    ) -> bool:
        with tempfile.TemporaryDirectory(prefix="sceneopt-replay-") as directory:
            replay_cache = ArtifactCache(directory)
            for scene_id in sorted(scene_runs):
                run = scene_runs[scene_id]
                payloads = self._fresh_scene_payloads(
                    run.scene, run.effective_face_budget, replay_cache
                )
                for stage in STAGE_NAMES:
                    actual = content_checksum(payloads[stage])
                    expected = run.artifacts[stage].checksum
                    if actual != expected:
                        raise IncrementalInconsistent(
                            scene_id,
                            stage=stage,
                            expected_checksum=expected,
                            actual_checksum=actual,
                        )
        return True

    def _estimate_resident_bytes(
        self,
        scene_runs: dict[str, SceneRun],
    ) -> int:
        total = 0
        for run in scene_runs.values():
            geometry = run.artifacts["geometry_simplify"].payload
            texture = run.artifacts["texture_bake"].payload
            total += int(np.asarray(geometry["simplified_vertices"]).nbytes)
            total += int(np.asarray(geometry["simplified_faces"]).nbytes)
            total += int(np.asarray(geometry["simplified_uv"]).nbytes)
            total += int(np.asarray(texture["full_texture"]).nbytes)
            total += int(np.asarray(texture["block_texture"]).nbytes)
            total += int(np.asarray(texture["base_colors"]).nbytes)
            total += int(np.asarray(texture["weight_sums"]).nbytes)
        return total

    def _self_check(
        self,
        scene_runs: dict[str, SceneRun],
        affected_scenes: set[str],
    ) -> tuple[dict[str, Any], set[str], set[str]]:
        all_scenes = set(self.scene_map)
        issues: list[str] = []
        stage_recomputed: dict[str, list[str]] = {}
        stage_reused: dict[str, list[str]] = {}
        stage_sets: dict[str, list[str]] = {}

        for stage in STAGE_NAMES:
            scene_ids = {
                scene_id
                for scene_id, run in scene_runs.items()
                if stage in run.artifacts
            }
            stage_sets[stage] = sorted(scene_ids)
            recomputed = {
                scene_id
                for scene_id, run in scene_runs.items()
                if run.artifacts[stage].source == "computed"
            }
            reused = scene_ids - recomputed
            stage_recomputed[stage] = sorted(recomputed)
            stage_reused[stage] = sorted(reused)
            if scene_ids != all_scenes:
                missing = sorted(all_scenes - scene_ids)
                extra = sorted(scene_ids - all_scenes)
                issues.append(
                    f"stage {stage} scene set mismatch: missing={missing}, extra={extra}"
                )
            if recomputed & reused:
                issues.append(f"stage {stage} recomputed/reused sets overlap")
            if not affected_scenes.issubset(recomputed | reused):
                issues.append(f"stage {stage} does not cover all affected scenes")

        globally_recomputed = {
            scene_id
            for scene_id, run in scene_runs.items()
            if any(
                artifact.source == "computed"
                for artifact in run.artifacts.values()
            )
        }
        globally_reused = all_scenes - globally_recomputed
        if globally_recomputed & globally_reused:
            issues.append("global recomputed/reused sets overlap")
        if not affected_scenes.issubset(globally_recomputed | globally_reused):
            issues.append("global primitive sets do not cover affected scenes")

        report = {
            "passed": not issues,
            "issues": issues,
            "all_stage_scene_sets_equal": all(
                set(values) == all_scenes for values in stage_sets.values()
            ),
            "recomputed_reused_disjoint": not any(
                set(stage_recomputed[stage]) & set(stage_reused[stage])
                for stage in STAGE_NAMES
            )
            and not bool(globally_recomputed & globally_reused),
            "affected_scenes_covered": affected_scenes.issubset(
                globally_recomputed | globally_reused
            ),
            "stage_scene_sets": stage_sets,
        }
        if issues:
            raise SelfCheckFailed(issues, self_check=report)
        return report, globally_recomputed, globally_reused

    def _run_locked(self, current: Snapshot | None) -> RunResult:
        old_scene_states = {} if current is None else current.scenes
        config_changed = bool(
            current is not None and current.config_digest != self.config.digest
        )
        changes: dict[str, _ChangeInfo] = {}
        for scene in self.scenes:
            old_state = old_scene_states.get(scene.scene_id)
            change = self._classify_change(scene, old_state)
            if config_changed and old_state is not None:
                change = _ChangeInfo(
                    "configuration_replacement",
                    change.reasons + ("configuration_changed",),
                    0,
                )
            changes[scene.scene_id] = change

        resource_flags: list[dict[str, Any]] = []
        effective_budgets: dict[str, int] = {}
        scene_degraded: dict[str, bool] = {}
        for scene in self.scenes:
            effective, degraded = self._effective_face_budget(scene, resource_flags)
            effective_budgets[scene.scene_id] = effective
            scene_degraded[scene.scene_id] = degraded

        effective_parallelism = min(
            int(self.config.parallel_workers),
            int(self.config.limits.max_parallel_recompute),
        )
        if (
            int(self.config.parallel_workers)
            > int(self.config.limits.max_parallel_recompute)
        ):
            if not self.config.limits.allow_degrade:
                raise ResourceLimitExceeded(
                    resource="parallel_recompute_scenes",
                    limit=int(self.config.limits.max_parallel_recompute),
                    actual=int(self.config.parallel_workers),
                )
            resource_flags.append(
                {
                    "resource": "parallel_recompute_scenes",
                    "degraded": True,
                    "requested": int(self.config.parallel_workers),
                    "effective": int(effective_parallelism),
                }
            )

        scene_runs: dict[str, SceneRun] = {}
        for scene in self.scenes:
            old_state = old_scene_states.get(scene.scene_id)
            run = self._compute_scene(
                scene,
                changes[scene.scene_id],
                old_state if isinstance(old_state, dict) else None,
                effective_budgets[scene.scene_id],
            )
            run.resource_degraded = scene_degraded[scene.scene_id]
            scene_runs[scene.scene_id] = run

        resident_bytes = self._estimate_resident_bytes(scene_runs)
        if resident_bytes > int(self.config.limits.max_resident_bytes):
            raise ResourceLimitExceeded(
                resource="resident_mesh_texture_bytes",
                limit=int(self.config.limits.max_resident_bytes),
                actual=resident_bytes,
            )

        affected_scenes = {
            scene_id
            for scene_id, change in changes.items()
            if change.change_type != "unchanged"
        }
        self_check, globally_recomputed, globally_reused = self._self_check(
            scene_runs, affected_scenes
        )
        full_replay_verified: bool | None = None
        if current is not None and self.verify_incremental:
            full_replay_verified = self._verify_full_replay(scene_runs)

        run_mode = "baseline" if current is None else "incremental"
        snapshot_states: dict[str, Any] = {}
        scene_summaries: dict[str, Any] = {}
        for scene in self.scenes:
            run = scene_runs[scene.scene_id]
            snapshot_states[scene.scene_id] = _snapshot_scene_state(
                scene,
                effective_face_budget=run.effective_face_budget,
                resource_degraded=run.resource_degraded,
                accepted=run.accepted,
                artifacts=run.artifacts,
            )
            summary = run.artifacts["summary_verify"].payload
            scene_summaries[scene.scene_id] = {
                "scene_revision": int(scene.scene_revision),
                "change_type": run.change_type,
                "change_reasons": list(run.change_reasons),
                "affected_stages": list(run.affected_stages),
                "accepted": bool(run.accepted),
                "resource_degraded": bool(run.resource_degraded),
                "stage_sources": {
                    stage: run.artifacts[stage].source for stage in STAGE_NAMES
                },
                "stage_checksums": {
                    stage: run.artifacts[stage].checksum for stage in STAGE_NAMES
                },
                "summary": summary["summary"],
                "checks": summary["checks"],
            }

        snapshot_version = 1 if current is None else current.version + 1
        snapshot_summary = {
            "run_mode": run_mode,
            "affected_scenes": sorted(affected_scenes),
            "recomputed_scenes": sorted(globally_recomputed),
            "reused_scenes": sorted(globally_reused),
            "self_check_passed": bool(self_check["passed"]),
            "full_replay_verified": full_replay_verified,
        }
        snapshot = Snapshot.create(
            version=snapshot_version,
            config_digest=self.config.digest,
            scenes=snapshot_states,
            summary=snapshot_summary,
        )
        commit_result = self.snapshot_store.commit(
            snapshot,
            gate=self.snapshot_store.gate,
            gate_held=True,
        )

        stage_recomputed = {
            stage: sorted(
                scene_id
                for scene_id, run in scene_runs.items()
                if run.artifacts[stage].source == "computed"
            )
            for stage in STAGE_NAMES
        }
        stage_reused = {
            stage: sorted(
                scene_id
                for scene_id, run in scene_runs.items()
                if run.artifacts[stage].source != "computed"
            )
            for stage in STAGE_NAMES
        }
        report: dict[str, Any] = {
            "format": "sceneopt-run-report-v1",
            "run_mode": run_mode,
            "manifest_path": self.manifest_path,
            "snapshot_path": self.snapshot_store.path,
            "snapshot_version": int(snapshot.version),
            "snapshot_checksum": snapshot.checksum,
            "snapshot_replaced": bool(commit_result.replaced),
            "snapshot_checksum_changed": bool(commit_result.checksum_changed),
            "config_digest": self.config.digest,
            "scene_ids": sorted(self.scene_map),
            "affected_scenes": sorted(affected_scenes),
            "recomputed_scenes": sorted(globally_recomputed),
            "reused_scenes": sorted(globally_reused),
            "stage_recomputed": stage_recomputed,
            "stage_reused": stage_reused,
            "self_check": self_check,
            "full_replay_verified": full_replay_verified,
            "resource": {
                "resident_mesh_texture_bytes": int(resident_bytes),
                "max_resident_bytes": int(self.config.limits.max_resident_bytes),
                "max_faces_per_scene": int(self.config.limits.max_faces_per_scene),
                "max_parallel_recompute": int(
                    self.config.limits.max_parallel_recompute
                ),
                "requested_parallelism": int(self.config.parallel_workers),
                "effective_parallelism": int(effective_parallelism),
                "degradations": resource_flags,
                "degraded": bool(resource_flags),
            },
            "scenes": scene_summaries,
        }
        return RunResult(
            snapshot=snapshot,
            commit_result=commit_result,
            report=report,
            scene_runs=scene_runs,
            cache=self.cache,
            snapshot_store=self.snapshot_store,
        )

