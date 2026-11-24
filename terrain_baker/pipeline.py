from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .cache import ChunkCache, write_interrupt_marker
from .errors import BakeInterrupted, RenderTaskError
from .graph import (
    build_chunks,
    build_invalidation_matrix,
    camera_digest,
    classify_invalidation,
    compute_dependency_digest,
    compute_global_version,
    dependency_graph_descriptor,
    lighting_digest,
    march_digest,
)
from .heightfield import GlobalHeightField
from .io_utils import atomic_write_json, canonical_digest
from .outputs import depth_to_uint16_preview, publish_gray_png, publish_npy, publish_npz, publish_file
from .png_codec import encode_gray_png
from .renderer import estimate_sample_buffer_bytes, render_chunk, render_rect
from .scheduler import BoundarySignal, run_bounded_chunks
from .seams import assemble_grid, compare_to_reference


def _sanitize_scene(scene: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in scene.items() if not key.startswith("_")}


def detect_changed_keys(old: Dict[str, Any], new: Dict[str, Any], section: str) -> List[str]:
    old_section = old.get(section, {}) if old else {}
    new_section = new.get(section, {})
    keys = sorted(set(old_section) | set(new_section))
    changed = []
    for key in keys:
        if canonical_digest(old_section.get(key)) != canonical_digest(new_section.get(key)):
            changed.append(key)
    return changed


def bake_scene(
    scene: Dict[str, Any],
    heightfield: GlobalHeightField,
    *,
    output_dir: str | Path,
    state_dir: str | Path | None = None,
    report_path: str | Path | None = None,
    mode: str = "auto",
    edit: Optional[Dict[str, Any]] = None,
    changed_keys: Optional[Sequence[str]] = None,
    max_workers_override: Optional[int] = None,
    memory_budget_override: Optional[int] = None,
    run_reference: bool = True,
    signal_token: Optional[BoundarySignal] = None,
    stop_after_completed: Optional[int] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    state_root = Path(state_dir) if state_dir else output_root / "state"
    report_file = Path(report_path) if report_path else output_root / scene["output"]["report"]
    report_file.parent.mkdir(parents=True, exist_ok=True)

    resolution = scene["resolution"]
    width = int(resolution["width"])
    height = int(resolution["height"])
    chunks = build_chunks(
        width,
        height,
        int(resolution["tile_width"]),
        int(resolution["tile_height"]),
    )
    global_version = compute_global_version(scene, heightfield)
    run_identifier = run_id or uuid.uuid4().hex[:12]

    edit_regions = list(edit.get("regions", [])) if edit else []
    effective_mode = mode
    effective_changed_keys = list(changed_keys or [])
    last_run_path = state_root / "last_run.json"
    previous_run = None
    if last_run_path.exists():
        try:
            import json

            previous_run = json.loads(last_run_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous_run = None

    if effective_mode == "auto":
        if previous_run:
            previous_scene = previous_run.get("scene", {})
            camera_keys = detect_changed_keys(previous_scene, scene, "camera")
            light_keys = detect_changed_keys(previous_scene, scene, "lighting")
            march_keys = detect_changed_keys(previous_scene, scene, "ray_march")
            if camera_keys:
                effective_mode = "camera"
                effective_changed_keys = camera_keys
            elif light_keys:
                effective_mode = "lighting"
                effective_changed_keys = light_keys
            elif march_keys:
                effective_mode = "ray_step"
                effective_changed_keys = march_keys
            else:
                effective_mode = "initial"
        else:
            effective_mode = "initial"

    with ChunkCache(state_root) as cache:
        cached_chunks: Dict[str, Any] = {}
        cache_metadata: Dict[str, Optional[Dict[str, Any]]] = {}
        current_digests: Dict[str, Optional[str]] = {}
        for chunk in chunks:
            cached = cache.load_chunk(chunk.chunk_id)
            cached_chunks[chunk.chunk_id] = cached
            metadata = cached.metadata if cached else cache.load_metadata(chunk.chunk_id)
            cache_metadata[chunk.chunk_id] = metadata
            if metadata and metadata.get("height_dependency_range"):
                current_digests[chunk.chunk_id] = compute_dependency_digest(
                    scene,
                    heightfield,
                    chunk,
                    metadata["height_dependency_range"],
                )
            else:
                current_digests[chunk.chunk_id] = None

        decisions = classify_invalidation(
            chunks,
            cache_metadata,
            current_digests,
            mode=effective_mode,
            edit_regions=edit_regions,
            changed_keys=effective_changed_keys,
        )
        decisions_by_id = {item["chunk_id"]: item for item in decisions}
        invalid_chunks = [chunk for chunk in chunks if decisions_by_id[chunk.chunk_id]["invalid"]]

        completed_render_count = 0
        count_lock = __import__("threading").Lock()

        def render_and_persist(chunk):
            nonlocal completed_render_count
            result = render_chunk(scene, heightfield, chunk)
            dependency_digest = compute_dependency_digest(
                scene, heightfield, chunk, result.dependency_range
            )
            dependency = dependency_graph_descriptor(
                chunk, result.dependency_range, dependency_digest
            )
            dependency["global_grid_coverage"] = result.coverage_range
            dependency["camera_digest"] = camera_digest(scene)
            dependency["lighting_digest"] = lighting_digest(scene)
            dependency["march_digest"] = march_digest(scene)
            arrays = {
                "gray": result.gray,
                "shade": result.shade,
                "depth": result.depth,
                "height": result.height,
                "normal": result.normal,
                "hit": result.hit,
            }
            metadata = cache.save_chunk(
                chunk_id=chunk.chunk_id,
                chunk_index=chunk.index,
                global_version=global_version,
                dependency_digest=dependency_digest,
                dependency=dependency,
                arrays=arrays,
            )
            with count_lock:
                completed_render_count += 1
                if (
                    stop_after_completed is not None
                    and completed_render_count >= int(stop_after_completed)
                    and signal_token is not None
                ):
                    signal_token.request_stop(-1)
            return {
                "chunk_id": chunk.chunk_id,
                "arrays": arrays,
                "metadata": metadata,
                "dependency_range": result.dependency_range,
                "coverage_range": result.coverage_range,
                "sample_buffer_bytes": result.sample_buffer_bytes,
            }

        scheduler_config = scene["scheduler"]
        workers = max_workers_override or int(scheduler_config["max_workers"])
        memory_budget = memory_budget_override or int(scheduler_config["memory_budget_bytes"])
        outcome = run_bounded_chunks(
            invalid_chunks,
            max_workers=workers,
            memory_budget_bytes=memory_budget,
            buffer_size_func=lambda chunk: estimate_sample_buffer_bytes(chunk.width, chunk.height),
            task_func=render_and_persist,
            signal_token=signal_token,
        )

        successful_by_id = {item["chunk_id"]: item for item in outcome.successes}
        if outcome.first_error is not None:
            partial_report = {
                "ok": False,
                "status": "partial_failure",
                "error": {
                    "code": "E_RENDER",
                    "message": str(outcome.first_error),
                    "scheduler_errors": outcome.errors,
                },
                "global_version": global_version,
                "successful_chunk_ids": sorted(successful_by_id),
                "cancelled_chunk_ids": outcome.cancelled,
                "chunk_decisions": decisions,
                "scheduler": {
                    "max_workers": min(workers, max(1, len(invalid_chunks))),
                    "memory_budget_bytes": memory_budget,
                    "max_active_chunks": outcome.max_active_chunks,
                    "max_used_sample_buffer_bytes": outcome.max_used_bytes,
                },
            }
            atomic_write_json(report_file, partial_report)
            cache.record_run(run_identifier, effective_mode, global_version, 1, partial_report)
            if isinstance(outcome.first_error, Exception):
                raise RenderTaskError(
                    "chunk rendering failed; first error is preserved",
                    {
                        "first_error": outcome.errors[0] if outcome.errors else None,
                        "partial_report": str(report_file),
                        "successful_chunk_ids": sorted(successful_by_id),
                    },
                ) from outcome.first_error
            raise RenderTaskError("chunk rendering failed")

        if outcome.interrupted:
            completed_ids = sorted(successful_by_id)
            pending_ids = [
                chunk.chunk_id
                for chunk in chunks
                if chunk.chunk_id not in successful_by_id
                and decisions_by_id[chunk.chunk_id]["invalid"]
            ]
            marker = {
                "schema": "terrain-baker-interrupt/v1",
                "status": "interrupted_at_chunk_boundary",
                "exit_code": BakeInterrupted.exit_code,
                "signal": getattr(signal_token, "signum", None),
                "run_id": run_identifier,
                "global_version": global_version,
                "completed_chunk_ids": completed_ids,
                "pending_chunk_ids": pending_ids,
                "note": "Only committed chunk metadata plus matching payload SHA-256 is recoverable.",
            }
            marker_path = write_interrupt_marker(state_root, marker)
            cache.record_run(run_identifier, effective_mode, global_version, 130, marker)
            raise BakeInterrupted(
                "termination handled at chunk boundary",
                {"marker": str(marker_path), **marker},
            )

        arrays_by_id: Dict[str, Dict[str, np.ndarray]] = {}
        chunk_reports: List[Dict[str, Any]] = []
        for chunk in chunks:
            rendered = successful_by_id.get(chunk.chunk_id)
            decision = dict(decisions_by_id[chunk.chunk_id])
            if rendered:
                arrays_by_id[chunk.chunk_id] = rendered["arrays"]
                decision["status"] = "rerendered"
                decision["invalid"] = True
                decision["height_dependency_range"] = rendered["dependency_range"]
                decision["global_grid_coverage"] = rendered["coverage_range"]
                decision["dependency_digest"] = rendered["metadata"]["dependency_digest"]
                decision["archive"] = rendered["metadata"].get("archived_previous")
                decision["sample_buffer_bytes"] = rendered["sample_buffer_bytes"]
            else:
                cached = cached_chunks[chunk.chunk_id]
                if cached is None:
                    raise RenderTaskError(
                        "chunk was classified reusable but no digest-valid state exists",
                        {"chunk_id": chunk.chunk_id},
                    )
                arrays_by_id[chunk.chunk_id] = cached.arrays
                decision["status"] = "reused"
                decision["invalid"] = False
                decision["archive"] = None
            chunk_reports.append(decision)

        assembled = assemble_grid(chunks, arrays_by_id, (height, width))

        reference = None
        seam_reference: Dict[str, Any] = {}
        if run_reference:
            reference = render_rect(scene, heightfield, 0, 0, width, height)
            seam_reference = compare_to_reference(assembled, reference, chunks)

        ownership_deviation = heightfield.ownership_routed_normal_deviation()
        ownership_perturbation = heightfield.boundary_same_point_deviation()
        boundary_normal_deviation_max = max(
            float(seam_reference.get("boundary_band_normal_vector_max_diff_vs_full", 0.0)),
            float(ownership_deviation),
        )
        seam_report = {
            **seam_reference,
            "heightfield_ownership_same_point_max_normal_deviation": ownership_deviation,
            "heightfield_half_open_perturbation_max_normal_deviation": ownership_perturbation,
            "boundary_normal_deviation_max": boundary_normal_deviation_max,
            "normal_deviation_max": boundary_normal_deviation_max,
            "boundary_normal_angle_deviation_max_degrees": seam_reference.get(
                "boundary_band_normal_angle_max_diff_vs_full_degrees", 0.0
            ),
            "normal_validation_method": (
                "Global central differences use stitched global samples and world scales; "
                "screen residual compares chunk assembly with a monolithic full rerender."
            ),
        }

        output_config = scene["output"]
        outputs: Dict[str, Any] = {}
        version_short = global_version[:16]
        outputs["grayscale"] = publish_gray_png(
            output_root / output_config["grayscale"], assembled["gray"], global_version
        )
        outputs["depth_world_npy"] = publish_npy(
            output_root / output_config["depth"], assembled["depth"], global_version, "depth_world"
        )
        outputs["height_world_npy"] = publish_npy(
            output_root / output_config["height"], assembled["height"], global_version, "height_world"
        )
        outputs["height_depth_npz"] = publish_npz(
            output_root / output_config["height_depth"],
            {
                "depth_world": assembled["depth"],
                "height_world": assembled["height"],
                "gray": assembled["gray"],
                "hit_mask": assembled["hit"],
            },
            global_version,
            (height, width),
        )
        depth_preview, depth_mapping = depth_to_uint16_preview(assembled["depth"])
        depth_png_data = encode_gray_png(depth_preview, {"global_version": version_short})
        outputs["depth_preview_png"] = publish_file(
            output_root / output_config["depth_preview"],
            depth_png_data,
            version=global_version,
            dimensions=(height, width),
            kind="depth_preview_uint16",
        )
        outputs["normals_npy"] = publish_npy(
            output_root / output_config["normals"], assembled["normal"], global_version, "normals"
        )
        if reference is not None:
            outputs["full_reference_gray_png"] = publish_gray_png(
                output_root / "full_reference_gray.png", reference.gray, global_version
            )
            outputs["full_reference_depth_npy"] = publish_npy(
                output_root / "full_reference_depth.npy",
                reference.depth,
                global_version,
                "full_reference_depth",
            )

        report = {
            "ok": True,
            "status": "success",
            "schema_version": "1.0",
            "run_id": run_identifier,
            "scene_id": scene.get("scene_id"),
            "change_mode": effective_mode,
            "edit": (
                {
                    "edit_id": edit.get("edit_id"),
                    "path": edit.get("_path"),
                    "regions": edit_regions,
                }
                if edit
                else None
            ),
            "global_version": global_version,
            "versions": {
                "global_version": global_version,
                "heightfield_content_digest": heightfield.content_digest,
            },
            "counts": {
                "total_chunks": len(chunks),
                "rerendered_chunks": len(invalid_chunks),
                "reused_chunks": len(chunks) - len(invalid_chunks),
                "scheduler_successes": len(outcome.successes),
            },
            "scheduler": {
                "requested_max_workers": workers,
                "bounded_pool_workers": min(workers, max(1, len(invalid_chunks))),
                "memory_budget_bytes": memory_budget,
                "max_active_chunks": outcome.max_active_chunks,
                "max_used_sample_buffer_bytes": outcome.max_used_bytes,
                "buffer_bytes_per_pixel": estimate_sample_buffer_bytes(1, 1) - 65536,
            },
            "chunks": chunk_reports,
            "invalidation_matrix": build_invalidation_matrix(chunk_reports, edit_regions),
            "seam_validation": seam_report,
            "outputs": outputs,
            "state_dir": str(state_root),
            "sqlite_cache": str(cache.db_path),
        }
        atomic_write_json(report_file, report)
        cache.record_run(run_identifier, effective_mode, global_version, 0, report)
        atomic_write_json(
            last_run_path,
            {
                "global_version": global_version,
                "mode": effective_mode,
                "scene": _sanitize_scene(scene),
                "report_path": str(report_file),
            },
        )
        interrupt_marker = state_root / "INTERRUPTED.json"
        if interrupt_marker.exists():
            try:
                previous_marker = interrupt_marker.read_text(encoding="utf-8")
                atomic_write_json(
                    state_root / "INTERRUPTED.recovered.json",
                    {"recovered_marker": previous_marker, "run_id": run_identifier},
                )
                interrupt_marker.unlink()
            except OSError:
                pass
        return report
