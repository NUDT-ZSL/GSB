"""Deterministic parallel scheduler for batches of scene pipelines."""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .config import PipelineConfig
from .errors import MemoryBudgetError, RegistrationBudgetError
from .incremental import IncrementalPlan
from .manifest import SceneManifest, priority_order
from .memory import AdmissionDecision, arbitrate, build_request
from .pipeline import SceneRunResult, run_scene
from .snapshot import SnapshotStore


@dataclass
class WorkerTask:
    manifest: SceneManifest
    observation_dir: str
    snapshot_root: str
    config: PipelineConfig
    admission: AdmissionDecision
    fault_path: Optional[str]
    skip_stages: List[str]


@dataclass
class BatchReport:
    batch_index: int
    scene_ids: List[str]
    results: List[Dict[str, Any]]
    admissions: List[Dict[str, Any]]


@dataclass
class ScheduleReport:
    workers: int
    scenes_per_batch: int
    ordered_scene_ids: List[str]
    batches: List[BatchReport] = field(default_factory=list)
    arbitration: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workers": self.workers,
            "scenes_per_batch": self.scenes_per_batch,
            "ordered_scene_ids": list(self.ordered_scene_ids),
            "batches": [
                {
                    "batch_index": batch.batch_index,
                    "scene_ids": list(batch.scene_ids),
                    "results": batch.results,
                    "admissions": batch.admissions,
                }
                for batch in self.batches
            ],
            "arbitration": list(self.arbitration),
            "failures": list(self.failures),
        }


def deterministic_batches(scenes: Sequence[SceneManifest],
                          scenes_per_batch: int) -> List[List[SceneManifest]]:
    """Fixed chunk boundaries in priority order; no size-based rebalancing."""
    ordered = priority_order(scenes)
    return [
        ordered[start:start + scenes_per_batch]
        for start in range(0, len(ordered), scenes_per_batch)
    ]


def _worker_entry(task: WorkerTask) -> SceneRunResult:
    return run_scene(
        manifest=task.manifest,
        config=task.config,
        observation_dir=task.observation_dir,
        snapshot_root=task.snapshot_root,
        admission=task.admission,
        fault_path=task.fault_path,
        skip_stages=task.skip_stages,
    )


def _admit_scene(manifest: SceneManifest, config: PipelineConfig,
                 point_count: int, vertex_estimate: int) -> AdmissionDecision:
    request = build_request(
        manifest=manifest,
        point_count=point_count,
        vertex_estimate=vertex_estimate,
        texture_resolution=config.texture.resolution,
        icp_iterations=config.icp.max_iterations,
    )
    return arbitrate(
        request=request,
        memory_budget=config.memory.resident_budget_units,
        comparison_budget=config.memory.max_registration_comparisons,
        downgrade_order=config.memory.downgrade_order,
    )


def run_schedule(manifests: Sequence[SceneManifest],
                 config: PipelineConfig,
                 observation_dir: str,
                 snapshot_root: str,
                 point_count: int,
                 vertex_estimate: int,
                 fault_path: Optional[str] = None,
                 incremental_plans: Optional[Dict[str, IncrementalPlan]] = None,
                 ) -> ScheduleReport:
    ordered = priority_order(manifests)
    report = ScheduleReport(
        workers=config.workers,
        scenes_per_batch=config.scenes_per_batch,
        ordered_scene_ids=[scene.scene_id for scene in ordered],
    )
    store = SnapshotStore(snapshot_root)
    batches = deterministic_batches(manifests, config.scenes_per_batch)
    incremental_plans = incremental_plans or {}

    for batch_index, batch in enumerate(batches):
        tasks: List[WorkerTask] = []
        batch_scene_ids: List[str] = []
        batch_admissions: List[Dict[str, Any]] = []
        for manifest in batch:
            decision = _admit_scene(manifest, config, point_count, vertex_estimate)
            report.arbitration.append(decision.to_dict())
            plan = incremental_plans.get(manifest.scene_id)
            skip_stages = plan.reused_stages if plan else []
            if not decision.admitted:
                error_type = (
                    MemoryBudgetError
                    if decision.rejection_code == "E_MEMORY_BUDGET"
                    else RegistrationBudgetError
                )
                failure = error_type(
                    "resource arbitration rejected scene",
                    scene_id=manifest.scene_id,
                    stage="scheduler",
                    details=decision.to_dict(),
                ).to_failure()
                report.failures.append(failure.to_dict())
                continue
            batch_admissions.append(decision.to_dict())
            tasks.append(WorkerTask(
                manifest=manifest,
                observation_dir=observation_dir,
                snapshot_root=snapshot_root,
                config=config,
                admission=decision,
                fault_path=fault_path,
                skip_stages=skip_stages,
            ))
            batch_scene_ids.append(manifest.scene_id)

        # Deterministic dispatch order; executor.map preserves in-order merge.
        if config.workers <= 1:
            results = [_worker_entry(task) for task in tasks]
        else:
            ctx = concurrent.futures.ProcessPoolExecutor(
                max_workers=config.workers,
                mp_context=_fork_context(),
            )
            with ctx as executor:
                results = list(executor.map(_worker_entry, tasks))

        batch_results: List[Dict[str, Any]] = []
        for result in results:
            payload = result.to_dict()
            if not result.success and result.failure is not None:
                report.failures.append(result.failure.to_dict())
            batch_results.append(payload)
        report.batches.append(BatchReport(
            batch_index=batch_index,
            scene_ids=batch_scene_ids,
            results=batch_results,
            admissions=batch_admissions,
        ))
    return report


def _fork_context():
    import multiprocessing
    try:
        return multiprocessing.get_context("fork")
    except ValueError:  # pragma: no cover - platforms without fork
        return multiprocessing.get_context()


