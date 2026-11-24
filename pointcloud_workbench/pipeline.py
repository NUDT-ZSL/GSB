"""End-to-end cached block rendering pipeline."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .binary_formats import depth_preview, encode_pfm, encode_png
from .camera import CameraGeometry, point_block_hits
from .errors import BusinessError, InterruptedRun, digest_error
from .renderer import (
    compare_images,
    logical_cost_units,
    render_block,
    render_camera_reference,
    render_digest,
    sample_buffer_bytes,
)
from .scheduler import LogicalClock, SchedulerJob, run_scheduler
from .spec import BlockSpec, CameraSpec, EditManifest, RenderConfig, TaskSpec, load_edit_manifest, load_task
from .state import BlockInspection, StateStore, inspect_block, write_block
from .util import atomic_write_bytes, atomic_write_json, digest_json, ensure_writable_directory


@dataclass
class RenderOptions:
    state_root: Path
    output_root: Path
    verify_full: bool = False
    stop_event: threading.Event | None = None
    after_block_persisted: Callable[[int, str], None] | None = None
    logical_clock: LogicalClock | None = None


@dataclass
class BlockPlan:
    block: BlockSpec
    action: str
    reasons: list[dict[str, Any]] = field(default_factory=list)
    affected_points: list[int] = field(default_factory=list)
    initial_inspection: BlockInspection | None = None

    @property
    def affected_count(self) -> int:
        return len(self.affected_points)


@dataclass
class TargetVersion:
    version_id: str
    dataset_digest: str
    camera_digest: str
    render_digest: str


@dataclass
class CameraResult:
    camera: CameraSpec
    target: TargetVersion
    gray: np.ndarray
    depth: np.ndarray
    plans: list[BlockPlan]
    final_inspections: dict[int, BlockInspection]
    digest_repairs: list[dict[str, Any]]
    scheduler_evidence: dict[str, Any]


def _target_version(task: TaskSpec, camera: CameraSpec) -> TargetVersion:
    camera_digest = camera.digest()
    render_digest = task.render.digest()
    version_id = digest_json(
        {
            "dataset_digest": task.dataset_digest,
            "camera_digest": camera_digest,
            "render_digest": render_digest,
        }
    )
    return TargetVersion(
        version_id=version_id,
        dataset_digest=task.dataset_digest,
        camera_digest=camera_digest,
        render_digest=render_digest,
    )


def _reason_from_inspection(inspection: BlockInspection) -> list[dict[str, Any]]:
    if not inspection.reasons:
        return [
            {
                "code": "CACHE_NOT_REUSABLE",
                "message": "completed block is not valid for the requested version",
            }
        ]
    result = []
    for reason in inspection.reasons:
        result.append(
            {
                "code": reason.get("code", "CACHE_NOT_REUSABLE"),
                "message": reason.get("message", "cached block is not reusable"),
                "details": reason.get("details", {}),
            }
        )
    return result


def _committed_matches(committed: dict[str, Any], target: TargetVersion) -> bool:
    return (
        committed.get("dataset_digest") == target.dataset_digest
        and committed.get("camera_digest") == target.camera_digest
        and committed.get("render_digest") == target.render_digest
    )


def _plan_blocks(
    task: TaskSpec,
    camera: CameraSpec,
    target: TargetVersion,
    state: StateStore,
    edit: EditManifest | None,
) -> tuple[list[BlockPlan], str]:
    blocks = task.render.blocks()
    inspections = {
        block.index: inspect_block(
            state.block_path(camera.camera_id, block.index),
            block,
            expected_version=target.version_id,
        )
        for block in blocks
    }
    committed = state.read_committed(camera.camera_id)
    if committed is None:
        mode = "resume"
    elif _committed_matches(committed, target):
        mode = "resume"
    elif (
        edit is not None
        and committed.get("dataset_digest") == edit.base_dataset_digest
        and target.dataset_digest == edit.edited_dataset_digest
        and committed.get("camera_digest") == target.camera_digest
        and committed.get("render_digest") == target.render_digest
    ):
        mode = "edit_migration"
    else:
        mode = "full_reset"

    plans: list[BlockPlan] = []
    if mode == "resume":
        for block in blocks:
            inspection = inspections[block.index]
            if inspection.reusable_for(target.version_id):
                plans.append(
                    BlockPlan(
                        block=block,
                        action="reuse",
                        reasons=[
                            {
                                "code": "CACHE_VERSION_AND_DIGEST_OK",
                                "message": "block version id and content digest match; pixels are reused",
                            }
                        ],
                        initial_inspection=inspection,
                    )
                )
            else:
                plans.append(
                    BlockPlan(
                        block=block,
                        action="render",
                        reasons=_reason_from_inspection(inspection),
                        initial_inspection=inspection,
                    )
                )
        return plans, mode

    if mode == "edit_migration":
        assert edit is not None
        geometry = CameraGeometry(camera, task.render)
        edited_projection = geometry.project(task.pointcloud)
        new_hits = point_block_hits(edited_projection, edit.affected_indices, task.render)
        affected_all = {int(index) for index in edit.affected_indices.tolist()}
        old_version_id = digest_json(
            {
                "dataset_digest": edit.base_dataset_digest,
                "camera_digest": target.camera_digest,
                "render_digest": target.render_digest,
            }
        )
        for block in blocks:
            inspection = inspections[block.index]
            if inspection.reusable_for(target.version_id):
                plans.append(
                    BlockPlan(
                        block=block,
                        action="reuse",
                        reasons=[
                            {
                                "code": "EDIT_RESUME_VERSION_AND_DIGEST_OK",
                                "message": "block already has the edited version and a valid digest",
                            }
                        ],
                        initial_inspection=inspection,
                    )
                )
                continue
            old_dependencies: set[int] = set()
            if inspection.dependencies is not None:
                old_dependencies = {int(value) for value in inspection.dependencies.tolist()}
            old_intersection = old_dependencies & affected_all
            replacement_hits = new_hits.get(block.index, set())
            old_version_ok = (
                inspection.loaded
                and inspection.content_ok
                and inspection.meta is not None
                and inspection.meta.get("version_id") == old_version_id
            )
            if old_version_ok and not old_intersection and not replacement_hits:
                state.promote(
                    camera.camera_id,
                    block,
                    inspection,
                    version_id=target.version_id,
                    dataset_digest=target.dataset_digest,
                    camera_digest=target.camera_digest,
                    render_digest=target.render_digest,
                    task_id=task.task_id,
                    point_count=task.pointcloud.point_count,
                )
                plans.append(
                    BlockPlan(
                        block=block,
                        action="promote",
                        reasons=[
                            {
                                "code": "EDIT_DEPENDENCIES_UNCHANGED",
                                "message": (
                                    "old dependency set is disjoint from edited indices and every "
                                    "replacement point misses this block; camera/render are unchanged"
                                ),
                                "edit_ids": [],
                                "affected_point_count": 0,
                            }
                        ],
                        initial_inspection=inspection,
                    )
                )
                continue
            reasons: list[dict[str, Any]] = []
            affected_points = sorted(old_intersection | replacement_hits)
            if old_intersection:
                reasons.append(
                    {
                        "code": "EDIT_DEPENDENCY_CHANGED",
                        "message": "block dependency set intersects replaced point indices",
                        "edit_ids": edit.regions_for_indices(old_intersection),
                        "affected_point_count": len(old_intersection),
                    }
                )
            if replacement_hits:
                reasons.append(
                    {
                        "code": "EDIT_NEW_COVERAGE",
                        "message": "replacement points project into this block even if absent from the old dependency set",
                        "edit_ids": edit.regions_for_indices(replacement_hits),
                        "affected_point_count": len(replacement_hits),
                    }
                )
            if not old_version_ok:
                reasons.extend(_reason_from_inspection(inspection))
            plans.append(
                BlockPlan(
                    block=block,
                    action="render",
                    reasons=reasons,
                    affected_points=affected_points,
                    initial_inspection=inspection,
                )
            )
        return plans, mode

    reason: dict[str, Any]
    if edit is not None and committed is not None and committed.get("dataset_digest") != edit.base_dataset_digest:
        reason = {
            "code": "EDIT_LINEAGE_MISMATCH",
            "message": "committed dataset is not the declared edit base; whole image is recomputed",
        }
    else:
        reason = {
            "code": "VERSION_RESET",
            "message": "point-cloud/camera/render version differs from committed state; whole image is recomputed",
        }
    for block in blocks:
        plans.append(
            BlockPlan(
                block=block,
                action="render",
                reasons=[reason],
                initial_inspection=inspections[block.index],
            )
        )
    return plans, mode


def _interrupt(
    state: StateStore,
    task: TaskSpec,
    camera: CameraSpec,
    *,
    completed: list[int],
    pending: list[int],
    reason: str,
) -> None:
    state.write_interrupt_marker(
        {
            "schema": 1,
            "status": "interrupted",
            "exit_code": 130,
            "task_id": task.task_id,
            "cache_key": task.cache_key,
            "camera_id": camera.camera_id,
            "completed_blocks": sorted(completed),
            "pending_blocks": sorted(pending),
            "reason": reason,
        }
    )
    raise InterruptedRun(
        "render stopped at a completed block boundary",
        details={
            "camera_id": camera.camera_id,
            "completed_blocks": sorted(completed),
            "pending_blocks": sorted(pending),
            "exit_code": 130,
        },
    )


def _run_scheduled_plans(
    task: TaskSpec,
    camera: CameraSpec,
    state: StateStore,
    target: TargetVersion,
    plans: list[BlockPlan],
    options: RenderOptions,
) -> dict[str, Any]:
    render_plans = [plan for plan in plans if plan.action == "render"]
    if not render_plans:
        return {
            "completed_indices": [],
            "admission_sequence": [],
            "rounds": [],
            "peak_in_flight_bytes": 0,
            "clock_snapshot": {},
        }
    geometry = CameraGeometry(camera, task.render)
    projection = geometry.project(task.pointcloud)

    def worker(job: SchedulerJob[BlockPlan]) -> dict[str, Any]:
        plan = job.payload
        rendered = render_block(
            cloud=task.pointcloud,
            render=task.render,
            block=plan.block,
            projection=projection,
            geometry=geometry,
            camera=camera,
        )
        metadata = write_block(
            state.block_path(camera.camera_id, plan.block.index),
            plan.block,
            rendered.gray,
            rendered.depth,
            rendered.dependency_indices,
            version_id=target.version_id,
            dataset_digest=target.dataset_digest,
            camera_digest=target.camera_digest,
            render_digest=target.render_digest,
            task_id=task.task_id,
            point_count=task.pointcloud.point_count,
            provenance="rendered",
        )
        if options.after_block_persisted is not None:
            options.after_block_persisted(plan.block.index, camera.camera_id)
        return {
            "block_index": plan.block.index,
            "dependency_count": metadata["dependency_count"],
            "content_digest": metadata["content_digest"],
        }

    jobs = [
        SchedulerJob(
            index=plan.block.index,
            payload=plan,
            sample_bytes=sample_buffer_bytes(plan.block, task.render),
            logical_cost=logical_cost_units(plan.block, task.render, task.pointcloud.point_count),
        )
        for plan in render_plans
    ]
    scheduler_result = run_scheduler(
        jobs,
        worker,
        max_workers=task.render.max_workers,
        memory_budget_bytes=task.render.memory_budget_bytes,
        round_budget_units=task.render.round_budget_units,
        clock=options.logical_clock,
        stop_event=options.stop_event,
    )
    if scheduler_result.errors:
        raise scheduler_result.errors[0]
    if scheduler_result.interrupted:
        _interrupt(
            state,
            task,
            camera,
            completed=scheduler_result.completed_indices,
            pending=scheduler_result.pending_indices,
            reason="stop requested at a block boundary",
        )
    return {
        "completed_indices": scheduler_result.completed_indices,
        "admission_sequence": scheduler_result.admission_sequence,
        "rounds": scheduler_result.rounds,
        "peak_in_flight_bytes": scheduler_result.peak_in_flight_bytes,
        "clock_snapshot": scheduler_result.clock_snapshot,
    }


def _verify_blocks(
    task: TaskSpec,
    camera: CameraSpec,
    state: StateStore,
    target: TargetVersion,
) -> tuple[dict[int, BlockInspection], list[tuple[BlockSpec, BlockInspection]]]:
    inspections: dict[int, BlockInspection] = {}
    bad: list[tuple[BlockSpec, BlockInspection]] = []
    for block in task.render.blocks():
        inspection = inspect_block(
            state.block_path(camera.camera_id, block.index),
            block,
            expected_version=target.version_id,
        )
        inspections[block.index] = inspection
        if not inspection.reusable_for(target.version_id):
            bad.append((block, inspection))
    return inspections, bad


def _repair_plan(block: BlockSpec, inspection: BlockInspection) -> BlockPlan:
    reasons = [
        {
            "code": "DIGEST_REPAIR",
            "message": "final per-block digest verification failed; only this block is re-rendered",
            "cause": _reason_from_inspection(inspection),
        }
    ]
    return BlockPlan(
        block=block,
        action="render",
        reasons=reasons,
        initial_inspection=inspection,
    )


def _assemble(
    render: RenderConfig,
    inspections: dict[int, BlockInspection],
) -> tuple[np.ndarray, np.ndarray]:
    gray = np.zeros((render.height, render.width), dtype=np.uint8)
    depth = np.full((render.height, render.width), np.inf, dtype=np.float32)
    for block in render.blocks():
        inspection = inspections[block.index]
        if inspection.gray is None or inspection.depth is None:
            raise digest_error("block has no arrays after verification", block_index=block.index)
        gray[block.y0 : block.y1, block.x0 : block.x1] = inspection.gray
        depth[block.y0 : block.y1, block.x0 : block.x1] = inspection.depth
    return gray, depth


def _render_camera(
    task: TaskSpec,
    camera: CameraSpec,
    edit: EditManifest | None,
    state: StateStore,
    options: RenderOptions,
) -> CameraResult:
    target = _target_version(task, camera)
    plans, mode = _plan_blocks(task, camera, target, state, edit)
    if options.stop_event is not None and options.stop_event.is_set():
        _interrupt(
            state,
            task,
            camera,
            completed=[],
            pending=[block.index for block in task.render.blocks()],
            reason="stop requested before block admission",
        )
    scheduler_evidence: dict[str, Any] = {
        "mode": mode,
        "phase_1": _run_scheduled_plans(task, camera, state, target, plans, options),
    }
    inspections, bad = _verify_blocks(task, camera, state, target)
    digest_repairs: list[dict[str, Any]] = []
    if bad:
        digest_repairs = [
            {
                "business_code": "E_DIGEST",
                "block_index": block.index,
                "block_id": block.block_id,
                "reasons": _reason_from_inspection(inspection),
                "repaired": False,
            }
            for block, inspection in bad
        ]
        repair_plans = [_repair_plan(block, inspection) for block, inspection in bad]
        scheduler_evidence["digest_repair"] = _run_scheduled_plans(
            task,
            camera,
            state,
            target,
            repair_plans,
            options,
        )
        inspections, bad = _verify_blocks(task, camera, state, target)
        if bad:
            raise digest_error(
                "per-block digest verification still fails after targeted re-render",
                camera_id=camera.camera_id,
                failed_blocks=[
                    {
                        "block_index": block.index,
                        "block_id": block.block_id,
                        "reasons": _reason_from_inspection(inspection),
                    }
                    for block, inspection in bad
                ],
            )
        for repair in digest_repairs:
            repair["repaired"] = True
    gray, depth = _assemble(task.render, inspections)
    repaired_indices = {item["block_index"] for item in digest_repairs}
    for plan in plans:
        if plan.block.index in repaired_indices and plan.action != "render":
            plan.action = "digest_repair"
            plan.reasons = [
                {
                    "code": "DIGEST_REPAIR",
                    "message": "cached block failed final digest verification and was selectively re-rendered",
                }
            ]
    state.commit(
        camera.camera_id,
        {
            "schema": 1,
            "task_id": task.task_id,
            "cache_key": task.cache_key,
            "dataset_digest": task.dataset_digest,
            "camera_digest": target.camera_digest,
            "render_digest": target.render_digest,
            "version_id": target.version_id,
            "block_count": len(task.render.blocks()),
            "image_digest": render_digest(gray, depth),
            "complete": True,
        },
    )
    return CameraResult(
        camera=camera,
        target=target,
        gray=gray,
        depth=depth,
        plans=plans,
        final_inspections=inspections,
        digest_repairs=digest_repairs,
        scheduler_evidence=scheduler_evidence,
    )


def _atomic_output(path: Path, data: bytes) -> None:
    try:
        atomic_write_bytes(path, data)
    except OSError as exc:
        raise BusinessError(
            "E_OUTPUT",
            "cannot write final output",
            details={"path": str(path), "os_error": str(exc)},
        ) from exc


def _camera_output_files(
    task: TaskSpec,
    result: CameraResult,
    output_root: Path,
) -> dict[str, str]:
    camera_root = output_root / result.camera.camera_id
    try:
        ensure_writable_directory(camera_root, "E_OUTPUT")
    except BusinessError:
        raise
    gray_path = camera_root / "grayscale.png"
    depth_path = camera_root / "depth.pfm"
    preview_path = camera_root / "depth_preview.png"
    blocks_path = camera_root / "blocks.json"
    _atomic_output(gray_path, encode_png(result.gray, mode="gray"))
    _atomic_output(depth_path, encode_pfm(result.depth))
    _atomic_output(preview_path, encode_png(depth_preview(result.depth), mode="gray"))

    block_reports = []
    for plan in result.plans:
        inspection = result.final_inspections[plan.block.index]
        meta = inspection.meta or {}
        dependencies = inspection.dependencies if inspection.dependencies is not None else np.empty(0)
        sx0, sy0, sx1, sy1 = task.render.block_supersample_rect(plan.block)
        depth_values = (
            inspection.depth[np.isfinite(inspection.depth)]
            if inspection.depth is not None
            else np.empty(0, dtype=np.float32)
        )
        depth_summary = {
            "finite_pixels": int(depth_values.size),
            "min_world": float(depth_values.min()) if depth_values.size else None,
            "max_world": float(depth_values.max()) if depth_values.size else None,
            "mean_world": float(depth_values.mean(dtype=np.float64)) if depth_values.size else None,
        }
        status = plan.action
        if status == "promote":
            status = "reused_promoted"
        elif status == "reuse":
            status = "reused"
        block_reports.append(
            {
                "index": plan.block.index,
                "block_id": plan.block.block_id,
                "rect": plan.block.rect_dict(),
                "supersampled_rect": {
                    "x0": sx0,
                    "y0": sy0,
                    "x1": sx1,
                    "y1": sy1,
                },
                "status": status,
                "reasons": plan.reasons,
                "affected_point_count": plan.affected_count,
                "affected_points": plan.affected_points,
                "dependency_count": int(dependencies.size),
                "dependency_indices": [int(value) for value in dependencies.tolist()],
                "dependency_digest": meta.get("dependency_digest"),
                "content_digest": meta.get("content_digest"),
                "depth_summary": depth_summary,
                "version_id": meta.get("version_id"),
                "digest_ok": inspection.content_ok,
            }
        )
    block_document = {
        "schema": 1,
        "camera_id": result.camera.camera_id,
        "width": task.render.width,
        "height": task.render.height,
        "supersample": task.render.supersample,
        "block_width": task.render.block_width,
        "block_height": task.render.block_height,
        "version_id": result.target.version_id,
        "blocks": block_reports,
    }
    try:
        atomic_write_json(blocks_path, block_document)
    except OSError as exc:
        raise BusinessError(
            "E_OUTPUT",
            "cannot write block diagnostic manifest",
            details={"path": str(blocks_path), "os_error": str(exc)},
        ) from exc
    return {
        "grayscale": str(gray_path.resolve()),
        "depth": str(depth_path.resolve()),
        "depth_preview": str(preview_path.resolve()),
        "blocks": str(blocks_path.resolve()),
    }


def _camera_report(
    task: TaskSpec,
    result: CameraResult,
    outputs: dict[str, str],
) -> dict[str, Any]:
    rendered = [plan for plan in result.plans if plan.action == "render"]
    repairs = [plan for plan in result.plans if plan.action == "digest_repair"]
    reused = [plan for plan in result.plans if plan.action in {"reuse", "promote"}]
    promoted = [plan for plan in result.plans if plan.action == "promote"]
    invalidated = rendered + repairs
    total_affected = set()
    for plan in invalidated:
        total_affected.update(plan.affected_points)
    return {
        "camera_id": result.camera.camera_id,
        "camera": result.camera.normalized(),
        "version_id": result.target.version_id,
        "dataset_digest": result.target.dataset_digest,
        "camera_digest": result.target.camera_digest,
        "render_digest": result.target.render_digest,
        "outputs": outputs,
        "image_digest": render_digest(result.gray, result.depth),
        "block_count": len(result.plans),
        "rendered_block_count": len(rendered),
        "reused_block_count": len(reused),
        "promoted_block_count": len(promoted),
        "invalidated_block_count": len(invalidated),
        "affected_point_count": len(total_affected),
        "invalidated_blocks": [
            {
                "block_index": plan.block.index,
                "block_id": plan.block.block_id,
                "screen_region": plan.block.rect_dict(),
                "reasons": plan.reasons,
                "affected_point_count": plan.affected_count,
                "affected_points": plan.affected_points,
            }
            for plan in invalidated
        ],
        "reused_blocks": [
            {
                "block_index": plan.block.index,
                "block_id": plan.block.block_id,
                "promoted_version": plan.action == "promote",
                "reason": plan.reasons,
            }
            for plan in reused
        ],
        "digest_repairs": result.digest_repairs,
        "digest_checks": {
            "checked_blocks": len(result.final_inspections),
            "valid_blocks": sum(
                1 for inspection in result.final_inspections.values() if inspection.content_ok
            ),
            "all_valid": all(
                inspection.content_ok and inspection.reusable_for(result.target.version_id)
                for inspection in result.final_inspections.values()
            ),
        },
        "reuse_safety": (
            "Reused blocks have matching camera/render versions, a valid content digest, "
            "and either an unchanged dataset or a dependency set disjoint from the edit "
            "indices with every replacement point projecting outside the block."
        ),
        "scheduler": result.scheduler_evidence,
    }


def _full_recompute_check(
    task: TaskSpec,
    result: CameraResult,
) -> dict[str, Any]:
    reference_gray, reference_depth, _ = render_camera_reference(
        task.pointcloud,
        result.camera,
        task.render,
    )
    comparison = compare_images(result.gray, result.depth, reference_gray, reference_depth)
    comparison["performed"] = True
    comparison["reference_image_digest"] = render_digest(reference_gray, reference_depth)
    return comparison


def render_task(task_path: Path, options: RenderOptions) -> dict[str, Any]:
    """Validate inputs, render/cached-render every camera, then atomically publish outputs."""

    task = load_task(Path(task_path))
    edit = (
        load_edit_manifest(task.edit_manifest_path, task)
        if task.edit_manifest_path is not None
        else None
    )
    state = StateStore(options.state_root, task.cache_key)

    camera_results: list[CameraResult] = []
    for camera in task.cameras:
        camera_results.append(_render_camera(task, camera, edit, state, options))

    if options.stop_event is not None and options.stop_event.is_set() and camera_results:
        last = camera_results[-1]
        _interrupt(
            state,
            task,
            last.camera,
            completed=[block.index for block in task.render.blocks()],
            pending=[],
            reason="stop requested after the final completed block boundary",
        )

    full_checks: dict[str, dict[str, Any]] = {}
    if options.verify_full:
        for result in camera_results:
            check = _full_recompute_check(task, result)
            full_checks[result.camera.camera_id] = check
            if not bool(check["pixel_identical"]):
                raise BusinessError(
                    "E_PIXEL_MISMATCH",
                    "incremental or concurrent result is not pixel-identical to full recomputation",
                    details={
                        "camera_id": result.camera.camera_id,
                        "comparison": check,
                    },
                )
    else:
        for result in camera_results:
            full_checks[result.camera.camera_id] = {
                "performed": False,
                "status": "not_requested",
            }

    output_root = Path(options.output_root).resolve()
    camera_reports: list[dict[str, Any]] = []
    for result in camera_results:
        outputs = _camera_output_files(task, result, output_root)
        report = _camera_report(task, result, outputs)
        report["full_recompute_verification"] = full_checks[result.camera.camera_id]
        camera_reports.append(report)

    report: dict[str, Any] = {
        "ok": True,
        "schema": 1,
        "task_id": task.task_id,
        "cache_key": task.cache_key,
        "task_path": str(task.task_path),
        "output_root": str(output_root),
        "dataset_digest": task.dataset_digest,
        "edit": (
            {
                "manifest": str(edit.path),
                "base_dataset_digest": edit.base_dataset_digest,
                "edited_dataset_digest": edit.edited_dataset_digest,
                "affected_point_count": int(edit.affected_indices.size),
                "regions": [
                    {
                        "id": region.region_id,
                        "start": region.start,
                        "count": region.count,
                        "reason": region.reason,
                    }
                    for region in edit.regions
                ],
            }
            if edit is not None
            else None
        ),
        "cameras": camera_reports,
        "digest_repair_block_count": sum(
            len(camera["digest_repairs"]) for camera in camera_reports
        ),
        "viewer": str((output_root / "workbench.html").resolve()),
    }
    report_path = output_root / "render_report.json"
    try:
        atomic_write_json(report_path, report)
    except OSError as exc:
        raise BusinessError(
            "E_OUTPUT",
            "cannot write render report",
            details={"path": str(report_path), "os_error": str(exc)},
        ) from exc

    from .viewer import build_static_viewer

    build_static_viewer(output_root)
    state.clear_interrupt_marker()
    return report

