"""Scene-revision change detection and dependency-failure propagation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .hashing import checksum_json
from .manifest import SceneManifest
from .snapshot import SnapshotStore

# Each stage depends on the listed input groups; downstream stages inherit
# invalidation automatically because they consume upstream artifacts.
STAGE_ORDER: Tuple[str, ...] = ("ingest", "registration", "geometry", "texture")
STAGE_INPUTS: Dict[str, Tuple[str, ...]] = {
    # Ingest validates stored observations against their seeds; geometry mode
    # only reshapes fusion, so it does not invalidate the ingest snapshot.
    "ingest": ("observations", "point_cloud_seed"),
    "registration": ("camera_poses", "point_cloud_seed", "icp_config"),
    "geometry": ("geometry_mode", "target_face_budget", "geometry_config"),
    "texture": ("texture_seed", "camera_observations", "texture_config"),
}


def manifest_fingerprint(manifest: SceneManifest,
                         camera_observation_ids: Optional[List[str]] = None
                         ) -> Dict[str, str]:
    """Content fingerprints of the input groups that drive each stage."""
    camera_observation_ids = camera_observation_ids or [
        camera.camera_id for camera in manifest.camera_poses
    ]
    return {
        "observations": checksum_json({
            "camera_ids": sorted(camera_observation_ids),
            "point_cloud_seed": manifest.point_cloud_seed,
        }),
        "geometry_mode": checksum_json(manifest.geometry_mode),
        "point_cloud_seed": checksum_json(manifest.point_cloud_seed),
        "camera_poses": checksum_json([
            camera.to_dict() for camera in manifest.camera_poses
        ]),
        "icp_config": checksum_json("icp-config-handled-by-scheduler"),
        "target_face_budget": checksum_json(manifest.target_face_budget),
        "geometry_config": checksum_json("geometry-config-handled-by-scheduler"),
        "texture_seed": checksum_json(manifest.texture_seed),
        "camera_observations": checksum_json({
            "replaced_cameras": sorted(camera_observation_ids),
            "texture_seed": manifest.texture_seed,
        }),
        "texture_config": checksum_json("texture-config-handled-by-scheduler"),
        "revision": checksum_json(manifest.scene_revision),
    }


@dataclass
class IncrementalPlan:
    scene_id: str
    previous_revision: int
    current_revision: int
    changed_inputs: List[str]
    invalidated_stages: List[str]
    reused_stages: List[str]
    recomputed_stages: List[str]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "previous_revision": int(self.previous_revision),
            "current_revision": int(self.current_revision),
            "changed_inputs": list(self.changed_inputs),
            "invalidated_stages": list(self.invalidated_stages),
            "reused_stages": list(self.reused_stages),
            "recomputed_stages": list(self.recomputed_stages),
            "reason": self.reason,
        }


def plan_incremental(previous: Optional[SceneManifest],
                     current: SceneManifest,
                     store: SnapshotStore,
                     replaced_cameras: Optional[List[str]] = None,
                     previous_fingerprints: Optional[Dict[str, str]] = None,
                     config_fingerprint: Optional[Dict[str, str]] = None
                     ) -> IncrementalPlan:
    """Compute the invalidated suffix and the reusable upstream stages."""
    current_fp = manifest_fingerprint(
        current,
        camera_observation_ids=replaced_cameras or [
            camera.camera_id for camera in current.camera_poses
        ],
    )
    if config_fingerprint:
        current_fp.update(config_fingerprint)

    if previous is None or previous_fingerprints is None:
        changed = sorted(set(current_fp) - {"revision"})
        invalidated = list(STAGE_ORDER)
        return IncrementalPlan(
            scene_id=current.scene_id,
            previous_revision=0,
            current_revision=current.scene_revision,
            changed_inputs=changed,
            invalidated_stages=invalidated,
            reused_stages=[],
            recomputed_stages=list(STAGE_ORDER),
            reason="new scene: full recompute",
        )

    if current.scene_revision < previous.scene_revision:
        raise ValueError(
            f"scene_revision regressed for {current.scene_id}: "
            f"{current.scene_revision} < {previous.scene_revision}"
        )

    changed = sorted(
        key for key, value in current_fp.items()
        if previous_fingerprints.get(key) != value
    )
    affected_stages: Set[str] = set()
    for stage, inputs in STAGE_INPUTS.items():
        if any(input_name in changed for input_name in inputs):
            affected_stages.add(stage)

    # Propagate downstream: invalidation is a suffix of the stage order.
    invalidated: Set[str] = set()
    if affected_stages:
        earliest = min(STAGE_ORDER.index(stage) for stage in affected_stages)
        invalidated = set(STAGE_ORDER[earliest:])

    existing = {
        stage for stage in STAGE_ORDER
        if store.current_version(current.scene_id, stage) > 0
    }
    reused = [stage for stage in STAGE_ORDER
              if stage in existing and stage not in invalidated]
    recomputed = [stage for stage in STAGE_ORDER if stage in invalidated]
    reason = (
        "no input change: all stages reusable"
        if not changed else
        f"changed inputs {changed} invalidate suffix {recomputed}"
    )
    return IncrementalPlan(
        scene_id=current.scene_id,
        previous_revision=previous.scene_revision,
        current_revision=current.scene_revision,
        changed_inputs=changed,
        invalidated_stages=sorted(invalidated, key=STAGE_ORDER.index),
        reused_stages=reused,
        recomputed_stages=recomputed,
        reason=reason,
    )
