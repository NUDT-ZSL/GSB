"""Independent snapshot verification and result cross-checking tool."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

import numpy as np

from .config import PipelineConfig
from .errors import InconsistentError
from .hashing import checksum_numpy
from .incremental import STAGE_ORDER
from .manifest import SceneManifest
from .observations import load_observation_bundle
from .registration import register_scene
from .snapshot import SnapshotStore
from .texture import check_consistency
from .geometry import Mesh


@dataclass
class SceneVerification:
    scene_id: str
    ok: bool
    checks: Dict[str, str] = field(default_factory=dict)
    issues: List[Dict[str, str]] = field(default_factory=list)


def verify_store(store: SnapshotStore, manifests: List[SceneManifest],
                 observation_dir: str, config: PipelineConfig
                 ) -> Dict[str, Any]:
    """Recompute every conclusion independently from raw observations."""
    manifest_by_id = {item.scene_id: item for item in manifests}
    verifications: List[SceneVerification] = []

    stage_sets = {
        stage: set(store.stage_scene_ids(stage)) for stage in STAGE_ORDER
    }
    expected_set = set(manifest_by_id)
    for stage, actual_set in stage_sets.items():
        missing = sorted(expected_set - actual_set)
        extra = sorted(actual_set - expected_set)
        if missing or extra:
            raise InconsistentError(
                "scene_id sets differ across stages",
                stage=stage,
                details={
                    "category": "scene_id_set",
                    "stage": stage,
                    "missing": missing,
                    "extra": extra,
                },
            )

    for scene_id in sorted(expected_set):
        manifest = manifest_by_id[scene_id]
        verification = SceneVerification(scene_id=scene_id, ok=True)
        try:
            for stage in STAGE_ORDER:
                store.verify(scene_id, stage)
                verification.checks[stage] = "checksum_ok"

            bundle = load_observation_bundle(manifest, observation_dir)
            registration_manifest = store.load_manifest(scene_id, "registration")
            live_registration = register_scene(
                bundle, config.icp,
                registration_manifest["payload"]["comparison_budget"],
            )
            stored_residual = registration_manifest["payload"]["mean_residual"]
            if not np.isclose(stored_residual, live_registration.mean_residual,
                              rtol=1e-10, atol=1e-10):
                raise InconsistentError(
                    "registration residual cannot be reproduced",
                    scene_id=scene_id, stage="registration",
                    details={
                        "category": "residual",
                        "stored": stored_residual,
                        "recomputed": live_registration.mean_residual,
                    },
                )
            stored_points = store.load_array(
                scene_id, "registration", "point_cloud.npy"
            )
            if checksum_numpy(stored_points) != checksum_numpy(bundle.points):
                raise InconsistentError(
                    "registration point cloud differs from ingest point cloud",
                    scene_id=scene_id, stage="registration",
                    details={"category": "point_cloud"},
                )
            verification.checks["registration_residual"] = "recomputed_ok"

            geometry_manifest = store.load_manifest(scene_id, "geometry")
            faces = store.load_array(scene_id, "geometry", "faces.npy")
            vertices = store.load_array(scene_id, "geometry", "vertices.npy")
            budget = geometry_manifest["payload"]["face_budget"]
            if int(faces.shape[0]) > int(budget):
                raise InconsistentError(
                    "mesh face count exceeds stored budget",
                    scene_id=scene_id, stage="geometry",
                    details={
                        "category": "face_budget",
                        "face_count": int(faces.shape[0]),
                        "budget": int(budget),
                    },
                )
            verification.checks["face_budget"] = "within_budget"

            texture_manifest = store.load_manifest(scene_id, "texture")
            texture = store.load_array(scene_id, "texture", "texture.npy")
            uv = store.load_array(scene_id, "texture", "uv.npy")
            mesh = Mesh(vertices=vertices, faces=faces)
            transforms = {
                item.camera_id: np.asarray(item.transform, dtype=np.float64)
                for item in live_registration.cameras
            }
            reports = check_consistency(
                mesh, uv, texture, bundle, transforms
            )
            stored_views = {
                item["camera_id"]: item
                for item in texture_manifest["payload"]["view_reports"]
            }
            for report in reports:
                stored = stored_views[report.camera_id]
                if not np.isclose(report.max_deviation,
                                  stored["max_deviation"], rtol=1e-10, atol=1e-10):
                    raise InconsistentError(
                        "texture consistency conclusion cannot be reproduced",
                        scene_id=scene_id, stage="texture",
                        details={
                            "category": "texture_consistency",
                            "camera_id": report.camera_id,
                            "stored": stored["max_deviation"],
                            "recomputed": report.max_deviation,
                        },
                    )
                if report.max_deviation > texture_manifest["payload"]["tolerance"]:
                    raise InconsistentError(
                        "stored texture snapshot violates its own tolerance",
                        scene_id=scene_id, stage="texture",
                        details={"category": "texture_tolerance",
                                 "camera_id": report.camera_id},
                    )
            verification.checks["texture_consistency"] = "recomputed_ok"
        except InconsistentError as exc:
            verification.ok = False
            verification.issues.append(exc.details or {"message": exc.message})
        verifications.append(verification)

    return {
        "all_ok": all(item.ok for item in verifications),
        "scene_count": len(verifications),
        "scenes": [
            {
                "scene_id": item.scene_id,
                "ok": item.ok,
                "checks": item.checks,
                "issues": item.issues,
            }
            for item in verifications
        ],
    }
