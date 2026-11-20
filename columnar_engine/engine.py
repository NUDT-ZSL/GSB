from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .model import Dataset, ExecutionConfig, Plan
from .reference import shard_ranges, verify_equivalence
from .rewrite import rewrite_plan
from .scheduler import ScheduleResult, Scheduler
from .store import EngineStore, ShardSpec
from .util import load_json_strict, sha256_json
from .validation import load_config, load_dataset, parse_plan


MIB = 1024 * 1024


@dataclass
class RunSummary:
    run_id: str
    status: str
    db_path: str
    shard_count: int
    shard_status_counts: dict[str, int]
    rewrite: dict[str, Any]
    equivalence: dict[str, Any]
    scheduler: dict[str, Any] = field(default_factory=dict)
    final_digest: str | None = None
    result: dict[str, Any] | None = None
    error_spectrum: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _dtype_size(dtype_name: str) -> int:
    return {"bool": 1, "int64": 8, "float64": 8}[dtype_name]


def _batch_ids_for_range(dataset: Dataset, start: int, end: int) -> list[int]:
    result: list[int] = []
    cursor = 0
    for batch in dataset.batches:
        batch_end = cursor + batch.rows
        if start < batch_end and end > cursor:
            result.append(batch.batch_id)
        cursor = batch_end
    return result


class Engine:
    def __init__(
        self,
        db_path: str | Path = "data/engine.db",
        work_dir: str | Path = "var",
    ) -> None:
        self.db_path = Path(db_path)
        self.work_dir = Path(work_dir)
        self.store: EngineStore | None = None

    @classmethod
    def initialize(
        cls,
        db_path: str | Path = "data/engine.db",
        work_dir: str | Path = "var",
    ) -> "Engine":
        engine = cls(db_path, work_dir)
        engine.store = EngineStore.initialize(db_path, work_dir)
        return engine

    def _ensure_store(self) -> EngineStore:
        if self.store is None:
            self.store = EngineStore.initialize(self.db_path, self.work_dir)
        return self.store

    def prepare(
        self,
        batches_path: str | Path,
        plan_path: str | Path,
        config_path: str | Path,
    ) -> tuple[
        Dataset,
        ExecutionConfig,
        dict[str, Any],
        Plan,
        Plan,
        Any,
        dict[str, Any],
    ]:
        dataset = load_dataset(batches_path)
        config = load_config(config_path)
        original_raw = load_json_strict(plan_path, code="E_BAD_PLAN")
        original_plan = parse_plan(copy.deepcopy(original_raw), dataset, config)
        rewritten_plan, report = rewrite_plan(
            copy.deepcopy(original_raw), dataset, config
        )
        equivalence = (
            verify_equivalence(original_plan, rewritten_plan, dataset, config)
            if config.verify_rewrite
            else {
                "equivalent": None,
                "bitwise": None,
                "skipped": True,
            }
        )
        report_dict = report.to_dict()
        report_dict["equivalence"] = equivalence
        return (
            dataset,
            config,
            original_raw,
            original_plan,
            rewritten_plan,
            report,
            report_dict,
        )

    def verify_rewrite(
        self,
        batches_path: str | Path,
        plan_path: str | Path,
        config_path: str | Path,
    ) -> dict[str, Any]:
        (
            _dataset,
            _config,
            _original_raw,
            _original_plan,
            _rewritten_plan,
            _report,
            report_dict,
        ) = self.prepare(batches_path, plan_path, config_path)
        return report_dict

    def run_sample(
        self,
        sample_dir: str | Path,
        *,
        fail_shard_id: int | None = None,
        crash_after_results: int | None = None,
        crash_after_commits: int | None = None,
        logical_clock_ms: float | None = None,
    ) -> RunSummary:
        sample_dir = Path(sample_dir)
        return self.run_paths(
            sample_dir / "batches.json",
            sample_dir / "plan.json",
            sample_dir / "config.json",
            fail_shard_id=fail_shard_id,
            crash_after_results=crash_after_results,
            crash_after_commits=crash_after_commits,
            logical_clock_ms=logical_clock_ms,
        )

    def run_paths(
        self,
        batches_path: str | Path,
        plan_path: str | Path,
        config_path: str | Path,
        *,
        fail_shard_id: int | None = None,
        crash_after_results: int | None = None,
        crash_after_commits: int | None = None,
        logical_clock_ms: float | None = None,
    ) -> RunSummary:
        (
            dataset,
            config,
            original_raw,
            _original_plan,
            rewritten_plan,
            report,
            report_dict,
        ) = self.prepare(batches_path, plan_path, config_path)

        plan_digest = sha256_json(original_raw)
        config_digest = sha256_json(config.raw)
        data_digest = dataset.source_digest
        run_id = sha256_json(
            {
                "plan_digest": plan_digest,
                "config_digest": config_digest,
                "data_digest": data_digest,
            }
        )[:24]

        scan_node = rewritten_plan.nodes[rewritten_plan.chain[0]]
        row_bytes = sum(
            _dtype_size(dataset.column_types[column])
            for column in scan_node["columns"]
        )
        shards: list[ShardSpec] = []
        for shard_id, (start, end) in enumerate(
            shard_ranges(dataset.rows, config.vector_width)
        ):
            rows = end - start
            memory_mib = (rows * row_bytes + 4096) / MIB
            shards.append(
                ShardSpec(
                    shard_id=shard_id,
                    row_start=start,
                    row_end=end,
                    memory_mib=memory_mib,
                    batch_ids=_batch_ids_for_range(dataset, start, end),
                    logical_cost_ms=config.logical_clock_step_ms,
                )
            )

        store = self._ensure_store()
        run = store.ensure_run(
            run_id=run_id,
            plan_digest=plan_digest,
            config_digest=config_digest,
            data_digest=data_digest,
            original_plan=original_raw,
            rewritten_plan=report.rewritten_plan,
            config=config,
            rewrite_report=report_dict,
            shards=shards,
        )

        if logical_clock_ms is not None:
            store.set_logical_clock(run_id, logical_clock_ms)
        store.reset_stale_running(run_id)

        run = store.get_run(run_id) or run
        if run["status"] == "COMPLETE":
            scheduler_result = ScheduleResult()
            scheduler_result.final_result = (
                json.loads(run["result_json"]) if run.get("result_json") else None
            )
            scheduler_result.final_digest = run.get("final_digest")
            scheduler_result.error_spectrum = (
                json.loads(run["error_spectrum_json"])
                if run.get("error_spectrum_json")
                else None
            )
        else:
            scheduler = Scheduler(
                store=store,
                run_id=run_id,
                plan=rewritten_plan,
                dataset=dataset,
                config=config,
                fail_shard_id=fail_shard_id,
                crash_after_results=crash_after_results,
                crash_after_commits=crash_after_commits,
            )
            scheduler_result = scheduler.run()

        final_run = store.get_run(run_id) or {}
        counts = store.shard_counts(run_id)
        shard_rows = store.list_shards(run_id)
        scheduler_report = {
            "waves": scheduler_result.waves,
            "committed_this_call": scheduler_result.committed_this_call,
            "abandoned": scheduler_result.abandoned,
            "not_started": scheduler_result.not_started,
            "failed": scheduler_result.failed,
            "replayed_shards": scheduler_result.replayed_shards,
            "reused_result_shards": scheduler_result.reused_result_shards,
            "logical_clock_ms": final_run.get("logical_clock_ms"),
            "vector_width": config.vector_width,
            "max_parallel_tasks": config.max_parallel_tasks,
            "scan_memory_budget_mib": config.scan_memory_budget_mib,
            "global_memory_budget_mib": config.global_memory_budget_mib,
            "round_time_budget_ms": config.round_time_budget_ms,
            "shards": [
                {
                    "shard_id": shard["shard_id"],
                    "status": shard["status"],
                    "reason": shard["reason"],
                    "attempt": shard["attempt"],
                    "digest": shard["digest"],
                    "logical_start_ms": shard["logical_start_ms"],
                    "logical_end_ms": shard["logical_end_ms"],
                }
                for shard in shard_rows
            ],
        }
        error = None
        if final_run.get("error_code"):
            error = {
                "code": final_run["error_code"],
                "message": final_run.get("error_message"),
            }
        return RunSummary(
            run_id=run_id,
            status=str(final_run.get("status", "UNKNOWN")),
            db_path=str(self.db_path),
            shard_count=len(shards),
            shard_status_counts=counts,
            rewrite=report_dict,
            equivalence=report_dict["equivalence"],
            scheduler=scheduler_report,
            final_digest=scheduler_result.final_digest or final_run.get("final_digest"),
            result=scheduler_result.final_result,
            error_spectrum=scheduler_result.error_spectrum,
            error=error,
        )

    def snapshot(self, run_id: str) -> dict[str, Any]:
        return self._ensure_store().read_snapshot(run_id)

