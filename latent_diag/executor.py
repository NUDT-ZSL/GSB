from __future__ import annotations

import json
import multiprocessing
import os
import signal
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from .arrayio import BlobStore
from .config import EffectiveConfig, load_config
from .db import LedgerDB
from .errors import EngineError
from .hashing import hash_json
from .pipeline import condition_cache_key, initial_noise, noise_cache_key
from .scheduler import AdmissionResult, admit_tasks, plan_invalidations
from .snapshots import SnapshotStore
from .task import TaskSpec, load_manifest, validate_manifest
from .worker import run_worker


def _parse_previous_tasks(
    snapshot_body: dict[str, Any] | None,
    db: LedgerDB,
    *,
    base_dir: Path,
) -> list[TaskSpec]:
    if snapshot_body is not None:
        return [
            TaskSpec.from_dict(payload, base_dir=base_dir)
            for payload in snapshot_body.get("tasks", [])
        ]
    return [TaskSpec.from_dict(payload) for payload in db.current_tasks()]


def _validate_latent_inputs(tasks: list[TaskSpec]) -> None:
    for task in tasks:
        initial_noise(task)


def _descriptor_is_valid(blobs: BlobStore, descriptor: dict[str, Any] | None) -> bool:
    return bool(descriptor and blobs.validate_descriptor(descriptor))


def _validate_checkpoint(
    *,
    task: TaskSpec,
    checkpoint: dict[str, Any],
    ledger: dict[str, Any] | None,
    blobs: BlobStore,
) -> bool:
    if ledger is None:
        return False
    if checkpoint.get("input_hash") != ledger.get("input_hash"):
        return False
    if checkpoint.get("ledger_hash") != ledger.get("content_hash"):
        return False
    ledger_descriptor = {
        "artifact_path": ledger.get("artifact_path"),
        "content_hash": ledger.get("content_hash"),
        "storage": "json",
    }
    if not _descriptor_is_valid(blobs, ledger_descriptor):
        return False
    for stage in checkpoint.get("stages", {}).values():
        if not _descriptor_is_valid(blobs, stage):
            return False
    for artifact in checkpoint.get("artifacts", []):
        if not _descriptor_is_valid(blobs, artifact):
            return False
    return True


def _reuse_for_compute(
    *,
    task: TaskSpec,
    config: EffectiveConfig,
    db: LedgerDB,
    blobs: BlobStore,
) -> dict[str, dict[str, Any]]:
    reuse: dict[str, dict[str, Any]] = {}
    condition = db.cache_artifact("condition", condition_cache_key(task, config))
    if _descriptor_is_valid(blobs, condition):
        reuse["condition"] = condition  # type: ignore[assignment]
    noise = db.cache_artifact("initial_noise", noise_cache_key(task))
    if _descriptor_is_valid(blobs, noise):
        reuse["noise"] = noise  # type: ignore[assignment]
    return reuse


def _snapshot_bodies(
    *,
    tasks: list[TaskSpec],
    config: EffectiveConfig,
    db: LedgerDB,
) -> dict[str, dict[str, Any]]:
    return {
        "manifest": {
            "engine": "latent-diag-snapshot-v1",
            "task_count": len(tasks),
            "tasks": [task.storage_dict() for task in tasks],
            "manifest_hash": hash_json([task.computational_payload(config) for task in tasks]),
        },
        "budget": {
            "engine": "latent-diag-budget-v1",
            "budget": config.budget,
            "tolerance": config.tolerance,
            "config_hash": config.compute_hash,
            "budget_hash": config.budget_hash,
        },
        "ledger": db.ledger_snapshot(),
        "cache": db.cache_snapshot(),
    }


def _status_for(
    *,
    rejected: dict[str, dict[str, Any]],
    tolerance_failed: list[str],
    failed: list[str],
) -> str:
    if failed:
        return "task_error"
    if tolerance_failed:
        return "tolerance_failed"
    if rejected:
        return "partial"
    return "ok"


def run_engine(
    *,
    tasks_path: str | Path,
    config_path: str | Path,
    db_path: str | Path,
    state_dir: str | Path,
    worker_count: int | None = None,
    tasks_per_batch: int | None = None,
    recompute_committed: bool = False,
    crash_after_checkpoints: int | None = None,
) -> dict[str, Any]:
    tasks_file = Path(tasks_path)
    config = load_config(config_path).with_overrides(
        worker_count=worker_count,
        tasks_per_batch=tasks_per_batch,
    )
    tasks = load_manifest(tasks_file)
    validate_manifest(tasks, config)
    _validate_latent_inputs(tasks)

    state = Path(state_dir)
    db = LedgerDB(db_path)
    snapshots = SnapshotStore(state)
    blobs = BlobStore(state / "cache")
    try:
        pointer = snapshots.current_metadata()
        previous_body = (
            snapshots.load_domain("manifest")["body"] if pointer is not None else None
        )
        previous_tasks = _parse_previous_tasks(
            previous_body,
            db,
            base_dir=tasks_file.parent,
        )
        plan = plan_invalidations(previous_tasks, tasks)
        run_id = db.begin_run(
            config_hash=config.compute_hash,
            budget_hash=config.budget_hash,
            manifest_version=(
                int(pointer["bundle_version"]) if pointer is not None else None
            ),
        )
        admission: AdmissionResult = admit_tasks(tasks, config)
        task_by_id = {task.task_id: task for task in tasks}
        task_index = {task.task_id: index for index, task in enumerate(tasks)}

        for task_id in plan.affected:
            db.record_event(
                run_id,
                "invalidation",
                task_id,
                {"reasons": plan.reasons.get(task_id, [])},
            )
        for task_id in plan.removed:
            db.record_event(
                run_id,
                "invalidation",
                task_id,
                {"reasons": ["task_removed"]},
            )
        db.retire_tasks(plan.removed, run_id)

        for task_id, rejection in admission.rejected.items():
            task = task_by_id[task_id]
            db.record_rejection(
                run_id=run_id,
                task=task,
                input_hash=task.input_hash(config),
                rejection=rejection,
                task_index=task_index[task_id],
            )

        checkpoints = db.latest_checkpoints()
        reuse_decisions: dict[str, dict[str, Any]] = {}
        for task in tasks:
            if task.task_id not in admission.accepted:
                continue
            if recompute_committed:
                continue
            if task.task_id not in plan.reuse_candidates:
                continue
            input_hash = task.input_hash(config)
            checkpoint = checkpoints.get(task.task_id)
            if checkpoint is None:
                checkpoint = db.checkpoint_for(task.task_id, input_hash)
            if checkpoint is None or checkpoint.get("input_hash") != input_hash:
                continue
            ledger = db.ledger_for(task.task_id)
            if _validate_checkpoint(
                task=task,
                checkpoint=checkpoint,
                ledger=ledger,
                blobs=blobs,
            ):
                reuse_decisions[task.task_id] = checkpoint

        actual_reused: list[str] = []
        actual_recomputed: list[str] = []
        tolerance_failed: list[str] = []
        tolerance_failures: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        committed_count = 0
        max_workers = int(config.budget["worker_count"])
        pool: ProcessPoolExecutor | None = None
        context = multiprocessing.get_context("fork")

        try:
            for batch in admission.batches:
                jobs: list[tuple[str, dict[str, Any]]] = []
                for task_id in batch:
                    if task_id in reuse_decisions:
                        continue
                    task = task_by_id[task_id]
                    reuse = _reuse_for_compute(
                        task=task,
                        config=config,
                        db=db,
                        blobs=blobs,
                    )
                    jobs.append(
                        (
                            task_id,
                            {
                                "task": task.storage_dict(),
                                "config": config.raw,
                                "cache_root": str(blobs.root),
                                "reuse": reuse,
                                "task_index": task_index[task_id],
                            },
                        )
                    )
                if jobs:
                    if pool is None:
                        pool = ProcessPoolExecutor(
                            max_workers=max_workers,
                            mp_context=context,
                        )
                    futures = {
                        task_id: pool.submit(run_worker, payload)
                        for task_id, payload in jobs
                    }
                    computed = {task_id: future.result() for task_id, future in futures.items()}
                else:
                    computed = {}

                for task_id in batch:
                    task = task_by_id[task_id]
                    if task_id in reuse_decisions:
                        db.commit_reuse(
                            run_id=run_id,
                            task_index=task_index[task_id],
                            task=task,
                            checkpoint=reuse_decisions[task_id],
                        )
                        actual_reused.append(task_id)
                        if reuse_decisions[task_id].get("status") == "tolerance_failed":
                            tolerance_failed.append(task_id)
                            reuse_error = reuse_decisions[task_id].get("error") or {
                                "code": "E_TOLERANCE_EXCEEDED",
                                "task_id": task_id,
                            }
                            tolerance_failures.append(reuse_error)
                        committed_count += 1
                    else:
                        result = computed[task_id]
                        if not result.get("ok") and result.get("code"):
                            error = {
                                "code": result["code"],
                                "message": result.get("message", result["code"]),
                                "task_id": task_id,
                                "details": result.get("details", {}),
                            }
                            db.commit_error(
                                run_id=run_id,
                                task=task,
                                input_hash=task.input_hash(config),
                                error=error,
                            )
                            failed.append(error)
                        else:
                            previous_ledger = db.ledger_for(task_id)
                            previous_hash = (
                                previous_ledger["content_hash"]
                                if previous_ledger is not None
                                else None
                            )
                            db.commit_task_result(
                                run_id=run_id,
                                task_index=task_index[task_id],
                                task=task,
                                result=result,
                                previous_ledger_hash=(
                                    previous_hash if recompute_committed else None
                                ),
                            )
                            actual_recomputed.append(task_id)
                            if result["status"] == "tolerance_failed":
                                tolerance_failed.append(task_id)
                                tolerance_failures.append(result["error"])
                            committed_count += 1
                    if (
                        crash_after_checkpoints is not None
                        and committed_count >= crash_after_checkpoints
                    ):
                        if pool is not None:
                            pool.shutdown(wait=False, cancel_futures=True)
                        db.close()
                        os.kill(os.getpid(), signal.SIGKILL)
        finally:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)

        status = _status_for(
            rejected=admission.rejected,
            tolerance_failed=tolerance_failed,
            failed=[entry["task_id"] for entry in failed],
        )
        summary: dict[str, Any] = {
            "ok": status == "ok",
            "status": status,
            "run_id": run_id,
            "task_count": len(tasks),
            "invalidation": plan.to_dict(),
            "admission": admission.to_dict(),
            "results": {
                "reused": actual_reused,
                "recomputed": actual_recomputed,
                "tolerance_failed": tolerance_failed,
                "tolerance_failures": tolerance_failures,
                "failed": failed,
                "rejected": list(admission.rejected),
                "reuse_recompute_disjoint": set(actual_reused).isdisjoint(
                    actual_recomputed
                ),
            },
            "budget_usage": {
                "total_denoise_iterations": admission.total_iterations,
                "peak_resident_latent_bytes": admission.peak_memory_bytes,
                "max_total_denoise_iterations": int(
                    config.budget["max_total_denoise_iterations"]
                ),
                "max_resident_latent_bytes": int(
                    config.budget["max_resident_latent_bytes"]
                ),
            },
            "schedule": {
                "dispatch_order": list(admission.accepted),
                "merge_order": list(admission.accepted),
                "batches": [list(batch) for batch in admission.batches],
            },
            "error_codes": sorted(
                {
                    *[entry["code"] for entry in failed],
                    *[
                        rejection["code"]
                        for rejection in admission.rejected.values()
                    ],
                    *[
                        failure["code"]
                        for failure in tolerance_failures
                    ],
                }
            ),
        }
        bodies = _snapshot_bodies(tasks=tasks, config=config, db=db)
        try:
            snapshot_metadata = snapshots.commit_bundle(bodies)
        except EngineError as exc:
            summary["snapshot_error"] = exc.to_dict()
            db.finish_run(run_id, "stage_busy" if exc.code == "E_STAGE_BUSY" else "snapshot_error", summary)
            raise
        db.record_snapshot_versions(snapshot_metadata)
        summary["snapshots"] = {
            "bundle_version": snapshot_metadata["bundle_version"],
            "generation": snapshot_metadata["generation"],
            "versions": {
                domain: envelope["version"]
                for domain, envelope in snapshot_metadata["domains"].items()
            },
            "checksums": {
                domain: envelope["checksum"]
                for domain, envelope in snapshot_metadata["domains"].items()
            },
        }
        summary["checkpoints_committed"] = committed_count
        db.finish_run(run_id, status, summary)
        return summary
    finally:
        db.close()

