"""End-to-end render pipeline with resume, digest repair and output commit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .errors import EngineError, error
from .interrupts import CancellationToken
from .model import Scene
from .output import build_error_report, encode_trace_gzip, write_outputs
from .persistence import StateIssue, StateStore
from .renderer import ImageResult, TileGeometry, TileResult, assemble_image, tile_geometries
from .scheduler import SchedulerStats, schedule_tiles
from .stability import evaluate_stability
from .util import probe_directory, sha256_bytes, sha256_json, version_id_for


OUTPUT_NAMES = {
    "grayscale": "grayscale.png",
    "depth": "depth.npy",
    "error_report": "error_report.json",
    "trace": "error_trace.jsonl.gz",
}


@dataclass
class PipelineResult:
    ok: bool
    exit_code: int
    payload: dict[str, Any]


def _interrupted_payload(
    store: StateStore,
    token: CancellationToken,
    completed_indices: Sequence[int],
) -> PipelineResult:
    store.mark_interrupted(token, completed_indices)
    return PipelineResult(
        ok=False,
        exit_code=token.exit_code,
        payload={
            "ok": False,
            "code": "E_INTERRUPTED",
            "message": "termination signal observed at a tile boundary",
            "signal": token.signal_name,
            "exit_code": token.exit_code,
            "version_id": store.version_id,
            "completed_tiles": sorted(completed_indices),
            "interrupt_marker": str(store.interrupted_path),
            "outputs_written": False,
        },
    )


def _tile_validation(
    store: StateStore,
    tiles: Sequence[TileGeometry],
) -> tuple[dict[int, TileResult], list[StateIssue], dict[int, str]]:
    results: dict[int, TileResult] = {}
    issues: list[StateIssue] = []
    digests: dict[int, str] = {}
    for tile in tiles:
        result, issue, digest = store.validate_block(tile)
        if result is None:
            if issue is not None:
                issues.append(issue)
            continue
        results[tile.index] = result
        if digest is not None:
            digests[tile.index] = digest
    return results, issues, digests


def run_render(
    scene: Scene,
    *,
    token: CancellationToken | None = None,
    run_stability: bool = True,
    collect_trace: bool = True,
) -> PipelineResult:
    token = token or CancellationToken()
    probe_directory(scene.render.state_dir, "E_STATE")
    probe_directory(scene.render.output_dir, "E_OUTPUT")
    version_id = version_id_for(scene)
    output_version_id = (
        "out-"
        + sha256_json(
            {
                "render_version_id": version_id,
                "stability_enabled": run_stability,
                "trace_enabled": collect_trace,
            }
        )[:32]
    )
    store = StateStore(scene, version_id)
    store.prepare()
    tiles = tile_geometries(scene)

    reusable, load_issues = store.load_reusable(tiles)
    digest_issue_tiles = {
        issue.tile_index
        for issue in load_issues
        if issue.code == "E_DIGEST" and issue.tile_index is not None
    }
    version_issue = any(issue.code == "E_VERSION" for issue in load_issues)
    missing = [tile for tile in tiles if tile.index not in reusable]
    rendered_records: dict[int, dict[str, Any]] = {}

    def persist_result(result: TileResult) -> None:
        rendered_records[result.tile.index] = store.persist(result)

    scheduler_stats = SchedulerStats(
        max_workers=min(scene.render.max_workers, max(1, len(missing))),
        memory_limit_bytes=scene.render.inflight_memory_limit_bytes,
    )
    if missing:
        _, scheduler_stats, cancelled = schedule_tiles(
            scene,
            missing,
            collect_trace=collect_trace,
            on_result=persist_result,
            token=token,
        )
        completed_now = sorted(set(reusable) | set(rendered_records))
        if cancelled or token.requested:
            return _interrupted_payload(store, token, completed_now)

    if token.requested:
        return _interrupted_payload(store, token, sorted(set(reusable) | set(rendered_records)))

    # Final mandatory block digest pass.  Invalid blocks are re-rendered once
    # before any output is allowed to reach the output directory.
    validated, validation_issues, block_digests = _tile_validation(store, tiles)
    first_validation_issues = list(validation_issues)
    if validation_issues:
        retry_tiles = [
            tile
            for tile in tiles
            if tile.index not in validated
        ]
        for issue in validation_issues:
            if issue.tile_index is not None:
                digest_issue_tiles.add(issue.tile_index)
        if retry_tiles:
            _, _, cancelled = schedule_tiles(
                scene,
                retry_tiles,
                collect_trace=collect_trace,
                on_result=persist_result,
                token=token,
            )
            if cancelled or token.requested:
                return _interrupted_payload(
                    store,
                    token,
                    sorted(set(reusable) | set(rendered_records)),
                )
        validated, validation_issues, block_digests = _tile_validation(store, tiles)
        if validation_issues:
            raise error(
                "E_DIGEST",
                "one or more block digests remain invalid after fallback re-render",
                details={
                    "issues": [issue.as_dict() for issue in validation_issues],
                    "output_written": False,
                },
            )
    if len(validated) != len(tiles):
        missing_indices = sorted(set(range(len(tiles))) - set(validated))
        raise error(
            "E_DIGEST",
            "not all blocks are available after digest validation",
            details={"missing_tile_indices": missing_indices},
        )

    all_tiles = [validated[index] for index in sorted(validated)]
    prior_scheduler = store.previous_scheduler_stats()
    report_scheduler = {
        "peak_inflight_bytes": max(
            int(prior_scheduler.get("peak_inflight_bytes", 0)),
            int(scheduler_stats.peak_inflight_bytes),
        ),
        "submitted_tiles": int(prior_scheduler.get("submitted_tiles", 0))
        + int(scheduler_stats.submitted_tiles),
        "completed_tiles": int(prior_scheduler.get("completed_tiles", 0))
        + int(scheduler_stats.completed_tiles),
        "max_workers": max(
            int(prior_scheduler.get("max_workers", 0)),
            int(scheduler_stats.max_workers),
        ),
        "memory_limit_bytes": int(scheduler_stats.memory_limit_bytes),
    }
    image = assemble_image(
        scene,
        all_tiles,
        collect_trace=collect_trace,
        extra_stats={
            "scheduler": report_scheduler,
            "reused_blocks": len(reusable),
            "rendered_blocks": len(rendered_records),
        },
    )

    stability = None
    if run_stability:
        try:
            stability = evaluate_stability(scene, image, token=token)
        except RuntimeError:
            if token.requested:
                return _interrupted_payload(
                    store,
                    token,
                    sorted(set(reusable) | set(rendered_records)),
                )
            raise
        if token.requested:
            return _interrupted_payload(
                store,
                token,
                sorted(set(reusable) | set(rendered_records)),
            )

    trace_bytes = encode_trace_gzip(image.trace_rows) if collect_trace else encode_trace_gzip([])
    trace_digest = sha256_bytes(trace_bytes)
    recovery_issues = [issue.as_dict() for issue in load_issues]
    recovery_issues.extend(issue.as_dict() for issue in first_validation_issues)
    recovery = {
        "state_version_matched": store.version_matched,
        "version_changed_full_redo": version_issue,
        "reused_tiles": sorted(reusable),
        "rendered_tiles": sorted(rendered_records),
        "digest_repaired_tiles": sorted(digest_issue_tiles),
        "recovered_error_codes": sorted(
            {issue["code"] for issue in recovery_issues if issue.get("code") in ("E_DIGEST", "E_VERSION")}
        ),
        "issues": recovery_issues,
    }
    report = build_error_report(
        scene,
        image,
        version_id,
        output_version_id,
        stability=stability,
        recovery=recovery,
        output_names=OUTPUT_NAMES,
        trace_digest=trace_digest,
        trace_bytes=len(trace_bytes),
    )
    output_info = write_outputs(
        scene,
        image,
        output_version_id,
        report,
        image.trace_rows,
        trace_bytes=trace_bytes,
    )
    store.mark_completed(block_digests, scheduler_stats.__dict__.copy())

    summary = dict(image.stats)
    payload: dict[str, Any] = {
        "ok": True,
        "code": "E_DIGEST" if digest_issue_tiles else None,
        "warning_codes": sorted(
            {issue["code"] for issue in recovery_issues if issue.get("code") in ("E_DIGEST", "E_VERSION")}
        ),
        "version_id": version_id,
        "output_version_id": output_version_id,
        "width": scene.render.width,
        "height": scene.render.height,
        "image": {
            "path": OUTPUT_NAMES["grayscale"],
            "format": "png-gray8",
            "width": scene.render.width,
            "height": scene.render.height,
        },
        "depth": {
            "path": OUTPUT_NAMES["depth"],
            "format": "npy-float32-little-endian",
            "units": "world_units",
        },
        "error_report": OUTPUT_NAMES["error_report"],
        "error_trace": OUTPUT_NAMES["trace"],
        "summary": summary,
        "state": {
            "dir": str(scene.render.state_dir),
            "version_id": version_id,
            "output_version_id": output_version_id,
            "version_matched": store.version_matched,
            "reused_tiles": len(reusable),
            "rendered_tiles": len(rendered_records),
            "digest_repaired_tiles": sorted(digest_issue_tiles),
        },
        "output": output_info,
        "stability": {
            "enabled": stability is not None,
            "step_trend": None if stability is None else stability["step"]["trend"],
            "cutoff_trend": None if stability is None else stability["cutoff"]["trend"],
            "early_terminated_pixels": summary["early_cutoff_pixels"],
        },
    }
    return PipelineResult(ok=True, exit_code=0, payload=payload)

