"""End-to-end deterministic rendering and verification pipeline."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .animation import SkinResult, SkinnedHand
from .collision import IntersectionDetector
from .deformation import DeformationAuditor
from .png import png_bytes, write_png
from .project import KeyMoment, Project, load_key_moments, load_project
from .sdf import SDFScene
from .store import VersionedArtifactStore, sha256_bytes, sha256_file, write_json
from .tracer import (
    TRACE_ERR_HIT_BEYOND_FAR_PLANE,
    TRACE_ERR_MAX_STEPS,
    TRACE_HIT,
    TRACE_MISS,
    TRACE_STATUS_NAMES,
    SphereTracer,
    TraceResult,
)


@dataclass(frozen=True)
class PipelineResult:
    output_root: Path
    version_dir: Path
    version: str
    report: dict[str, Any]


def _matrix_list(matrix: np.ndarray) -> list[list[float]]:
    return [[float(value) for value in row] for row in np.asarray(matrix)]


def _trace_digest(trace: TraceResult) -> str:
    digest = hashlib.sha256()
    digest.update(np.ascontiguousarray(trace.hit_t, dtype="<f8").tobytes())
    digest.update(np.ascontiguousarray(trace.status, dtype="i1").tobytes())
    digest.update(np.ascontiguousarray(trace.normals, dtype="<f8").tobytes())
    return digest.hexdigest()


def _write_frame_data(
    directory: Path,
    project: Project,
    frame: int,
    label: str,
    skin_result: SkinResult,
    scene: SDFScene,
) -> str:
    relative = f"skinning/frame_{frame:04d}.json"
    primitives = {}
    for primitive in scene.primitives:
        lower, upper = primitive.aabb()
        primitives[primitive.id] = {
            "type": primitive.spec.kind,
            "bone": primitive.bone,
            "world_matrix": _matrix_list(primitive.world_matrix),
            "world_aabb": {
                "lower": [float(value) for value in lower],
                "upper": [float(value) for value in upper],
            },
        }
    payload = {
        "schema_version": "1.0",
        "project_id": project.project_id,
        "project_version": project.project_version,
        "frame": frame,
        "time_seconds": skin_result.time_seconds,
        "moment": label,
        "bones": {
            name: {"world_matrix": _matrix_list(skin_result.world_matrices[name])}
            for name in project.joint_order
        },
        "skinned_vertices": [
            [float(value) for value in vertex]
            for vertex in skin_result.skinned_vertices
        ],
        "sdf_primitives": primitives,
    }
    write_json(directory / relative, payload)
    return relative


def _write_trace_arrays(directory: Path, frame: int, trace: TraceResult) -> dict[str, str]:
    trace_dir = directory / "trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "hit_t": f"trace/frame_{frame:04d}_hit_t.npy",
        "status": f"trace/frame_{frame:04d}_status.npy",
        "normals": f"trace/frame_{frame:04d}_normals.npy",
    }
    np.save(directory / files["hit_t"], trace.hit_t, allow_pickle=False)
    np.save(directory / files["status"], trace.status, allow_pickle=False)
    np.save(directory / files["normals"], trace.normals, allow_pickle=False)
    return files


def _status_legend() -> dict[str, dict[str, Any]]:
    meanings = {
        TRACE_HIT: "First surface crossing was found and shaded.",
        TRACE_MISS: "Ray left the scene without a surface crossing.",
        TRACE_ERR_MAX_STEPS: "Ray exhausted max_steps before a terminal crossing.",
        TRACE_ERR_HIT_BEYOND_FAR_PLANE: (
            "A surface crossing was found beyond the camera far plane."
        ),
    }
    return {
        name: {"code": code, "meaning": meanings[code]}
        for code, name in TRACE_STATUS_NAMES.items()
    }


def _canonical_records(records: list[dict[str, Any]]) -> bytes:
    return json.dumps(records, sort_keys=True, ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    )


def _repeatability(
    project: Project,
    skinner: SkinnedHand,
    detector: IntersectionDetector,
    moment: KeyMoment,
    reference_image: np.ndarray,
    reference_trace: TraceResult,
    reference_collisions: list[dict[str, Any]],
) -> dict[str, Any]:
    settings = project.raw.get("repeatability", {})
    hit_tolerance = float(settings.get("hit_t_tolerance", 1e-10))
    normal_tolerance = float(settings.get("normal_tolerance", 1e-10))
    skin_result = skinner.skin(moment.frame)
    scene = SDFScene.from_project(project, skin_result.world_matrices)
    image, trace = SphereTracer(scene, project.camera, project.render).render()
    collisions = detector.detect(scene, moment.frame, moment.label)

    same_hit_mask = np.array_equal(
        reference_trace.status == TRACE_HIT,
        trace.status == TRACE_HIT,
    )
    same_statuses = np.array_equal(reference_trace.status, trace.status)
    if same_hit_mask and same_statuses:
        hit_t_delta = float(
            np.max(
                np.abs(
                    reference_trace.hit_t[reference_trace.status == TRACE_HIT]
                    - trace.hit_t[trace.status == TRACE_HIT]
                ),
                initial=0.0,
            )
        )
        normal_delta = float(
            np.max(
                np.abs(
                    reference_trace.normals[reference_trace.status == TRACE_HIT]
                    - trace.normals[trace.status == TRACE_HIT]
                ),
                initial=0.0,
            )
        )
    else:
        hit_t_delta = float("inf")
        normal_delta = float("inf")
    image_equal = np.array_equal(reference_image, image)
    collisions_equal = _canonical_records(reference_collisions) == _canonical_records(
        collisions
    )
    passed = bool(
        same_statuses
        and same_hit_mask
        and hit_t_delta <= hit_tolerance
        and normal_delta <= normal_tolerance
        and image_equal
        and collisions_equal
    )
    return {
        "frame": moment.frame,
        "moment": moment.label,
        "statuses_identical": bool(same_statuses),
        "hit_mask_identical": bool(same_hit_mask),
        "max_hit_t_delta": hit_t_delta,
        "max_normal_component_delta": normal_delta,
        "image_bytes_identical": bool(image_equal),
        "intersections_identical": bool(collisions_equal),
        "tolerances": {
            "hit_t": hit_tolerance,
            "normal_component": normal_tolerance,
        },
        "passed": passed,
    }


def run_pipeline(
    project_path: str | Path,
    key_moments_path: str | Path,
    output_dir: str | Path,
    version: str | None = None,
    repeat_check: bool = True,
    progress: Callable[[str], None] | None = None,
) -> PipelineResult:
    project_source = Path(project_path)
    key_source = Path(key_moments_path)
    project = load_project(project_source)
    key_moments = load_key_moments(key_source)
    frame_set = set(project.frames)
    for moment in key_moments:
        if moment.frame not in frame_set:
            raise ValueError(
                f"key moment frame {moment.frame} is outside {project.frame_start}..{project.frame_end}"
            )

    selected_version = version or project.project_version
    store = VersionedArtifactStore(Path(output_dir))
    staging = store.begin(selected_version)
    reporter = progress or (lambda message: None)

    shutil.copyfile(project_source, staging / "project.json")
    shutil.copyfile(key_source, staging / "key_moments.json")
    project_sha = sha256_file(project_source)
    key_sha = sha256_file(key_source)

    sampler = SkinnedHand(project)
    rest_scene = SDFScene.rest_scene(project, sampler.sampler.rest_world_matrices)
    detector = IntersectionDetector(project.collision_config)
    auditor = DeformationAuditor(project, sampler, rest_scene)
    key_by_frame = {moment.frame: moment for moment in key_moments}

    frame_records: list[dict[str, Any]] = []
    collision_records: list[dict[str, Any]] = []
    key_skin_results: dict[int, SkinResult] = {}
    key_scenes: dict[int, SDFScene] = {}
    collision_by_frame: dict[int, list[dict[str, Any]]] = {}
    repeat_reference = None

    for frame in project.frames:
        moment = key_by_frame.get(frame)
        label = moment.label if moment else ""
        skin_result = sampler.skin(frame)
        scene = SDFScene.from_project(project, skin_result.world_matrices)
        image, trace = SphereTracer(scene, project.camera, project.render).render()
        image_relative = f"render/frame_{frame:04d}.png"
        write_png(staging / image_relative, image)
        trace_files = _write_trace_arrays(staging, frame, trace)
        skin_relative = _write_frame_data(
            staging, project, frame, label, skin_result, scene
        )
        counts = trace.status_counts()
        frame_records.append(
            {
                "frame": frame,
                "moment": label,
                "time_seconds": skin_result.time_seconds,
                "image": image_relative,
                "image_sha256": sha256_bytes(png_bytes(image)),
                "skinning_data": skin_relative,
                "trace_arrays": trace_files,
                "geometry_digest": _trace_digest(trace),
                "status_counts": counts,
            }
        )
        if moment is not None:
            key_skin_results[frame] = skin_result
            key_scenes[frame] = scene
            frame_collisions = detector.detect(scene, frame, label)
            collision_by_frame[frame] = frame_collisions
            collision_records.extend(frame_collisions)
            if repeat_check and (
                repeat_reference is None
                or len(frame_collisions) > len(repeat_reference[3])
            ):
                # Prefer a collision-rich key moment so repeatability also
                # exercises the non-empty intersection path.
                repeat_reference = (moment, image, trace, frame_collisions)
        reporter(
            f"frame {frame:04d}: HIT={counts['HIT']} "
            f"ERR_MAX_STEPS={counts['ERR_TRACE_MAX_STEPS']} "
            f"ERR_BEYOND_FAR={counts['ERR_HIT_BEYOND_FAR_PLANE']}"
        )

    audit_frames = []
    for moment in key_moments:
        result = key_skin_results[moment.frame]
        audit_frames.append(
            auditor.audit_frame(
                moment.frame,
                moment.label,
                result,
                key_scenes[moment.frame],
            )
        )
    deformation_report = auditor.report(audit_frames)

    status_totals = {
        name: sum(record["status_counts"][name] for record in frame_records)
        for name in TRACE_STATUS_NAMES.values()
    }
    error_count = (
        status_totals["ERR_TRACE_MAX_STEPS"]
        + status_totals["ERR_HIT_BEYOND_FAR_PLANE"]
    )
    render_manifest = {
        "schema_version": "1.0",
        "project_id": project.project_id,
        "project_version": project.project_version,
        "frame_range": [project.frame_start, project.frame_end],
        "fps": project.fps,
        "camera": {
            "mode": project.camera.mode,
            "position": [float(value) for value in project.camera.position],
            "target": [float(value) for value in project.camera.target],
            "up": [float(value) for value in project.camera.up],
            "near": project.camera.near,
            "far": project.camera.far,
            "half_width": project.camera.half_width,
            "half_height": project.camera.half_height,
        },
        "tracing": {
            "width": project.render.width,
            "height": project.render.height,
            "max_steps": project.render.max_steps,
            "epsilon": project.render.epsilon,
            "step_safety": project.render.step_safety,
            "min_step": project.render.min_step,
            "max_step": project.render.max_step,
            "gradient_epsilon": project.render.gradient_epsilon,
            "smooth_union_radius": project.render.smooth_union_radius,
        },
        "status_legend": _status_legend(),
        "status_totals": status_totals,
        "render_status": "OK" if error_count == 0 else "REVIEW",
        "frames": frame_records,
    }
    write_json(staging / "render_manifest.json", render_manifest)

    intersection_payload = {
        "schema_version": "1.0",
        "project_id": project.project_id,
        "project_version": project.project_version,
        "method": (
            "For each distinct-bone primitive pair, the overlap SDF is "
            "max(dA, dB); a negative minimum is reported as penetration depth."
        ),
        "key_moments": [
            {
                "frame": moment.frame,
                "label": moment.label,
                "intersection_count": len(collision_by_frame.get(moment.frame, [])),
            }
            for moment in key_moments
        ],
        "summary": {
            "intersection_count": len(collision_records),
            "status": "INTERSECTIONS_FOUND" if collision_records else "NO_INTERSECTIONS",
        },
        "intersections": collision_records,
    }
    write_json(staging / "intersections.json", intersection_payload)
    write_json(staging / "deformation_report.json", deformation_report)

    if repeat_reference is not None:
        moment, image, trace, collisions = repeat_reference
        repeatability = _repeatability(
            project,
            sampler,
            detector,
            moment,
            image,
            trace,
            collisions,
        )
    else:
        repeatability = {
            "enabled": False,
            "passed": True,
            "reason": "repeat_check disabled or no key moments",
        }
    write_json(staging / "repeatability.json", repeatability)

    run_report = {
        "schema_version": "1.0",
        "project_id": project.project_id,
        "project_version": project.project_version,
        "run_status": "COMPLETE" if error_count == 0 else "REVIEW",
        "render_status": render_manifest["render_status"],
        "trace_error_pixels": error_count,
        "intersection_count": len(collision_records),
        "deformation_status": deformation_report["summary"]["status"],
        "repeatability_passed": bool(repeatability["passed"]),
        "artifacts": {
            "render_manifest": "render_manifest.json",
            "intersections": "intersections.json",
            "deformation_report": "deformation_report.json",
            "repeatability": "repeatability.json",
            "skinning_frames": "skinning/",
            "rendered_frames": "render/",
            "trace_arrays": "trace/",
        },
    }
    write_json(staging / "run_report.json", run_report)
    write_json(
        staging / "version_info.json",
        {
            "schema_version": "1.0",
            "project_id": project.project_id,
            "project_version": project.project_version,
            "project_sha256": project_sha,
            "key_moments_sha256": key_sha,
            "frame_count": len(frame_records),
            "trace_error_pixels": error_count,
        },
    )
    destination, _ = store.commit(staging, selected_version)
    run_report["version_directory"] = str(destination)
    return PipelineResult(
        output_root=Path(output_dir),
        version_dir=destination,
        version=selected_version,
        report=run_report,
    )

