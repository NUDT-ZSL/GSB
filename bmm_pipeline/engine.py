from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .config import PipelineConfig
from .errors import PipelineError, error
from .jobs import (
    JobSpec,
    job_from_dict,
    jobs_fingerprint,
    read_job_records,
    validate_job,
)
from .kernels import compute_job
from .snapshots import (
    STAGES,
    CommittedSnapshot,
    SnapshotStore,
    atomic_write_json,
    decode_array,
    encode_array,
    sha256_bytes,
)
from .strategy import StrategyPlan, choose_strategy, plan_from_dict


FaultHook = Callable[[dict[str, Any]], None]


@dataclass
class RunReport:
    run_dir: str
    status: str
    current_stage: str
    snapshot_version: int
    job_count: int
    committed_stages: list[dict[str, Any]]
    resumed_from_stage: str | None
    rebuilt_stages: list[str]
    discarded_blocks: int
    executed_stages: list[str] = field(default_factory=list)
    summaries: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_dir": self.run_dir,
            "status": self.status,
            "current_stage": self.current_stage,
            "snapshot_version": self.snapshot_version,
            "job_count": self.job_count,
            "recovery_report": {
                "committed_stages": self.committed_stages,
                "resumed_from_stage": self.resumed_from_stage,
                "rebuilt_stages": self.rebuilt_stages,
                "executed_stages": self.executed_stages,
                "discarded_blocks": self.discarded_blocks,
            },
            "job_summaries": self.summaries,
        }


class PipelineEngine:
    def __init__(self, run_dir: str | Path, config: PipelineConfig | None = None):
        self.run_dir = Path(run_dir)
        self.config = config or PipelineConfig()
        self.store = SnapshotStore(self.run_dir)

    def run(
        self,
        jobs_path: str | Path,
        *,
        fault_hook: FaultHook | None = None,
    ) -> RunReport:
        try:
            return self._run(Path(jobs_path), fault_hook)
        except PipelineError as exc:
            self.record_error(exc)
            raise

    def _run(self, jobs_path: Path, fault_hook: FaultHook | None) -> RunReport:
        records, source_checksum = read_job_records(jobs_path)
        if len(records) > self.config.max_stage_jobs:
            raise error(
                "E_RESOURCE_LIMIT",
                "单阶段任务数超过配置上限",
                field="max_stage_jobs",
                stage="validation",
                details={
                    "job_count": len(records),
                    "max_stage_jobs": self.config.max_stage_jobs,
                },
            )
        if not records:
            raise error(
                "E_JOB_INVALID",
                "任务清单为空",
                field="jobs_path",
                stage="validation",
            )

        committed = self.store.load_committed()
        self._validate_config_chain(committed)
        if committed and self.store.needs_current_sync(committed[-1]):
            self.store.sync_current()
        latest_index = committed[-1].version if committed else 0
        resumed_from = committed[-1].stage if committed else None
        committed_summary = [snapshot.summary() for snapshot in committed]
        rebuilt_stages = list(STAGES[latest_index:])
        progress = self.store.read_progress()
        discarded_blocks = self._discarded_blocks(progress, latest_index)
        self.store.clear_progress()

        jobs: list[JobSpec] | None = None
        jobset_fingerprint: str | None = None
        validation_snapshot = self.store.snapshot_for_stage(committed, "validation")
        if validation_snapshot is not None:
            jobs = [
                job_from_dict(payload)
                for payload in validation_snapshot.payload["jobs"]
            ]
            jobset_fingerprint = jobs_fingerprint(jobs)
            if validation_snapshot.jobset_fingerprint != jobset_fingerprint:
                raise error(
                    "E_RESUME_MISMATCH",
                    "已提交校验阶段的任务指纹与快照内容不一致",
                    stage="validation",
                    version=validation_snapshot.version,
                    details={
                        "snapshot_fingerprint": validation_snapshot.jobset_fingerprint,
                        "current_fingerprint": jobset_fingerprint,
                    },
                )
            stored_source = validation_snapshot.payload.get("source_checksum")
            if stored_source != source_checksum:
                raise error(
                    "E_RESUME_MISMATCH",
                    "任务清单文件在已提交校验阶段后发生变化",
                    stage="validation",
                    version=validation_snapshot.version,
                    details={
                        "snapshot_source_checksum": stored_source,
                        "current_source_checksum": source_checksum,
                    },
                )

        executed_stages: list[str] = []
        for stage in STAGES[latest_index:]:
            executed_stages.append(stage)
            if stage == "validation":
                jobs, jobset_fingerprint = self._run_validation(
                    records, source_checksum
                )
            elif stage == "planning":
                if jobs is None or jobset_fingerprint is None:
                    raise error(
                        "E_STAGE_INVALID",
                        "缺少校验阶段任务输入",
                        stage=stage,
                    )
                self._run_planning(jobs, jobset_fingerprint)
            elif stage == "accumulation":
                self._run_accumulation(fault_hook)
            elif stage == "aggregation":
                self._run_aggregation()

        final_snapshots = self.store.load_committed()
        latest = final_snapshots[-1]
        summaries: list[dict[str, Any]] = []
        aggregation = self.store.snapshot_for_stage(final_snapshots, "aggregation")
        if aggregation is not None:
            summaries = list(aggregation.payload.get("summary", {}).get("jobs", []))
        return RunReport(
            run_dir=str(self.run_dir),
            status="complete" if latest.stage == "aggregation" else "incomplete",
            current_stage=latest.stage,
            snapshot_version=latest.version,
            job_count=latest.job_count,
            committed_stages=committed_summary,
            resumed_from_stage=resumed_from,
            rebuilt_stages=rebuilt_stages,
            discarded_blocks=discarded_blocks,
            executed_stages=executed_stages,
            summaries=summaries,
        )

    def _validate_config_chain(
        self, snapshots: list[CommittedSnapshot]
    ) -> None:
        fingerprint = self.config.fingerprint()
        for snapshot in snapshots:
            if snapshot.config_fingerprint != fingerprint:
                raise error(
                    "E_RESUME_MISMATCH",
                    "恢复配置与已提交快照配置指纹不一致",
                    stage=snapshot.stage,
                    version=snapshot.version,
                    details={
                        "snapshot_fingerprint": snapshot.config_fingerprint,
                        "current_fingerprint": fingerprint,
                    },
                )

    def _discarded_blocks(
        self, progress: dict[str, Any] | None, latest_index: int
    ) -> int:
        if not progress:
            return 0
        progress_index = progress.get("stage_index")
        if not isinstance(progress_index, int) or progress_index <= latest_index:
            return 0
        if progress.get("stage") != "accumulation":
            return 0
        blocks = progress.get("blocks_done", 0)
        return int(blocks) if isinstance(blocks, int) and blocks >= 0 else 0

    def _run_validation(
        self, records: list[dict[str, Any]], source_checksum: str
    ) -> tuple[list[JobSpec], str]:
        self.store.write_progress(
            {
                "stage": "validation",
                "stage_index": 1,
                "jobs_done": 0,
                "total_jobs": len(records),
                "blocks_done": 0,
                "total_blocks": 0,
            }
        )
        jobs: list[JobSpec] = []
        seen: set[str] = set()
        for line_number, record in enumerate(records, start=1):
            job = validate_job(record, line_number)
            if job.job_id in seen:
                raise error(
                    "E_JOB_INVALID",
                    f"重复的 job_id: {job.job_id}",
                    job_id=job.job_id,
                    field="job_id",
                    stage="validation",
                    details={"line": line_number},
                )
            seen.add(job.job_id)
            jobs.append(job)
            self.store.write_progress(
                {
                    "stage": "validation",
                    "stage_index": 1,
                    "jobs_done": len(jobs),
                    "total_jobs": len(records),
                    "blocks_done": 0,
                    "total_blocks": 0,
                }
            )
        fingerprint = jobs_fingerprint(jobs)
        payload = {
            "jobs": [job.to_dict() for job in jobs],
            "source_checksum": source_checksum,
            "validated_jobs": len(jobs),
            "config": self.config.to_dict(),
        }
        self.store.commit_stage(
            "validation",
            payload,
            [job.job_id for job in jobs],
            expected_jobs=[job.job_id for job in jobs],
            config_fingerprint=self.config.fingerprint(),
            jobset_fingerprint=fingerprint,
        )
        self.store.clear_progress()
        return jobs, fingerprint

    def _run_planning(
        self, jobs: list[JobSpec], jobset_fingerprint: str
    ) -> None:
        self.store.write_progress(
            {
                "stage": "planning",
                "stage_index": 2,
                "jobs_done": 0,
                "total_jobs": len(jobs),
                "blocks_done": 0,
                "total_blocks": 0,
            }
        )
        plans: dict[str, dict[str, Any]] = {}
        for index, job in enumerate(jobs, start=1):
            plan = choose_strategy(job, self.config)
            plans[job.job_id] = plan.to_dict()
            self.store.write_progress(
                {
                    "stage": "planning",
                    "stage_index": 2,
                    "jobs_done": index,
                    "total_jobs": len(jobs),
                    "blocks_done": 0,
                    "total_blocks": 0,
                }
            )
        total_blocks = sum(int(plan["block_count"]) for plan in plans.values())
        payload = {
            "plans": plans,
            "total_planned_blocks": total_blocks,
            "degraded_jobs": sorted(
                job_id for job_id, plan in plans.items() if plan["degraded"]
            ),
        }
        self.store.commit_stage(
            "planning",
            payload,
            [job.job_id for job in jobs],
            expected_jobs=[job.job_id for job in jobs],
            config_fingerprint=self.config.fingerprint(),
            jobset_fingerprint=jobset_fingerprint,
        )
        self.store.clear_progress()

    def _jobs_and_plans(
        self,
    ) -> tuple[list[JobSpec], dict[str, StrategyPlan]]:
        snapshots = self.store.load_committed()
        validation = self.store.snapshot_for_stage(snapshots, "validation")
        planning = self.store.snapshot_for_stage(snapshots, "planning")
        if validation is None or planning is None:
            raise error(
                "E_STAGE_INCOMPLETE",
                "累加阶段缺少已提交的校验或策略快照",
                stage="accumulation",
            )
        jobs = [job_from_dict(payload) for payload in validation.payload["jobs"]]
        plans = {
            job_id: plan_from_dict(payload)
            for job_id, payload in planning.payload["plans"].items()
        }
        return jobs, plans

    def _run_accumulation(self, fault_hook: FaultHook | None) -> None:
        jobs, plans = self._jobs_and_plans()
        total_blocks = sum(plan.block_count for plan in plans.values())
        planned_total = int(
            self.store.snapshot_for_stage(
                self.store.load_committed(), "planning"
            ).payload["total_planned_blocks"]
        )
        if total_blocks != planned_total:
            raise error(
                "E_INCONSISTENT",
                "策略快照计划分块总数与重新加载的计划不一致",
                stage="accumulation",
                details={
                    "total_blocks": total_blocks,
                    "planned_total": planned_total,
                },
            )
        blocks_done = 0
        jobs_done = 0

        def progress(current_job: str) -> None:
            self.store.write_progress(
                {
                    "stage": "accumulation",
                    "stage_index": 3,
                    "jobs_done": jobs_done,
                    "total_jobs": len(jobs),
                    "blocks_done": blocks_done,
                    "total_blocks": total_blocks,
                    "current_job": current_job,
                }
            )

        progress("")
        results: dict[str, dict[str, Any]] = {}
        total_compensation = 0
        total_flushed = 0
        max_abs_error = 0.0
        for job in jobs:
            plan = plans[job.job_id]

            def hook(job_id: str, tile_in_job: int) -> None:
                nonlocal blocks_done
                blocks_done += 1
                progress(job_id)
                if fault_hook is not None:
                    fault_hook(
                        {
                            "stage": "accumulation",
                            "job_id": job_id,
                            "tile_in_job": tile_in_job,
                            "blocks_done": blocks_done,
                            "total_blocks": total_blocks,
                        }
                    )

            run = compute_job(job, plan, self.config, hook=hook)
            if run.stats.block_count != plan.block_count:
                raise error(
                    "E_INCONSISTENT",
                    "实际分块数与策略记录不一致",
                    job_id=job.job_id,
                    stage="accumulation",
                    details={
                        "actual_blocks": run.stats.block_count,
                        "planned_blocks": plan.block_count,
                    },
                )
            encoded = encode_array(run.result)
            results[job.job_id] = {
                "array": encoded,
                "result_checksum": encoded["checksum"],
                "shape": list(run.result.shape),
                "diagnostics": run.diagnostics,
            }
            jobs_done += 1
            total_compensation += run.stats.compensation_interventions
            total_flushed += run.stats.flushed_subnormals
            max_abs_error = max(
                max_abs_error, float(run.diagnostics["max_abs_error"])
            )
            progress(job.job_id)
        if blocks_done != total_blocks:
            raise error(
                "E_INCONSISTENT",
                "累计分块数与策略计划不一致",
                stage="accumulation",
                details={
                    "blocks_done": blocks_done,
                    "total_blocks": total_blocks,
                },
            )
        job_ids = [job.job_id for job in jobs]
        payload = {
            "results": results,
            "total_blocks": blocks_done,
            "total_compensation_interventions": total_compensation,
            "total_flushed_subnormals": total_flushed,
            "max_abs_error": max_abs_error,
        }
        validation_snapshot = self.store.snapshot_for_stage(
            self.store.load_committed(), "validation"
        )
        self.store.commit_stage(
            "accumulation",
            payload,
            job_ids,
            expected_jobs=job_ids,
            config_fingerprint=self.config.fingerprint(),
            jobset_fingerprint=validation_snapshot.jobset_fingerprint,
        )
        self.store.clear_progress()

    def _run_aggregation(self) -> None:
        snapshots = self.store.load_committed()
        validation = self.store.snapshot_for_stage(snapshots, "validation")
        planning = self.store.snapshot_for_stage(snapshots, "planning")
        accumulation = self.store.snapshot_for_stage(snapshots, "accumulation")
        if validation is None or planning is None or accumulation is None:
            raise error(
                "E_STAGE_INCOMPLETE",
                "聚合阶段缺少前置已提交快照",
                stage="aggregation",
            )
        jobs = [job_from_dict(payload) for payload in validation.payload["jobs"]]
        self.store.write_progress(
            {
                "stage": "aggregation",
                "stage_index": 4,
                "jobs_done": 0,
                "total_jobs": len(jobs),
                "blocks_done": 0,
                "total_blocks": 0,
            }
        )
        output_records: dict[str, dict[str, Any]] = {}
        summaries: list[dict[str, Any]] = []
        for index, job in enumerate(jobs, start=1):
            result_payload = accumulation.payload["results"][job.job_id]
            result = decode_array(result_payload["array"])
            if list(result.shape) != list(job.output_shape):
                raise error(
                    "E_INCONSISTENT",
                    "累加快照结果形状与业务对象不一致",
                    job_id=job.job_id,
                    stage="aggregation",
                    details={
                        "actual_shape": list(result.shape),
                        "expected_shape": list(job.output_shape),
                    },
                )
            output_name = f"{hashlib.sha256(job.job_id.encode('utf-8')).hexdigest()[:20]}.npy"
            output_path = self.store.results_dir / output_name
            file_checksum = self._write_npy_atomic(output_path, result)
            summary = {
                "job_id": job.job_id,
                "op_kind": job.op_kind,
                "shape": list(job.output_shape),
                "dtype": job.dtype,
                "layout": job.layout,
                "accum_mode": job.accum_mode,
                "result_checksum": result_payload["result_checksum"],
                "output_file": output_name,
                "output_checksum": file_checksum,
                "diagnostics": result_payload["diagnostics"],
            }
            summaries.append(summary)
            output_records[job.job_id] = {
                "file": output_name,
                "checksum": file_checksum,
                "shape": list(job.output_shape),
                "result_checksum": result_payload["result_checksum"],
            }
            self.store.write_progress(
                {
                    "stage": "aggregation",
                    "stage_index": 4,
                    "jobs_done": index,
                    "total_jobs": len(jobs),
                    "blocks_done": 0,
                    "total_blocks": 0,
                }
            )
        overall = {
            "job_count": len(jobs),
            "total_blocks": int(accumulation.payload["total_blocks"]),
            "compensation_interventions": int(
                accumulation.payload["total_compensation_interventions"]
            ),
            "flushed_subnormals": int(
                accumulation.payload["total_flushed_subnormals"]
            ),
            "max_abs_error": float(accumulation.payload["max_abs_error"]),
            "degraded_jobs": sorted(
                job_id
                for job_id, plan in planning.payload["plans"].items()
                if plan["degraded"]
            ),
            "tolerance_ok": True,
        }
        summary_payload = {"jobs": summaries, "overall": overall}
        summary_path = self.run_dir / "final_results.json"
        atomic_write_json(summary_path, summary_payload)
        summary_bytes = summary_path.read_bytes()
        payload = {
            "outputs": output_records,
            "summary": summary_payload,
            "summary_file": summary_path.name,
            "summary_checksum": sha256_bytes(summary_bytes),
        }
        job_ids = [job.job_id for job in jobs]
        self.store.commit_stage(
            "aggregation",
            payload,
            job_ids,
            expected_jobs=job_ids,
            config_fingerprint=self.config.fingerprint(),
            jobset_fingerprint=validation.jobset_fingerprint,
        )
        self.store.clear_progress()

    def _write_npy_atomic(self, path: Path, array: np.ndarray) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                np.save(handle, np.asarray(array, dtype=np.float64, order="C"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            return sha256_bytes(path.read_bytes())
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def record_error(self, exc: PipelineError) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **exc.to_dict(),
        }
        line = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        descriptor = os.open(
            self.store.error_log_path,
            os.O_CREAT | os.O_APPEND | os.O_WRONLY,
            0o644,
        )
        try:
            os.write(descriptor, (line + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def recent_errors(self, limit: int = 10) -> list[dict[str, Any]]:
        if not self.store.error_log_path.exists():
            return []
        lines = self.store.error_log_path.read_text(encoding="utf-8").splitlines()
        records: list[dict[str, Any]] = []
        for line in lines[-max(1, limit) :]:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records
