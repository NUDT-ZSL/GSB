from __future__ import annotations

import math
import os
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from .diagnostics import build_error_spectrum
from .errors import EngineError, fail
from .model import Dataset, ExecutionConfig, Plan
from .ops import relation_to_wire
from .reference import materialize_prefix, shard_ranges
from .store import EngineStore


@dataclass
class ScheduleResult:
    committed_this_call: list[int] = field(default_factory=list)
    waves: list[dict[str, Any]] = field(default_factory=list)
    abandoned: list[dict[str, Any]] = field(default_factory=list)
    not_started: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    replayed_shards: list[int] = field(default_factory=list)
    reused_result_shards: list[int] = field(default_factory=list)
    final_result: dict[str, Any] | None = None
    final_digest: str | None = None
    error_spectrum: dict[str, Any] | None = None


def _worker_boundary(
    plan: Plan,
    dataset: Dataset,
    shard_id: int,
    row_start: int,
    row_end: int,
    fail_shard_id: int | None,
) -> dict[str, Any]:
    if fail_shard_id is not None and shard_id == fail_shard_id:
        raise EngineError(
            "E_SHARD_FAILURE",
            "injected shard execution failure",
            shard_id=shard_id,
            details={"fault_injection": "fail_shard_id"},
        )
    boundary, _first_stateful = materialize_prefix(
        plan, dataset, row_start, row_end, shard_id
    )
    return relation_to_wire(boundary)


class Scheduler:
    def __init__(
        self,
        *,
        store: EngineStore,
        run_id: str,
        plan: Plan,
        dataset: Dataset,
        config: ExecutionConfig,
        fail_shard_id: int | None = None,
        crash_after_results: int | None = None,
        crash_after_commits: int | None = None,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.plan = plan
        self.dataset = dataset
        self.config = config
        self.fail_shard_id = fail_shard_id
        self.crash_after_results = crash_after_results
        self.crash_after_commits = crash_after_commits
        self.result_count = 0
        self.commit_count = 0

    def run(self) -> ScheduleResult:
        result = ScheduleResult()
        initial = self.store.list_shards(self.run_id)
        result.reused_result_shards = [
            int(shard["shard_id"])
            for shard in initial
            if shard["status"] == "RESULT_READY"
        ]
        result.replayed_shards = [
            int(shard["shard_id"])
            for shard in initial
            if shard["status"] == "PENDING" and int(shard["attempt"]) > 0
        ]
        idle_rounds = 0
        while True:
            shards = self.store.list_shards(self.run_id)
            by_status: dict[str, list[dict[str, Any]]] = {}
            for shard in shards:
                by_status.setdefault(shard["status"], []).append(shard)

            pending = by_status.get("PENDING", [])
            ready = by_status.get("RESULT_READY", [])
            running = by_status.get("RUNNING", [])

            if ready:
                commit = self.store.commit_ready(
                    run_id=self.run_id,
                    plan=self.plan,
                    config=self.config,
                    shard_count=len(shards),
                    diagnostics_builder=build_error_spectrum,
                )
                committed = list(commit.get("committed", []))
                if committed:
                    result.committed_this_call.extend(committed)
                    self.commit_count += len(committed)
                    if (
                        self.crash_after_commits is not None
                        and self.commit_count >= self.crash_after_commits
                    ):
                        os._exit(137)
                if "result" in commit:
                    result.final_result = commit.get("result")
                    result.final_digest = commit.get("final_digest")
                    result.error_spectrum = commit.get("error_spectrum")

            run = self.store.get_run(self.run_id)
            if run and run["status"] == "COMPLETE":
                break

            if not pending:
                if running:
                    idle_rounds += 1
                    if idle_rounds > 1200:
                        fail(
                            "E_STATE",
                            "timed out waiting for shards owned by another executor",
                            details={"running_shards": [s["shard_id"] for s in running]},
                        )
                    time.sleep(0.05)
                    continue
                if ready:
                    continue
                break

            memory_abandon = self._memory_precheck(pending)
            if memory_abandon:
                self._abandon_pending(pending, memory_abandon, result)
                continue

            run = self.store.get_run(self.run_id)
            clock = float(run["logical_clock_ms"])
            logical_allowance = int(
                math.floor(
                    self.config.round_time_budget_ms / self.config.logical_clock_step_ms
                    + 1e-12
                )
            )
            if logical_allowance < 1:
                self._abandon_pending(pending, "ROUND_TIME_BUDGET_EXHAUSTED", result)
                continue
            wave_slots = min(
                self.config.max_parallel_tasks,
                logical_allowance,
            )

            started: list[tuple[dict[str, Any], str, float, float]] = []
            local_memory = 0.0
            external_wait = False
            for shard in pending:
                if len(started) >= wave_slots:
                    break
                memory = float(shard["memory_mib"])
                if local_memory + memory > self.config.global_memory_budget_mib + 1e-12:
                    continue
                logical_start = clock + len(started) * self.config.logical_clock_step_ms
                logical_end = logical_start + self.config.logical_clock_step_ms
                token, reason = self.store.try_claim(
                    run_id=self.run_id,
                    shard_id=int(shard["shard_id"]),
                    logical_start_ms=logical_start,
                    lease_seconds=3600.0,
                )
                if token is not None:
                    started.append((shard, token, logical_start, logical_end))
                    local_memory += memory
                    self.store.advance_logical_clock(self.run_id, logical_end)
                    continue
                if reason in {
                    "PARALLEL_TASK_CAPACITY",
                    "GLOBAL_MEMORY_BUDGET",
                }:
                    external_wait = True
                    break
                if reason and reason.startswith("STATUS_"):
                    continue

            if not started:
                if external_wait:
                    idle_rounds += 1
                    if idle_rounds > 1200:
                        fail(
                            "E_STATE",
                            "timed out waiting for global resource leases",
                        )
                    time.sleep(0.05)
                    continue
                remaining = [shard for shard in pending if shard["status"] == "PENDING"]
                if remaining and local_memory == 0.0:
                    self._abandon_pending(
                        remaining, "GLOBAL_MEMORY_BUDGET_EXHAUSTED", result
                    )
                    continue
                for shard in remaining:
                    self.store.record_not_started(
                        run_id=self.run_id,
                        shard_id=int(shard["shard_id"]),
                        reason="SCHEDULER_MADE_NO_PROGRESS",
                    )
                    result.not_started.append(
                        {"shard_id": shard["shard_id"], "reason": "SCHEDULER_MADE_NO_PROGRESS"}
                    )
                continue

            idle_rounds = 0
            wave = {
                "round_clock_start_ms": clock,
                "round_clock_end_ms": max(item[3] for item in started),
                "started": [int(item[0]["shard_id"]) for item in started],
                "max_parallel_tasks": self.config.max_parallel_tasks,
                "logical_time_allowance": logical_allowance,
            }
            failures: list[EngineError] = []
            with ThreadPoolExecutor(max_workers=len(started)) as executor:
                futures = {
                    executor.submit(
                        _worker_boundary,
                        self.plan,
                        self.dataset,
                        int(shard["shard_id"]),
                        int(shard["row_start"]),
                        int(shard["row_end"]),
                        self.fail_shard_id,
                    ): (shard, token, logical_start, logical_end)
                    for shard, token, logical_start, logical_end in started
                }
                for future in as_completed(futures):
                    shard, token, _logical_start, logical_end = futures[future]
                    shard_id = int(shard["shard_id"])
                    try:
                        payload = future.result()
                        self.store.record_result(
                            run_id=self.run_id,
                            shard_id=shard_id,
                            lease_token=token,
                            payload=payload,
                            logical_end_ms=logical_end,
                        )
                        self.result_count += 1
                        if (
                            self.crash_after_results is not None
                            and self.result_count >= self.crash_after_results
                        ):
                            os._exit(137)
                    except EngineError as error:
                        failures.append(error)
                        error.shard_id = shard_id
                        self.store.record_failure(
                            run_id=self.run_id,
                            shard_id=shard_id,
                            lease_token=token,
                            error=error,
                        )
                        result.failed.append(
                            {"shard_id": shard_id, **error.to_dict()}
                        )
                    except Exception as exc:  # pragma: no cover - defensive boundary
                        error = EngineError(
                            "E_SHARD_FAILURE",
                            f"unexpected shard failure: {exc}",
                            shard_id=shard_id,
                        )
                        failures.append(error)
                        self.store.record_failure(
                            run_id=self.run_id,
                            shard_id=shard_id,
                            lease_token=token,
                            error=error,
                        )
                        result.failed.append(
                            {"shard_id": shard_id, **error.to_dict()}
                        )

            current = {
                int(shard["shard_id"]): shard["status"]
                for shard in self.store.list_shards(self.run_id)
            }
            counts: dict[str, int] = {}
            for shard_id in wave["started"]:
                status = current.get(shard_id, "UNKNOWN")
                counts[status] = counts.get(status, 0) + 1
            wave["outcomes"] = counts
            result.waves.append(wave)

            commit = self.store.commit_ready(
                run_id=self.run_id,
                plan=self.plan,
                config=self.config,
                shard_count=len(shards),
                diagnostics_builder=build_error_spectrum,
            )
            committed = list(commit.get("committed", []))
            if committed:
                result.committed_this_call.extend(committed)
                self.commit_count += len(committed)
                if (
                    self.crash_after_commits is not None
                    and self.commit_count >= self.crash_after_commits
                ):
                    os._exit(137)
            if "result" in commit:
                result.final_result = commit.get("result")
                result.final_digest = commit.get("final_digest")
                result.error_spectrum = commit.get("error_spectrum")
            self.store.advance_logical_clock(
                self.run_id, max(item[3] for item in started)
            )
            if failures:
                self._mark_peer_failure_not_started(result)
                break

        self._refresh_outcomes(result)
        run = self.store.get_run(self.run_id) or {}
        if run.get("status") == "COMPLETE":
            result.final_result = (
                run.get("result")
                if "result" in run
                else (json.loads(run["result_json"]) if run.get("result_json") else None)
            )
            result.final_digest = run.get("final_digest")
            if run.get("error_spectrum_json"):
                result.error_spectrum = json.loads(run["error_spectrum_json"])
        else:
            self._set_incomplete_run_status(run, result)
        return result

    def _memory_precheck(
        self, pending: list[dict[str, Any]]
    ) -> str | None:
        for shard in pending:
            memory = float(shard["memory_mib"])
            if memory > self.config.scan_memory_budget_mib + 1e-12:
                return "SHARD_EXCEEDS_SCAN_MEMORY_BUDGET"
            if memory > self.config.global_memory_budget_mib + 1e-12:
                return "GLOBAL_MEMORY_BUDGET_EXHAUSTED"
        return None

    def _abandon_pending(
        self,
        pending: list[dict[str, Any]],
        reason: str,
        result: ScheduleResult,
    ) -> None:
        for shard in pending:
            if shard["status"] != "PENDING":
                continue
            self.store.record_abandoned(
                run_id=self.run_id,
                shard_id=int(shard["shard_id"]),
                reason=reason,
                logical_start_ms=float(
                    (self.store.get_run(self.run_id) or {}).get("logical_clock_ms", 0.0)
                ),
            )
            result.abandoned.append(
                {"shard_id": int(shard["shard_id"]), "reason": reason}
            )

    def _mark_peer_failure_not_started(self, result: ScheduleResult) -> None:
        for shard in self.store.list_shards(self.run_id):
            if shard["status"] == "PENDING":
                self.store.record_not_started(
                    run_id=self.run_id,
                    shard_id=int(shard["shard_id"]),
                    reason="PEER_SHARD_FAILURE",
                )
                result.not_started.append(
                    {"shard_id": int(shard["shard_id"]), "reason": "PEER_SHARD_FAILURE"}
                )

    def _refresh_outcomes(self, result: ScheduleResult) -> None:
        for shard in self.store.list_shards(self.run_id):
            if shard["status"] == "ABANDONED" and not any(
                item["shard_id"] == shard["shard_id"] for item in result.abandoned
            ):
                result.abandoned.append(
                    {
                        "shard_id": int(shard["shard_id"]),
                        "reason": shard["reason"] or "ABANDONED",
                    }
                )
            if shard["status"] == "NOT_STARTED" and not any(
                item["shard_id"] == shard["shard_id"] for item in result.not_started
            ):
                result.not_started.append(
                    {
                        "shard_id": int(shard["shard_id"]),
                        "reason": shard["reason"] or "NOT_STARTED",
                    }
                )
            if shard["status"] == "FAILED" and not any(
                item["shard_id"] == shard["shard_id"] for item in result.failed
            ):
                result.failed.append(
                    {
                        "shard_id": int(shard["shard_id"]),
                        "code": shard["reason"] or "E_SHARD_FAILURE",
                    }
                )

    def _set_incomplete_run_status(
        self, run: dict[str, Any], result: ScheduleResult
    ) -> None:
        status = run.get("status")
        if status == "COMPLETE":
            return
        counts = self.store.shard_counts(self.run_id)
        if result.failed or counts.get("FAILED", 0):
            self.store.mark_run_status(self.run_id, "FAILED_PARTIAL")
        elif result.abandoned or counts.get("ABANDONED", 0):
            self.store.mark_run_status(self.run_id, "ABANDONED_PARTIAL")
        elif result.not_started or counts.get("NOT_STARTED", 0):
            self.store.mark_run_status(self.run_id, "NOT_STARTED_PARTIAL")

