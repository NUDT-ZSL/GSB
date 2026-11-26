from __future__ import annotations

import multiprocessing as mp
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from .canonical import content_digest
from .errors import E_STAGE_BUSY, TuningError
from .ledger import SnapshotStore, atomic_write_json
from .planner import plan_search
from .recovery import recover_workspace
from .worker import evaluate_point


class TuningEngine:
    def __init__(
        self,
        output_dir: str | Path,
        tasks: list[dict[str, Any]],
        platform: dict[str, Any],
        policy: dict[str, Any],
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tasks = tasks
        self.platform = platform
        self.policy = policy
        self.plan = plan_search(tasks, platform, policy)
        self.store = SnapshotStore(self.output_dir)
        self.input_digest = content_digest(
            {"tasks": tasks, "platform": platform, "policy": policy}
        )

    def _fresh_state(self) -> dict[str, Any]:
        return {
            "input_digest": self.input_digest,
            "priority_order": list(self.policy["priority_order"]),
            "selected_plan": self.plan["selected"],
            "options_considered": self.plan["options_considered"],
            "rejections": self.plan["rejections"],
            "results": [],
            "committed_batches": [],
            "next_batch_no": 1,
            "logical_clock_used_ms": 0.0,
        }

    def _request_for(self, point: dict[str, Any], batch: dict[str, Any]) -> dict[str, Any]:
        total_threads = sum(
            max(1, int(schedule.get("threads", 1)))
            for schedule in [
                self.plan["point_index"][point_id]["schedule"]
                for point_id in batch["point_ids"]
            ]
        )
        staging_path = self.store.staging_dir / f"{point['point_id']}.partial.json"
        return {
            "point_id": point["point_id"],
            "tuning_id": point["tuning_id"],
            "op_kind": point["op_kind"],
            "workload_shape": point["workload_shape"],
            "quant_config": point["quant_config"],
            "effective_bits": point["effective_bits"],
            "schedule": point["schedule"],
            "platform": self.platform,
            "active_threads_in_batch": total_threads,
            "timing_repeats": int(self.policy["timing_repeats"]),
            "warmup_repeats": int(self.policy["warmup_repeats"]),
            "staging_path": str(staging_path),
        }

    def _finalize_worker_result(
        self, point: dict[str, Any], worker_result: dict[str, Any], batch_no: int
    ) -> dict[str, Any]:
        report = worker_result["quant_report"]
        estimate = worker_result["estimate"]
        measured = worker_result["measured"]
        relative_rmse = float(report["metrics"]["relative_rmse"])
        predicted_latency_us = float(estimate["expected_latency_us"])
        measured_latency_us = float(measured["latency_us"])
        ranking_score = (
            predicted_latency_us * (1.0 + relative_rmse)
            + 0.05 * float(estimate["resident_memory_mb"])
        )
        observed_score = (
            measured_latency_us * (1.0 + relative_rmse)
            + 0.05 * float(estimate["resident_memory_mb"])
        )
        return {
            "point_id": point["point_id"],
            "tuning_id": point["tuning_id"],
            "op_kind": point["op_kind"],
            "sequence_no": point["sequence_no"],
            "batch_no": batch_no,
            "logical_clock_ms": point["logical_clock_ms"],
            "priority": point["priority"],
            "schedule": point["schedule"],
            "effective_bits": point["effective_bits"],
            "quant_digest": point["quant_digest"],
            "quant_metrics_digest": report["metrics_digest"],
            "quant_report": report,
            "estimate": estimate,
            "measured": measured,
            "predicted_latency_us": predicted_latency_us,
            "prediction_error_ratio": (
                measured_latency_us / predicted_latency_us
                if predicted_latency_us > 0
                else None
            ),
            "ranking_score": ranking_score,
            "observed_score": observed_score,
        }

    def build_report(self, state: dict[str, Any], recovery_info: dict[str, Any] | None = None) -> dict[str, Any]:
        rankings: dict[str, list[dict[str, Any]]] = {}
        for result in state["results"]:
            rankings.setdefault(result["tuning_id"], []).append(
                {
                    "point_id": result["point_id"],
                    "schedule_id": result["schedule"]["schedule_id"],
                    "schedule_revision": result["schedule"].get("schedule_revision", 1),
                    "batch_no": result["batch_no"],
                    "ranking_score": result["ranking_score"],
                    "measured_latency_us": result["measured"]["latency_us"],
                    "predicted_latency_us": result["predicted_latency_us"],
                    "relative_rmse": result["quant_report"]["metrics"]["relative_rmse"],
                    "resident_memory_mb": result["estimate"]["resident_memory_mb"],
                }
            )
        best: dict[str, Any] = {}
        for tuning_id, items in rankings.items():
            ordered = sorted(
                items,
                key=lambda item: (
                    item["ranking_score"],
                    item["sequence_no"] if "sequence_no" in item else item["point_id"],
                ),
            )
            # Include sequence_no in compact ranking for stable tie-breaking.
            full_by_id = {result["point_id"]: result for result in state["results"]}
            ordered = sorted(
                items,
                key=lambda item: (
                    item["ranking_score"],
                    full_by_id[item["point_id"]]["sequence_no"],
                ),
            )
            rankings[tuning_id] = ordered
            best[tuning_id] = ordered[0]

        return {
            "ok": True,
            "input_digest": self.input_digest,
            "checkpoint_version": state.get("snapshot_version", 0),
            "committed_point_count": len(state["results"]),
            "committed_batches": state["committed_batches"],
            "logical_clock_used_ms": state["logical_clock_used_ms"],
            "selected_plan": state["selected_plan"],
            "rejections": state["rejections"],
            "rankings": rankings,
            "best": best,
            "recovery": recovery_info,
        }

    def run(
        self,
        resume: bool = False,
        stop_after_batches: int | None = None,
        write_report: bool = True,
    ) -> dict[str, Any]:
        recovery_info: dict[str, Any] | None = None
        if resume:
            full_recovery = recover_workspace(self.output_dir)
            record = full_recovery["snapshot"]
            recovery_info = {
                key: value for key, value in full_recovery.items() if key != "snapshot"
            }
            if record is None:
                state = self._fresh_state()
                expected_version = 0
                start_batch = 1
            else:
                state = record["payload"]
                expected_version = int(record["snapshot_version"])
                start_batch = int(state["next_batch_no"])
                if state.get("input_digest") != self.input_digest:
                    raise TuningError(
                        "E_INCONSISTENT",
                        "tasks/platform/policy changed relative to recovered snapshot",
                        {
                            "snapshot_input_digest": state.get("input_digest"),
                            "current_input_digest": self.input_digest,
                        },
                    )
        else:
            existing = self.store.latest_version()
            if existing:
                raise TuningError(
                    E_STAGE_BUSY,
                    f"output already contains checkpoint v{existing}; use resume",
                    {"checkpoint_version": existing},
                )
            state = self._fresh_state()
            expected_version = 0
            start_batch = 1

        committed_ids = {result["point_id"] for result in state["results"]}
        batches_by_no = {batch["batch_no"]: batch for batch in self.plan["batches"]}
        pending_batches = [
            batch for batch in self.plan["batches"] if batch["batch_no"] >= start_batch
        ]

        new_batches = 0
        context = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else None
        executor_kwargs: dict[str, Any] = {"max_workers": int(self.policy["worker_count"])}
        if context is not None:
            executor_kwargs["mp_context"] = context

        with ProcessPoolExecutor(**executor_kwargs) as executor:
            for batch in pending_batches:
                batch_no = int(batch["batch_no"])
                batch_path_ids = list(batch["point_ids"])
                batch_path_ids = [pid for pid in batch_path_ids if pid not in committed_ids]
                if not batch_path_ids:
                    state["next_batch_no"] = batch_no + 1
                    continue

                points = [self.plan["point_index"][pid] for pid in batch_path_ids]
                requests = [self._request_for(point, batch) for point in points]
                dispatch_path = self.store.work_dir / f"batch-{batch_no:06d}.dispatch.json"
                inflight_path = self.store.work_dir / f"batch-{batch_no:06d}.inflight"
                atomic_write_json(
                    dispatch_path,
                    {
                        "batch_no": batch_no,
                        "dispatch_order": [point["point_id"] for point in points],
                        "worker_count": self.policy["worker_count"],
                        "requests": requests,
                    },
                )
                inflight_path.write_text(
                    content_digest(
                        {"batch_no": batch_no, "point_ids": batch_path_ids, "pid": os.getpid()}
                    ),
                    encoding="utf-8",
                )

                batch_started = time.perf_counter_ns()
                worker_results = list(executor.map(evaluate_point, requests))
                batch_wall_ns = time.perf_counter_ns() - batch_started

                # Merge in dispatched plan order; never in process-completion order.
                results_by_id = {item["point_id"]: item for item in worker_results}
                for point in points:
                    worker_result = results_by_id[point["point_id"]]
                    state["results"].append(
                        self._finalize_worker_result(point, worker_result, batch_no)
                    )
                    committed_ids.add(point["point_id"])

                state["committed_batches"].append(batch_no)
                state["next_batch_no"] = batch_no + 1
                state["logical_clock_used_ms"] += float(batch["logical_cost_ms"])
                state.setdefault("batch_wall_ns", {})[str(batch_no)] = batch_wall_ns

                record = self.store.commit(state, expected_version=expected_version)
                expected_version = int(record["snapshot_version"])
                state["snapshot_version"] = expected_version
                state["checkpoint_version"] = expected_version

                # Commit is durable before marker cleanup. A kill between commit
                # and cleanup is safe: recovery retains the checkpoint and only
                # removes the stale inflight marker.
                cleanup_paths = [inflight_path, dispatch_path]
                cleanup_paths.extend(Path(request["staging_path"]) for request in requests)
                for path in cleanup_paths:
                    try:
                        Path(path).unlink()
                    except FileNotFoundError:
                        pass

                new_batches += 1
                if stop_after_batches is not None and new_batches >= stop_after_batches:
                    break

        complete = state["next_batch_no"] > len(self.plan["batches"])
        report = self.build_report(state, recovery_info)
        report["complete"] = complete
        report["new_committed_batches"] = new_batches
        if write_report and complete:
            report_path = self.output_dir / "reports" / "run-report.json"
            atomic_write_json(report_path, report)
            report["report_path"] = str(report_path)
        return report
