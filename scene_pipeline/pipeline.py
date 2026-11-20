"""Per-scene deterministic pipeline executed inside scheduler workers."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from .config import PipelineConfig
from .errors import StageBusyError, StageFailure, failure_from_exception
from .fault_injection import FaultPlan
from .geometry import process_geometry
from .hashing import checksum_numpy
from .memory import AdmissionDecision
from .observations import load_observation_bundle
from .registration import register_scene
from .snapshot import SnapshotStore
from .texture import process_texture


@dataclass
class StageExecution:
    stage: str
    status: str  # "computed" | "reused" | "failed" | "skipped"
    version: int
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SceneRunResult:
    scene_id: str
    success: bool
    stages: List[StageExecution]
    admission: Dict[str, Any]
    failure: Optional[StageFailure]
    reused_stages: List[str]
    recomputed_stages: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "success": self.success,
            "stages": [stage.__dict__ for stage in self.stages],
            "admission": self.admission,
            "failure": None if self.failure is None else self.failure.to_dict(),
            "reused_stages": list(self.reused_stages),
            "recomputed_stages": list(self.recomputed_stages),
        }


def _commit(store: SnapshotStore, scene_id: str, stage: str,
            payload: Dict[str, Any],
            arrays: Optional[Dict[str, np.ndarray]] = None) -> int:
    """Commit with bounded retry so parallel workers serialise on the lock.

    A genuinely concurrent, non-retried conflict still surfaces immediately as
    ``E_STAGE_BUSY`` via :class:`SnapshotStore` tests.
    """
    import time

    last_busy = None
    for attempt in range(20):
        try:
            manifest = store.try_commit(scene_id, stage, payload, arrays)
            return int(manifest["version"])
        except StageBusyError as exc:
            last_busy = exc
            time.sleep(0.01 * (attempt + 1))
    raise last_busy


def run_scene(manifest, config: PipelineConfig, observation_dir: str,
              snapshot_root: str, admission: AdmissionDecision,
              fault_path: Optional[str] = None,
              skip_stages: Optional[List[str]] = None) -> SceneRunResult:
    """Run the four-stage pipeline for one scene, reusing upstream snapshots."""
    faults = FaultPlan.load(fault_path)
    store = SnapshotStore(snapshot_root)
    skip_stages = set(skip_stages or [])
    stages: List[StageExecution] = []
    reused: List[str] = []
    recomputed: List[str] = []

    try:
        # ------------------------------------------------------------- ingest
        if "ingest" in skip_stages and store.current_version(manifest.scene_id, "ingest"):
            version = store.current_version(manifest.scene_id, "ingest")
            stages.append(StageExecution("ingest", "reused", version))
            reused.append("ingest")
            bundle = load_observation_bundle(manifest, observation_dir)
        else:
            faults.trigger(manifest.scene_id, "ingest")
            bundle = load_observation_bundle(manifest, observation_dir)
            version = _commit(
                store, manifest.scene_id, "ingest",
                {
                    "scene_revision": manifest.scene_revision,
                    "point_cloud_sha256": checksum_numpy(bundle.points),
                    "colors_sha256": checksum_numpy(bundle.colors),
                    "camera_ids": [camera.camera_id for camera in bundle.cameras],
                },
            )
            stages.append(StageExecution("ingest", "computed", version))
            recomputed.append("ingest")

        # ------------------------------------------------------ registration
        if ("registration" in skip_stages
                and store.current_version(manifest.scene_id, "registration")):
            version = store.current_version(manifest.scene_id, "registration")
            stages.append(StageExecution("registration", "reused", version))
            reused.append("registration")
            registration = register_scene(
                bundle, config.icp, admission.comparison_budget
            )
        else:
            faults.trigger(manifest.scene_id, "registration")
            registration = register_scene(
                bundle, config.icp, admission.comparison_budget
            )
            version = _commit(
                store, manifest.scene_id, "registration",
                registration.to_payload(),
                arrays={
                    "point_cloud.npy": registration.points,
                    **{
                        f"transform_{item.camera_id}.npy": item.transform
                        for item in registration.cameras
                    },
                },
            )
            stages.append(StageExecution(
                "registration", "computed", version,
                detail={"total_comparisons": registration.total_comparisons,
                        "downgraded": registration.downgraded},
            ))
            recomputed.append("registration")

        # ----------------------------------------------------------- geometry
        if ("geometry" in skip_stages
                and store.current_version(manifest.scene_id, "geometry")):
            version = store.current_version(manifest.scene_id, "geometry")
            stages.append(StageExecution("geometry", "reused", version))
            reused.append("geometry")
            geometry = process_geometry(
                registration, manifest.geometry_mode,
                admission.final_face_budget, config.geometry.grid_resolution,
                downgraded=bool(admission.downgrade_categories),
            )
        else:
            faults.trigger(manifest.scene_id, "geometry")
            geometry = process_geometry(
                registration, manifest.geometry_mode,
                admission.final_face_budget, config.geometry.grid_resolution,
                downgraded=bool(admission.downgrade_categories),
            )
            version = _commit(
                store, manifest.scene_id, "geometry",
                geometry.to_payload(),
                arrays={
                    "vertices.npy": geometry.mesh.vertices,
                    "faces.npy": geometry.mesh.faces,
                },
            )
            stages.append(StageExecution(
                "geometry", "computed", version,
                detail={"face_count": geometry.post_simplify_face_count},
            ))
            recomputed.append("geometry")

        # ------------------------------------------------------------ texture
        if ("texture" in skip_stages
                and store.current_version(manifest.scene_id, "texture")):
            version = store.current_version(manifest.scene_id, "texture")
            stages.append(StageExecution("texture", "reused", version))
            reused.append("texture")
        else:
            faults.trigger(manifest.scene_id, "texture")
            texture = process_texture(
                geometry, registration, bundle,
                resolution=admission.final_texture_resolution,
                tolerance=config.texture.tolerance,
                downgraded=bool(admission.downgrade_categories),
            )
            version = _commit(
                store, manifest.scene_id, "texture",
                texture.to_payload(),
                arrays={"texture.npy": texture.texture, "uv.npy": texture.uv},
            )
            stages.append(StageExecution(
                "texture", "computed", version,
                detail={
                    "resolution": texture.resolution,
                    "max_deviation": max(
                        report.max_deviation for report in texture.view_reports
                    ),
                },
            ))
            recomputed.append("texture")

        return SceneRunResult(
            scene_id=manifest.scene_id,
            success=True,
            stages=stages,
            admission=admission.to_dict(),
            failure=None,
            reused_stages=reused,
            recomputed_stages=recomputed,
        )
    except Exception as exc:  # structured worker failure, never kills siblings
        failure = failure_from_exception(exc, stage=_infer_stage(stages),
                                         scene_id=manifest.scene_id)
        stages.append(StageExecution(failure.stage, "failed", 0,
                                     detail={"code": failure.code}))
        return SceneRunResult(
            scene_id=manifest.scene_id,
            success=False,
            stages=stages,
            admission=admission.to_dict(),
            failure=failure,
            reused_stages=reused,
            recomputed_stages=recomputed,
        )


def _infer_stage(stages: List[StageExecution]) -> str:
    order = ("ingest", "registration", "geometry", "texture")
    done = {stage.stage for stage in stages}
    for stage in order:
        if stage not in done:
            return stage
    return "unknown"
