from __future__ import annotations

import concurrent.futures
import heapq
from dataclasses import dataclass
from typing import Any

from .batching import (
    build_job_plan,
    build_shard_demands,
    cross_worker_duplicates,
    truth_batch,
    wave_count,
)
from .determinism import generate_shard, shard_generation_cost_ms
from .errors import BusinessError, ErrorCode
from .locking import FileLock
from .models import CacheKey, JobPlan, JobSpec, ShardDemand, Workload
from .storage import StateStore


@dataclass(frozen=True)
class CacheResolution:
    kind: str
    digest: str | None
    origin: str
    reason: str
    detail: dict[str, Any]

    @property
    def hit(self) -> bool:
        return self.kind != "miss"


class LogicalClock:
    """An injected integer clock.  No method reads the system wall clock."""

    def __init__(self, *, start_ms: int, step_ms: int) -> None:
        self.now_ms = int(start_ms)
        self.step_ms = int(step_ms)

    def quantize(self, duration_ms: int) -> int:
        if duration_ms <= 0:
            return 0
        return ((duration_ms + self.step_ms - 1) // self.step_ms) * self.step_ms

    def advance(self, duration_ms: int) -> int:
        self.now_ms += self.quantize(duration_ms)
        return self.now_ms


def _initial_state(workload: Workload, plan: JobPlan) -> dict[str, Any]:
    cache_generation = workload.cache.generation
    return {
        "schema": 1,
        "run_id": None,
        "job": plan.job.name,
        "dataset": plan.dataset.name,
        "dataset_fingerprint": plan.dataset_fingerprint,
        "cache_generation": cache_generation,
        "next_round": 0,
        "clock_ms": workload.config.logical_clock_start_ms,
        "memory_bytes": 0,
        "memory_peak_bytes": 0,
        "loaded_shards": {},
        "committed_batches": [],
        "abandoned": [],
        "not_started": [],
        "failures": [],
        "dataset_stats": {},
        "cache_reused": [],
        "cache_invalidated": [],
        "batch_invalidations": [],
        "batch_cache_hits": [],
        "cache_decisions": [],
        "recovery_actions": [],
        "actual_timeouts": [],
    }


def _dataset_stats(state: dict[str, Any], dataset: str) -> dict[str, Any]:
    stats = state["dataset_stats"].setdefault(
        dataset,
        {
            "cache_hits": 0,
            "cache_misses": 0,
            "generated": 0,
            "reused_bytes": 0,
            "abandoned": 0,
            "not_started": 0,
            "failed": 0,
            "demand_count": 0,
        },
    )
    return stats


def _prepare_batch_invalidations(
    store: StateStore, plan: JobPlan
) -> tuple[list[dict[str, Any]], list[str]]:
    current_by_semantic = {
        (batch.epoch, batch.worker_id, batch.local_index): batch.batch_key
        for batch in plan.batches
    }
    invalidated: list[dict[str, Any]] = []
    hits: list[str] = []
    cached_by_semantic: dict[tuple[int, int, int], str] = {}
    for row in store.find_stale_batches(
        job_name=plan.job.name,
        dataset_name=plan.dataset.name,
        dataset_fingerprint_value=plan.dataset_fingerprint,
    ):
        semantic = (row["epoch"], row["worker_id"], row["local_index"])
        cached_by_semantic[semantic] = row["batch_key"]
    for semantic, current_key in sorted(current_by_semantic.items()):
        old_key = cached_by_semantic.get(semantic)
        if old_key == current_key:
            hits.append(current_key)
        elif old_key is not None:
            invalidated.append(
                {
                    "trigger": "dataset_version_or_generation_parameters_changed",
                    "epoch": semantic[0],
                    "worker_id": semantic[1],
                    "local_index": semantic[2],
                    "old_batch_key": old_key,
                    "new_batch_key": current_key,
                }
            )
    return invalidated, sorted(hits)


def _cache_key(plan: JobPlan, workload: Workload, shard_index: int) -> CacheKey:
    return CacheKey(
        dataset=plan.dataset.name,
        shard_index=shard_index,
        cache_generation=workload.cache.generation,
        generation_fingerprint=plan.dataset_fingerprint,
    )


def _cache_resolution(
    *,
    store: StateStore,
    workload: Workload,
    plan: JobPlan,
    key: CacheKey,
) -> CacheResolution:
    if not plan.dataset.cacheable:
        return CacheResolution(
            kind="miss",
            digest=None,
            origin="generated",
            reason="dataset_not_cacheable",
            detail={},
        )
    content = store.get_content(key)
    if content is not None and content["state"] == "ready":
        try:
            loaded = store.blobs.read(key)
            if loaded is not None:
                _, manifest = loaded
                if (
                    manifest["digest"] == content["digest"]
                    and int(manifest["bytes"]) == int(content["bytes"])
                ):
                    return CacheResolution(
                        kind="content",
                        digest=content["digest"],
                        origin="content_cache",
                        reason="content_addressed_hit",
                        detail={
                            "key": key.as_dict(),
                            "digest": content["digest"],
                        },
                    )
        except BusinessError as exc:
            content = None
            content_error = exc.as_dict()["error"]
        else:
            content_error = {
                "code": ErrorCode.CACHE_FILE_CORRUPT.value,
                "message": "content row has no readable blob",
            }
    else:
        content_error = None

    entry = store.get_cache_entry(key)
    if entry is not None:
        exact = (
            int(entry["generation"]) == key.cache_generation
            and entry["generation_fingerprint"] == key.generation_fingerprint
        )
        if exact and int(entry["bytes"]) == plan.dataset.shard_bytes:
            return CacheResolution(
                kind="cache_view",
                digest=entry["digest"],
                origin=entry["origin"],
                reason="cache_view_hit",
                detail={
                    "key": key.as_dict(),
                    "digest": entry["digest"],
                    "last_used_clock": entry["last_used_clock"],
                },
            )
        triggers: list[str] = []
        if int(entry["generation"]) != key.cache_generation:
            triggers.append("cache_generation_changed")
        if entry["generation_fingerprint"] != key.generation_fingerprint:
            triggers.append("sample_generation_parameters_changed")
        if int(entry["bytes"]) != plan.dataset.shard_bytes:
            triggers.append("cached_byte_size_changed")
        return CacheResolution(
            kind="miss",
            digest=None,
            origin="generated",
            reason="incremental_cache_invalidation",
            detail={
                "key": key.as_dict(),
                "triggers": triggers,
                "cached_generation": entry["generation"],
                "cached_fingerprint": entry["generation_fingerprint"],
                "cached_digest": entry["digest"],
                "content_error": content_error,
            },
        )
    stale_content = store.get_content_for_shard(
        key.dataset, key.shard_index
    )
    if stale_content is not None:
        triggers = []
        if int(stale_content["generation"]) != key.cache_generation:
            triggers.append("cache_generation_changed")
        if stale_content["generation_fingerprint"] != key.generation_fingerprint:
            triggers.append("sample_generation_parameters_changed")
        if triggers:
            return CacheResolution(
                kind="miss",
                digest=None,
                origin="generated",
                reason="incremental_cache_invalidation",
                detail={
                    "key": key.as_dict(),
                    "triggers": triggers,
                    "cached_generation": stale_content["generation"],
                    "cached_fingerprint": stale_content[
                        "generation_fingerprint"
                    ],
                    "cached_digest": stale_content["digest"],
                    "content_error": content_error,
                },
            )
    return CacheResolution(
        kind="miss",
        digest=None,
        origin="generated",
        reason="cache_miss",
        detail={"key": key.as_dict(), "content_error": content_error},
    )


def _generate_one(
    *,
    workload: Workload,
    plan: JobPlan,
    shard_index: int,
) -> tuple[bytes, str]:
    data, digest, _ = generate_shard(
        global_seed=workload.global_seed,
        dataset=plan.dataset,
        dataset_fingerprint_value=plan.dataset_fingerprint,
        shard_index=shard_index,
    )
    return data, digest


def _persist_generated_content(
    *,
    store: StateStore,
    workload: Workload,
    plan: JobPlan,
    key: CacheKey,
    data: bytes,
    digest: str,
    clock: int,
) -> tuple[str, bool]:
    """Persist under a per-key file lock, reusing a concurrent winner."""

    if not plan.dataset.cacheable:
        return digest, False
    lock_path = (
        store.lock_dir
        / "shards"
        / f"{store.blobs.key_hash(key)}.lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(lock_path):
        existing = store.get_content(key)
        if existing is not None and existing["state"] == "ready":
            loaded = store.blobs.read(key)
            if loaded is not None and loaded[1]["digest"] == existing["digest"]:
                if existing["digest"] != digest:
                    raise BusinessError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "concurrent shard generation produced a different digest",
                        details={
                            "key": key.as_dict(),
                            "existing_digest": existing["digest"],
                            "new_digest": digest,
                        },
                    )
                return existing["digest"], True
        manifest = store.blobs.write(key, data, digest)
        store.commit_content(
            key=key,
            digest=digest,
            byte_count=len(data),
            manifest=manifest,
            clock=clock,
        )
    return digest, False


def _simulate_times(
    *,
    outcomes: list[dict[str, Any]],
    limit_per_dataset: int,
    budget_ms: int,
    clock_start_ms: int,
    clock_step_ms: int,
) -> tuple[int, bool]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for outcome in outcomes:
        grouped.setdefault(outcome["dataset"], []).append(outcome)
    makespan = 0
    any_not_started = False
    for _, tasks in sorted(grouped.items()):
        slots = [0 for _ in range(limit_per_dataset)]
        heapq.heapify(slots)
        blocked = False
        for task in tasks:
            duration = int(task["duration_ms"])
            start = slots[0]
            if blocked or start + duration > budget_ms:
                blocked = True
                task["outcome"] = "not_started"
                task["reason"] = "logical_time_budget"
                task["start_ms"] = None
                task["end_ms"] = None
                any_not_started = True
                continue
            end = start + duration
            heapq.heapreplace(slots, end)
            task["outcome"] = "started"
            task["start_ms"] = clock_start_ms + start
            task["end_ms"] = clock_start_ms + end
            makespan = max(makespan, end)
    rounded = 0
    if makespan > 0:
        rounded = ((makespan + clock_step_ms - 1) // clock_step_ms) * clock_step_ms
    return rounded, any_not_started


def _execute_round_tasks(
    *,
    store: StateStore,
    workload: Workload,
    plan: JobPlan,
    outcomes: list[dict[str, Any]],
    resolutions: dict[int, CacheResolution],
    clock: int,
    tolerance_ms: int,
) -> bool:
    """Run accepted tasks concurrently and wait for every started task."""

    miss_tasks = [
        task
        for task in outcomes
        if task["outcome"] == "started" and not resolutions[task["shard_index"]].hit
    ]
    futures: dict[concurrent.futures.Future[tuple[bytes, str]], dict[str, Any]] = {}
    executors: list[concurrent.futures.Executor] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for task in miss_tasks:
        grouped.setdefault(task["dataset"], []).append(task)
    for _, tasks in sorted(grouped.items()):
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=workload.config.per_dataset_prefetch_limit,
            thread_name_prefix="shardpipe",
        )
        executors.append(executor)
        for task in tasks:
            future = executor.submit(
                _generate_one,
                workload=workload,
                plan=plan,
                shard_index=int(task["shard_index"]),
            )
            futures[future] = task

    timed_out = False
    if futures:
        actual_wait_seconds = max(
            0.001,
            (
                workload.config.logical_time_budget_ms
                + workload.config.tolerance_ms
            )
            / 1000.0,
        )
        done, pending = concurrent.futures.wait(
            futures, timeout=actual_wait_seconds
        )
        if pending:
            timed_out = True
            for future in pending:
                task = futures[future]
                task["timed_out"] = True
                # Started work is never abandoned.
                try:
                    future.result()
                except BaseException:
                    pass
        for future, task in futures.items():
            try:
                data, digest = future.result()
            except BaseException as exc:
                task["outcome"] = "started_failed"
                task["reason"] = "generation_error"
                task["error"] = f"{type(exc).__name__}: {exc}"
                continue
            if task.get("timed_out"):
                task["outcome"] = "started_failed"
                task["reason"] = "logical_and_actual_wait_timeout"
                task["error"] = "generation completed after the actual wait deadline"
                continue
            try:
                final_digest, concurrent_reuse = _persist_generated_content(
                    store=store,
                    workload=workload,
                    plan=plan,
                    key=_cache_key(plan, workload, int(task["shard_index"])),
                    data=data,
                    digest=digest,
                    clock=clock,
                )
            except BusinessError as exc:
                task["outcome"] = "started_failed"
                task["reason"] = "cache_commit_error"
                task["error"] = exc.message
                task["error_code"] = exc.code
                continue
            task["outcome"] = "generated"
            task["reason"] = (
                "concurrent_generation_reused"
                if concurrent_reuse
                else "cache_miss_generated"
            )
            task["digest"] = final_digest

    for executor in executors:
        executor.shutdown(wait=True)
    return timed_out


def _commit_ready_batches(
    *,
    store: StateStore,
    workload: Workload,
    plan: JobPlan,
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    loaded = set(state["loaded_shards"])
    committed = set(state["committed_batches"])
    result: list[dict[str, Any]] = []
    batch_cache_hit = False
    for batch in plan.batches:
        if batch.batch_id in committed:
            continue
        identities = {
            _cache_key(plan, workload, shard_index).identity()
            for shard_index in batch.source_shards
        }
        if not identities.issubset(loaded):
            continue
        truth = truth_batch(workload, plan, batch)
        existing = store.get_batch_result(batch.batch_key)
        if existing is None:
            existing = store.get_batch_result_by_digest(truth.batch_digest)
        if existing is not None:
            if existing["batch_digest"] != truth.batch_digest:
                raise BusinessError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "persisted batch cache digest does not match serial truth",
                    details={
                        "batch_id": batch.batch_id,
                        "batch_key": batch.batch_key,
                        "persisted": existing["batch_digest"],
                        "truth": truth.batch_digest,
                    },
                )
            batch_state = existing["state"]
            batch_cache_hit = True
            if batch.batch_key not in state["batch_cache_hits"]:
                state["batch_cache_hits"].append(batch.batch_key)
        else:
            batch_state = truth.as_dict()
        result.append(
            {
                "batch_id": batch.batch_id,
                "batch_key": batch.batch_key,
                "batch_digest": truth.batch_digest,
                "dataset": plan.dataset.name,
                "job_name": plan.job.name,
                "epoch": batch.epoch,
                "worker_id": batch.worker_id,
                "local_index": batch.local_index,
                "state": batch_state,
            }
        )
        committed.add(batch.batch_id)
    state["committed_batches"] = sorted(committed)
    return result, batch_cache_hit


def _run_round(
    *,
    store: StateStore,
    workload: Workload,
    plan: JobPlan,
    run_id: str,
    state: dict[str, Any],
    round_index: int,
    demands: tuple[ShardDemand, ...],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    config = workload.config
    clock = LogicalClock(
        start_ms=int(state["clock_ms"]),
        step_ms=config.logical_clock_step_ms,
    )
    round_start = clock.now_ms
    loaded = set(state["loaded_shards"])
    abandoned_keys = {
        item["key"] for item in state["abandoned"] if "key" in item
    }
    candidates = [
        demand
        for demand in demands
        if demand.wave_index == round_index
        and _cache_key(plan, workload, demand.shard_index).identity() not in loaded
        and _cache_key(plan, workload, demand.shard_index).identity()
        not in abandoned_keys
    ]
    candidates.sort(
        key=lambda demand: (demand.earliest_global_ordinal, demand.shard_index)
    )

    outcomes: list[dict[str, Any]] = []
    resolutions: dict[int, CacheResolution] = {}
    admitted: list[dict[str, Any]] = []
    planned_memory = int(state["memory_bytes"])
    memory_exhausted = False
    for demand in candidates:
        key = _cache_key(plan, workload, demand.shard_index)
        outcome = {
            "dataset": demand.dataset,
            "shard_index": demand.shard_index,
            "cache_generation": demand.cache_generation,
            "generation_fingerprint": demand.generation_fingerprint,
            "bytes": demand.bytes,
            "wave_index": demand.wave_index,
            "worker_ids": list(demand.worker_ids),
            "batch_ids": list(demand.batch_ids),
            "key": key.identity(),
            "outcome": "pending",
            "reason": "",
            "start_ms": None,
            "end_ms": None,
            "duration_ms": 0,
        }
        outcomes.append(outcome)
        _dataset_stats(state, demand.dataset)["demand_count"] += 1
        if memory_exhausted:
            outcome["outcome"] = "abandoned"
            outcome["reason"] = "memory_budget_exhausted"
            continue
        if planned_memory + demand.bytes > config.global_memory_budget_bytes:
            memory_exhausted = True
            outcome["outcome"] = "abandoned"
            outcome["reason"] = "memory_budget_exhausted"
            continue
        planned_memory += demand.bytes
        resolution = _cache_resolution(
            store=store, workload=workload, plan=plan, key=key
        )
        resolutions[demand.shard_index] = resolution
        outcome["cache_kind"] = resolution.kind
        outcome["cache_reason"] = resolution.reason
        if resolution.hit:
            outcome["duration_ms"] = clock.quantize(config.cache_hit_ms)
        else:
            raw_cost = shard_generation_cost_ms(
                byte_count=demand.bytes,
                base_ms=config.generation_base_ms,
                bytes_per_ms=config.generation_bytes_per_ms,
                step_ms=config.logical_clock_step_ms,
            )
            outcome["duration_ms"] = clock.quantize(raw_cost)
        admitted.append(outcome)
        decision = {
            "dataset": demand.dataset,
            "shard_index": demand.shard_index,
            "kind": resolution.kind,
            "reason": resolution.reason,
            "detail": resolution.detail,
        }
        state["cache_decisions"].append(decision)
        if resolution.hit:
            state["cache_reused"].append(
                {
                    "dataset": demand.dataset,
                    "shard_index": demand.shard_index,
                    "digest": resolution.digest,
                    "source": resolution.kind,
                    "worker_ids": list(demand.worker_ids),
                }
            )
        elif resolution.reason == "incremental_cache_invalidation":
            state["cache_invalidated"].append(
                {
                    "dataset": demand.dataset,
                    "shard_index": demand.shard_index,
                    "triggers": resolution.detail.get("triggers", []),
                    "affected_batches": list(demand.batch_ids),
                    "cached_generation": resolution.detail.get(
                        "cached_generation"
                    ),
                    "new_generation": demand.cache_generation,
                }
            )

    elapsed, _ = _simulate_times(
        outcomes=admitted,
        limit_per_dataset=config.per_dataset_prefetch_limit,
        budget_ms=config.logical_time_budget_ms,
        clock_start_ms=round_start,
        clock_step_ms=config.logical_clock_step_ms,
    )
    logical_end = round_start + elapsed
    actual_timed_out = _execute_round_tasks(
        store=store,
        workload=workload,
        plan=plan,
        outcomes=admitted,
        resolutions=resolutions,
        clock=logical_end,
        tolerance_ms=config.tolerance_ms,
    )
    if actual_timed_out:
        logical_end = max(
            logical_end, round_start + config.logical_time_budget_ms
        )
        state["actual_timeouts"].append(
            {
                "round_index": round_index,
                "logical_deadline_ms": round_start
                + config.logical_time_budget_ms,
                "observed_at_clock_ms": logical_end,
            }
        )
    clock.now_ms = logical_end

    successful_shards: list[dict[str, Any]] = []
    cache_updates: list[dict[str, Any]] = []
    round_peak = int(state["memory_peak_bytes"])
    started_memory = int(state["memory_bytes"])
    for outcome in admitted:
        if outcome.get("start_ms") is not None:
            started_memory += int(outcome["bytes"])
        if outcome["outcome"] in {
            "not_started",
            "abandoned",
            "started_failed",
        }:
            continue
        digest = outcome.get("digest")
        if outcome["outcome"] == "started":
            resolution = resolutions[int(outcome["shard_index"])]
            if resolution.hit:
                outcome["outcome"] = "hit"
                outcome["reason"] = resolution.reason
                outcome["digest"] = resolution.digest
                digest = resolution.digest
            else:
                outcome["outcome"] = "started_failed"
                outcome["reason"] = "task_did_not_complete"
                continue
        if digest is None:
            outcome["outcome"] = "started_failed"
            outcome["reason"] = "missing_digest"
            continue
        identity = outcome["key"]
        if identity in state["loaded_shards"]:
            continue
        origin = (
            resolutions[int(outcome["shard_index"])].origin
            if outcome["outcome"] == "hit"
            else "generated"
        )
        shard_record = {
            "dataset": outcome["dataset"],
            "shard_index": outcome["shard_index"],
            "cache_generation": outcome["cache_generation"],
            "generation_fingerprint": outcome["generation_fingerprint"],
            "digest": digest,
            "bytes": int(outcome["bytes"]),
            "origin": origin,
        }
        successful_shards.append(shard_record)
        state["loaded_shards"][identity] = {
            **shard_record,
            "round_index": round_index,
        }
        if plan.dataset.cacheable:
            cache_updates.append(
                {
                    "dataset": shard_record["dataset"],
                    "shard_index": shard_record["shard_index"],
                    "generation": shard_record["cache_generation"],
                    "generation_fingerprint": shard_record[
                        "generation_fingerprint"
                    ],
                    "bytes": shard_record["bytes"],
                    "digest": shard_record["digest"],
                    "origin": origin,
                    "last_used_clock": logical_end,
                }
            )
        stats = _dataset_stats(state, outcome["dataset"])
        if outcome["outcome"] == "hit":
            stats["cache_hits"] += 1
            stats["reused_bytes"] += int(outcome["bytes"])
        else:
            stats["cache_misses"] += 1
            stats["generated"] += 1

    round_peak = max(round_peak, started_memory)
    for outcome in outcomes:
        if outcome["outcome"] == "abandoned":
            state["abandoned"].append(
                {
                    "dataset": outcome["dataset"],
                    "shard_index": outcome["shard_index"],
                    "key": outcome["key"],
                    "reason": outcome["reason"],
                    "wave_index": outcome["wave_index"],
                }
            )
            _dataset_stats(state, outcome["dataset"])["abandoned"] += 1
        elif outcome["outcome"] == "started_failed":
            state["failures"].append(
                {
                    "dataset": outcome["dataset"],
                    "shard_index": outcome["shard_index"],
                    "key": outcome["key"],
                    "reason": outcome["reason"],
                    "error": outcome.get("error"),
                }
            )
            _dataset_stats(state, outcome["dataset"])["failed"] += 1
        elif outcome["outcome"] == "not_started":
            state["not_started"].append(
                {
                    "dataset": outcome["dataset"],
                    "shard_index": outcome["shard_index"],
                    "key": outcome["key"],
                    "reason": outcome["reason"],
                    "wave_index": outcome["wave_index"],
                }
            )
            _dataset_stats(state, outcome["dataset"])["not_started"] += 1

    new_memory = sum(
        int(item["bytes"]) for item in state["loaded_shards"].values()
    )
    if new_memory > config.global_memory_budget_bytes:
        raise BusinessError(
            ErrorCode.STATE_CORRUPT,
            "resident shard memory exceeds the configured budget",
            details={
                "memory_bytes": new_memory,
                "budget_bytes": config.global_memory_budget_bytes,
            },
        )
    state["memory_bytes"] = new_memory
    state["memory_peak_bytes"] = max(
        int(state["memory_peak_bytes"]), round_peak
    )
    state["clock_ms"] = logical_end

    batches, batch_cache_hit = _commit_ready_batches(
        store=store, workload=workload, plan=plan, state=state
    )
    state["next_round"] = round_index + 1

    round_state = {
        "round_index": round_index,
        "logical_start_ms": round_start,
        "logical_end_ms": logical_end,
        "budget_ms": config.logical_time_budget_ms,
        "memory_before_bytes": int(
            state["memory_bytes"] - sum(item["bytes"] for item in successful_shards)
        ),
        "memory_after_bytes": new_memory,
        "memory_peak_bytes": int(state["memory_peak_bytes"]),
        "candidate_count": len(outcomes),
        "started_count": sum(
            1
            for item in outcomes
            if item["outcome"] in {"generated", "hit", "started_failed"}
        ),
        "outcomes": outcomes,
        "batches_committed": [batch["batch_id"] for batch in batches],
        "batch_cache_hit": batch_cache_hit,
        "actual_timed_out": actual_timed_out,
    }
    return round_state, {
        "successful_shards": successful_shards,
        "batches": batches,
        "cache_updates": cache_updates,
    }, state


def run_schedule(
    *,
    store: StateStore,
    workload: Workload,
    job: JobSpec,
    run_id: str | None = None,
    resume_run_id: str | None = None,
) -> dict[str, Any]:
    plan = build_job_plan(workload, job)
    if cross_worker_duplicates(plan):
        raise BusinessError(
            ErrorCode.STATE_CORRUPT,
            "planned worker streams contain duplicate samples",
            details={"duplicates": cross_worker_duplicates(plan)[:10]},
        )
    if resume_run_id is not None:
        run = store.get_run(resume_run_id)
        if run["job_name"] != job.name or run["workload_digest"] != workload.digest:
            raise BusinessError(
                ErrorCode.RUN_CONFLICT,
                "resume workload does not match the persisted run",
                details={"run_id": resume_run_id},
            )
        run_id = resume_run_id
    elif run_id is None:
        run_id = store.next_run_id(job.name, workload.digest)

    run_lock_path = store.lock_dir / "runs" / f"{_safe_lock_name(run_id)}.lock"
    run_lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(run_lock_path, blocking=False):
        existing = None
        try:
            existing = store.get_run(run_id)
        except BusinessError as exc:
            if exc.code != ErrorCode.RUN_NOT_FOUND.value:
                raise
        if existing is None:
            state = _initial_state(workload, plan)
            state["run_id"] = run_id
            initial_invalidations, batch_hits = _prepare_batch_invalidations(
                store, plan
            )
            state["batch_invalidations"] = initial_invalidations
            state["batch_cache_hits"] = batch_hits
            store.create_run(
                workload,
                job,
                run_id=run_id,
                clock=workload.config.logical_clock_start_ms,
                initial_state=state,
            )
            snapshot = store.get_snapshot(run_id)
        else:
            if existing["job_name"] != job.name:
                raise BusinessError(
                    ErrorCode.RUN_CONFLICT,
                    "run id belongs to another job",
                    details={"run_id": run_id, "job_name": existing["job_name"]},
                )
            snapshot = store.get_snapshot(run_id)
            state = snapshot["state"]

        if existing is not None and existing["status"] == "complete":
            return {
                "ok": True,
                "status": "complete",
                "run_id": run_id,
                "snapshot_version": snapshot["version"],
                "already_complete": True,
                "result": existing["result"],
            }

        recovery_actions = store.recover_cache(
            workload=workload,
            clock=int(state["clock_ms"]),
            run_id=run_id,
        )
        if recovery_actions:
            state["recovery_actions"].extend(recovery_actions)

        expected_batches = len(plan.batches)
        demands = build_shard_demands(plan, workload.cache.generation)
        rounds_total = wave_count(plan)
        current_round = int(state.get("next_round", 0))
        if rounds_total == 0 and current_round == 0:
            final_status = (
                "complete" if expected_batches == 0 else "incomplete"
            )
            store.finalize_run(
                run_id=run_id,
                status=final_status,
                result={
                    "status": final_status,
                    "expected_batches": expected_batches,
                    "committed_batches": 0,
                },
                clock=int(state["clock_ms"]),
            )
            snapshot = store.get_snapshot(run_id)
            return {
                "ok": True,
                "status": final_status,
                "run_id": run_id,
                "snapshot_version": snapshot["version"],
                "result": store.get_run(run_id)["result"],
            }

        for round_index in range(current_round, rounds_total):
            round_state, commit_payload, state = _run_round(
                store=store,
                workload=workload,
                plan=plan,
                run_id=run_id,
                state=state,
                round_index=round_index,
                demands=demands,
            )
            expected_snapshot = int(store.get_snapshot(run_id)["version"])
            store.commit_round(
                run_id=run_id,
                expected_snapshot_version=expected_snapshot,
                round_index=round_index,
                round_state=round_state,
                new_state=state,
                successful_shards=commit_payload["successful_shards"],
                batches=commit_payload["batches"],
                cache_updates=commit_payload["cache_updates"],
                cache_capacity=workload.config.cache_capacity_entries,
                run_status="running",
                clock=int(state["clock_ms"]),
            )

        missing = sorted(
            set(batch.batch_id for batch in plan.batches)
            - set(state["committed_batches"])
        )
        final_status = "complete" if not missing else "incomplete"
        stats = state.get("dataset_stats", {})
        hits = sum(int(item["cache_hits"]) for item in stats.values())
        misses = sum(int(item["cache_misses"]) for item in stats.values())
        final_result = {
            "status": final_status,
            "expected_batches": expected_batches,
            "committed_batches": len(state["committed_batches"]),
            "missing_batches": missing,
            "clock_ms": int(state["clock_ms"]),
            "memory_bytes": int(state["memory_bytes"]),
            "memory_peak_bytes": int(state["memory_peak_bytes"]),
            "cache_hits": hits,
            "cache_misses": misses,
            "cache_hit_rate": (hits / (hits + misses)) if hits + misses else None,
            "abandoned_shards": state["abandoned"],
            "not_started_shards": state["not_started"],
            "failures": state["failures"],
            "cache_invalidated": state["cache_invalidated"],
            "cache_reused": state["cache_reused"],
            "batch_invalidations": state["batch_invalidations"],
            "batch_cache_hits": state["batch_cache_hits"],
            "recovery_actions": state["recovery_actions"],
            "actual_timeouts": state["actual_timeouts"],
            "rounds": rounds_total,
        }
        store.finalize_run(
            run_id=run_id,
            status=final_status,
            result=final_result,
            clock=int(state["clock_ms"]),
        )
        snapshot = store.get_snapshot(run_id)
        return {
            "ok": True,
            "status": final_status,
            "run_id": run_id,
            "snapshot_version": snapshot["version"],
            "result": final_result,
        }


def _safe_lock_name(run_id: str) -> str:
    from .util import sha256_text

    return sha256_text(run_id)


def status_report(store: StateStore, run_id: str | None) -> dict[str, Any]:
    run = store.get_run(run_id)
    actual_run_id = run["run_id"]
    snapshot = store.get_snapshot(actual_run_id)
    state = snapshot["state"]
    loaded_shards = state.get("loaded_shards", {})
    committed_batch_ids = sorted(set(state.get("committed_batches", [])))
    rounds = [
        row
        for row in store.get_rounds(actual_run_id)
        if int(row["snapshot_version"]) <= int(snapshot["version"])
    ]
    status_workload = store.get_workload(run["workload_digest"])
    memory_by_dataset: dict[str, int] = {}
    for shard in loaded_shards.values():
        memory_by_dataset[shard["dataset"]] = memory_by_dataset.get(
            shard["dataset"], 0
        ) + int(shard["bytes"])
    abandoned_by_dataset: dict[str, list[dict[str, Any]]] = {}
    for item in state.get("abandoned", []):
        abandoned_by_dataset.setdefault(item["dataset"], []).append(
            {
                "shard_index": item["shard_index"],
                "reason": item["reason"],
                "wave_index": item["wave_index"],
            }
        )
    datasets: list[dict[str, Any]] = []
    for dataset in sorted(status_workload.datasets, key=lambda item: item.name):
        stats = state.get("dataset_stats", {}).get(
            dataset.name,
            {
                "cache_hits": 0,
                "cache_misses": 0,
                "generated": 0,
                "reused_bytes": 0,
                "abandoned": 0,
                "not_started": 0,
                "failed": 0,
                "demand_count": 0,
            },
        )
        attempts = int(stats["cache_hits"]) + int(stats["cache_misses"])
        datasets.append(
            {
                "dataset": dataset.name,
                "cache_hit_rate": (
                    int(stats["cache_hits"]) / attempts if attempts else None
                ),
                "cache_hits": int(stats["cache_hits"]),
                "cache_misses": int(stats["cache_misses"]),
                "generated": int(stats["generated"]),
                "reused_bytes": int(stats["reused_bytes"]),
                "memory_bytes": memory_by_dataset.get(dataset.name, 0),
                "abandoned_shards": sorted(
                    abandoned_by_dataset.get(dataset.name, []),
                    key=lambda item: (
                        item["shard_index"],
                        item["wave_index"],
                    ),
                ),
                "not_started_shards": int(stats["not_started"]),
                "failed_shards": int(stats["failed"]),
            }
        )
    round_summaries = [
        {
            "round_index": int(round_row["round_index"]),
            "logical_start_ms": int(round_row["logical_start_ms"]),
            "logical_end_ms": int(round_row["logical_end_ms"]),
            "peak_bytes": int(round_row["peak_bytes"]),
            "snapshot_version": int(round_row["snapshot_version"]),
        }
        for round_row in rounds
    ]
    return {
        "ok": True,
        "run_id": actual_run_id,
        "job_name": run["job_name"],
        "dataset": run["dataset_name"],
        "status": run["status"],
        "snapshot_version": int(snapshot["version"]),
        "clock_watermark_ms": int(snapshot["logical_clock_ms"]),
        "memory": {
            "budget_bytes": status_workload.config.global_memory_budget_bytes,
            "used_bytes": int(snapshot["memory_bytes"]),
            "peak_bytes": int(snapshot["memory_peak_bytes"]),
        },
        "batches": {
            "committed": len(committed_batch_ids),
            "batch_ids": committed_batch_ids,
        },
        "datasets": datasets,
        "abandoned_shards": sorted(
            state.get("abandoned", []),
            key=lambda item: (item["dataset"], item["shard_index"]),
        ),
        "not_started_shards": sorted(
            state.get("not_started", []),
            key=lambda item: (item["dataset"], item["shard_index"]),
        ),
        "failures": sorted(
            state.get("failures", []),
            key=lambda item: (item["dataset"], item["shard_index"]),
        ),
        "rounds": round_summaries,
        "recovery": store.recovered(actual_run_id),
        "cache_reused": sorted(
            state.get("cache_reused", []),
            key=lambda item: (item["dataset"], item["shard_index"]),
        ),
        "cache_invalidated": sorted(
            state.get("cache_invalidated", []),
            key=lambda item: (item["dataset"], item["shard_index"]),
        ),
        "batch_cache_hits": sorted(set(state.get("batch_cache_hits", []))),
        "batch_invalidations": sorted(
            state.get("batch_invalidations", []),
            key=lambda item: (
                item.get("epoch", -1),
                item.get("worker_id", -1),
                item.get("local_index", -1),
            ),
        ),
        "actual_timeouts": state.get("actual_timeouts", []),
    }
