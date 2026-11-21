from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .snapshots import STAGES, SnapshotStore


def _recent_errors(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    records: list[dict[str, Any]] = []
    for line in lines[-max(1, limit) :]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def build_status(run_dir: str | Path, *, recent_error_limit: int = 10) -> dict[str, Any]:
    store = SnapshotStore(run_dir)
    snapshots = store.load_committed()
    progress = store.read_progress()
    latest = snapshots[-1] if snapshots else None
    latest_index = latest.version if latest else 0
    progress_index = (
        int(progress.get("stage_index", 0))
        if progress and isinstance(progress.get("stage_index", 0), int)
        else 0
    )
    if progress_index > latest_index:
        current_stage = str(progress.get("stage", "unknown"))
        lifecycle = "in_progress"
    elif latest is not None:
        current_stage = latest.stage
        lifecycle = "complete" if latest.stage == "aggregation" else "paused"
    else:
        current_stage = "not_started"
        lifecycle = "not_started"

    snapshots_by_stage = {snapshot.stage: snapshot for snapshot in snapshots}
    jobs: list[str] = []
    validation = snapshots_by_stage.get("validation")
    if validation is not None:
        jobs = [str(payload["job_id"]) for payload in validation.payload["jobs"]]
    completed_by_stage = {
        stage: len(snapshots_by_stage[stage].completed_jobs)
        for stage in STAGES
        if stage in snapshots_by_stage
    }
    per_job: dict[str, dict[str, bool]] = {job_id: {} for job_id in jobs}
    for stage in STAGES:
        snapshot = snapshots_by_stage.get(stage)
        if snapshot is None:
            continue
        completed = set(snapshot.completed_jobs)
        for job_id in jobs:
            per_job[job_id][stage] = job_id in completed

    compensation_interventions = 0
    flushed_subnormals = 0
    deviation_spectrum: list[dict[str, Any]] = []
    accumulation = snapshots_by_stage.get("accumulation")
    if accumulation is not None:
        compensation_interventions = int(
            accumulation.payload.get("total_compensation_interventions", 0)
        )
        flushed_subnormals = int(
            accumulation.payload.get("total_flushed_subnormals", 0)
        )
        for job_id, result in accumulation.payload.get("results", {}).items():
            diagnostics = result.get("diagnostics", {})
            deviation_spectrum.append(
                {
                    "job_id": job_id,
                    "max_abs_error": diagnostics.get("max_abs_error", 0.0),
                    "mean_abs_error": diagnostics.get("mean_abs_error", 0.0),
                    "p95_abs_error": diagnostics.get("p95_abs_error", 0.0),
                    "p99_abs_error": diagnostics.get("p99_abs_error", 0.0),
                    "mismatch_count": diagnostics.get("mismatch_count", 0),
                }
            )

    snapshot_rows = [
        {
            "stage": snapshot.stage,
            "version": snapshot.version,
            "content_checksum": snapshot.content_checksum,
            "completed_jobs": len(snapshot.completed_jobs),
            "job_count": snapshot.job_count,
        }
        for snapshot in snapshots
    ]
    return {
        "run_dir": str(Path(run_dir)),
        "status": lifecycle,
        "current_stage": current_stage,
        "snapshot_version": latest.version if latest else 0,
        "snapshot_checksum": latest.content_checksum if latest else None,
        "job_count": len(jobs),
        "completed_count_by_stage": completed_by_stage,
        "per_job_completion": per_job,
        "compensation_interventions": compensation_interventions,
        "flushed_subnormals": flushed_subnormals,
        "deviation_spectrum": deviation_spectrum,
        "progress": progress,
        "snapshots": snapshot_rows,
        "recent_errors": _recent_errors(
            store.error_log_path, recent_error_limit
        ),
    }
