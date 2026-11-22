"""Core cache lifecycle, tiering, eviction, recovery, and diagnostics."""

from __future__ import annotations

import hashlib
import os
import signal
import sqlite3
from pathlib import Path
from typing import Any

from .errors import (
    E_ALL_RUNS_COMMITTED,
    E_ARTIFACT_MISSING,
    E_BAD_CONFIG,
    E_BAD_PIPELINE,
    E_CLOCK_ROLLBACK,
    E_CONFIG_CONFLICT,
    E_CONFIG_ROLLBACK,
    E_DIGEST_MISMATCH,
    E_EVICT_CANDIDATE_MISSING,
    E_OBJECT_MISSING,
    E_RUN_NOT_FOUND,
    E_STATE_CONFLICT,
    E_STATE_CORRUPT,
    E_STATE_NOT_LOADED,
    E_TASK_MISSING,
    BusinessError,
)
from .model import (
    ArtifactPlan,
    ConfigSpec,
    Dataset,
    TIER_RANK,
    TIERS,
    canonical_json,
    fp_value,
    high_precision_order,
    load_dataset,
    plan_artifacts,
    score_components,
)
from .store import Storage, remove_state, safe_json_load


class Engine:
    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.storage = Storage(self.state_dir)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def load_paths(
        self,
        tasks_path: str | Path,
        pipelines_path: str | Path,
        config_path: str | Path,
        *,
        reset: bool = False,
    ) -> dict[str, Any]:
        dataset = load_dataset(tasks_path, pipelines_path, config_path)
        return self.load(dataset, reset=reset)

    def load(self, dataset: Dataset, *, reset: bool = False) -> dict[str, Any]:
        # Digests and expected digests are checked before any stateful path is
        # created, so invalid input cannot leave a DB or CAS object behind.
        plans = plan_artifacts(dataset.tasks)
        plan_by_name = {plan.task.name: plan for plan in plans}

        if reset:
            remove_state(self.state_dir)
        storage = Storage(self.state_dir)
        config = dataset.config
        config_json = config.canonical_json()
        config_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()

        with storage.lock():
            storage.initialize()
            conn = storage.connection()
            try:
                loaded = storage.state_loaded(conn)
                active_version = storage.config_version(conn)
                if loaded and active_version is not None:
                    if config.version < active_version:
                        raise BusinessError(
                            E_CONFIG_ROLLBACK,
                            "capacity config version would roll back persisted state",
                            requested_version=config.version,
                            active_version=active_version,
                        )
                    row = conn.execute(
                        "SELECT content_hash FROM configs WHERE version=?",
                        (config.version,),
                    ).fetchone()
                    if (
                        config.version == active_version
                        and row is not None
                        and row["content_hash"] != config_hash
                    ):
                        raise BusinessError(
                            E_CONFIG_CONFLICT,
                            "the same config version has different contents",
                            requested_version=config.version,
                            active_version=active_version,
                        )
                if loaded:
                    self._assert_same_snapshot(conn, dataset, plan_by_name)
            finally:
                conn.close()

            # Object publication is idempotent and happens before the metadata
            # transaction. The CAS is immutable, so a crash here can only leave
            # harmless temporary or orphan objects.
            for plan in plans:
                storage.objects.materialize_generated(
                    plan.task.name,
                    plan.task.written_at_ms,
                    plan.size_bytes,
                    plan.digest,
                )

            with storage.transaction() as conn:
                loaded = storage.state_loaded(conn)
                active_version = storage.config_version(conn)
                if loaded and active_version is not None and config.version < active_version:
                    raise BusinessError(
                        E_CONFIG_ROLLBACK,
                        "capacity config version would roll back persisted state",
                        requested_version=config.version,
                        active_version=active_version,
                    )
                conn.execute(
                    "INSERT INTO configs(version, content_hash, config_json, loaded_order) "
                    "VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(version) DO UPDATE SET "
                    "content_hash=excluded.content_hash, config_json=excluded.config_json",
                    (
                        config.version,
                        config_hash,
                        config_json,
                        storage.next_counter(conn, "config_load_order"),
                    ),
                )
                storage.set_meta(conn, "active_config_version", config.version)

                if not loaded:
                    for index, plan in enumerate(plans, start=1):
                        self._insert_task_and_artifact(
                            conn, plan, config, load_order=index
                        )
                    for index, pipeline in enumerate(dataset.pipelines, start=1):
                        conn.execute(
                            "INSERT INTO pipelines(name, tasks_json, runs_json, load_order) "
                            "VALUES(?, ?, ?, ?)",
                            (
                                pipeline.name,
                                canonical_json(list(pipeline.tasks)),
                                canonical_json(
                                    [
                                        {"run_seq": run.run_seq, "at_ms": run.at_ms}
                                        for run in pipeline.runs
                                    ]
                                ),
                                index,
                            ),
                        )
                    storage.set_meta(conn, "state_loaded", "1")
                    storage.set_meta(conn, "logical_clock_ms", 0)
                    storage.set_meta(conn, "standalone_eviction_seq", 0)
                conn.execute(
                    "UPDATE artifacts SET error_scale=?",
                    (config.fixed_point_scale,),
                )
                self._refresh_accounts(
                    conn,
                    config,
                    pipeline="__load__",
                    run_seq=0,
                    at_ms=max((task.last_hit_at_ms for task in dataset.tasks), default=0),
                    before=None,
                    write_ledger=False,
                )

        return {
            "ok": True,
            "command": "load",
            "config_version": config.version,
            "tasks": len(dataset.tasks),
            "pipelines": len(dataset.pipelines),
            "objects": len(plans),
            "reset": reset,
        }

    def _assert_same_snapshot(
        self,
        conn: sqlite3.Connection,
        dataset: Dataset,
        plan_by_name: dict[str, ArtifactPlan],
    ) -> None:
        existing_tasks = {
            row["name"]: row for row in conn.execute("SELECT * FROM tasks ORDER BY name")
        }
        requested_tasks = {task.name: task for task in dataset.tasks}
        if set(existing_tasks) != set(requested_tasks):
            raise BusinessError(
                E_STATE_CONFLICT,
                "task set differs from the loaded snapshot; use --reset to replace state",
                existing=sorted(existing_tasks),
                requested=sorted(requested_tasks),
            )
        for name, row in existing_tasks.items():
            task = requested_tasks[name]
            plan = plan_by_name[name]
            comparisons = {
                "artifact_name": task.artifact_name,
                "size_bytes": task.size_bytes,
                "written_at_ms": task.written_at_ms,
                "last_hit_at_ms": task.last_hit_at_ms,
                "initial_hit_count": task.hit_count,
                "pinned": int(task.pinned),
                "expected_digest": task.expected_digest,
                "digest": plan.digest,
                "depends_json": canonical_json(list(task.depends_on)),
            }
            for field, requested in comparisons.items():
                if row[field] != requested:
                    raise BusinessError(
                        E_STATE_CONFLICT,
                        "task definition differs from the loaded snapshot; use --reset",
                        task_name=name,
                        field=field,
                        persisted=row[field],
                        requested=requested,
                    )

        existing_pipelines = {
            row["name"]: row
            for row in conn.execute("SELECT * FROM pipelines ORDER BY name")
        }
        requested_pipelines = {pipeline.name: pipeline for pipeline in dataset.pipelines}
        if set(existing_pipelines) != set(requested_pipelines):
            raise BusinessError(
                E_STATE_CONFLICT,
                "pipeline set differs from the loaded snapshot; use --reset",
                existing=sorted(existing_pipelines),
                requested=sorted(requested_pipelines),
            )
        for name, row in existing_pipelines.items():
            pipeline = requested_pipelines[name]
            tasks_json = canonical_json(list(pipeline.tasks))
            runs_json = canonical_json(
                [
                    {"run_seq": run.run_seq, "at_ms": run.at_ms}
                    for run in pipeline.runs
                ]
            )
            if row["tasks_json"] != tasks_json or row["runs_json"] != runs_json:
                raise BusinessError(
                    E_STATE_CONFLICT,
                    "pipeline definition differs from the loaded snapshot; use --reset",
                    pipeline=name,
                )

    def _insert_task_and_artifact(
        self, conn: sqlite3.Connection, plan: ArtifactPlan, config: ConfigSpec, load_order: int
    ) -> None:
        task = plan.task
        conn.execute(
            "INSERT INTO tasks("
            "name, depends_json, artifact_name, size_bytes, written_at_ms, "
            "last_hit_at_ms, initial_hit_count, pinned, expected_digest, digest, "
            "object_relpath, load_order"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.name,
                canonical_json(list(task.depends_on)),
                task.artifact_name,
                task.size_bytes,
                task.written_at_ms,
                task.last_hit_at_ms,
                task.hit_count,
                int(task.pinned),
                task.expected_digest,
                plan.digest,
                self.storage.objects.relative_path(plan.digest),
                load_order,
            ),
        )
        conn.execute(
            "INSERT INTO artifacts("
            "task_name, artifact_name, size_bytes, written_at_ms, digest, object_relpath, "
            "tier, hit_count, last_hit_ms, last_access_ms, pinned, error_scale"
            ") VALUES(?, ?, ?, ?, ?, ?, 'cold', ?, ?, ?, ?, ?)",
            (
                task.name,
                task.artifact_name,
                task.size_bytes,
                task.written_at_ms,
                plan.digest,
                self.storage.objects.relative_path(plan.digest),
                task.hit_count,
                task.last_hit_at_ms,
                task.last_hit_at_ms,
                int(task.pinned),
                config.fixed_point_scale,
            ),
        )

    # ------------------------------------------------------------------
    # Pipeline runs
    # ------------------------------------------------------------------
    def run(
        self,
        pipeline: str,
        *,
        run_seq: int | None = None,
        at_ms: int | None = None,
    ) -> dict[str, Any]:
        storage = self.storage
        with storage.lock():
            storage.initialize()
            result: dict[str, Any] | None = None
            with storage.transaction() as conn:
                if not storage.state_loaded(conn):
                    raise BusinessError(E_STATE_NOT_LOADED, "state has not been loaded")
                config = storage.active_config(conn)
                if config is None:
                    raise BusinessError(E_STATE_NOT_LOADED, "active capacity config is missing")
                pipe = self._pipeline(conn, pipeline)
                selected = self._select_run(pipe, conn, run_seq, allow_all_committed=False)
                if at_ms is not None and (
                    not isinstance(at_ms, int) or isinstance(at_ms, bool) or at_ms < 0
                ):
                    raise BusinessError(E_BAD_PIPELINE, "at_ms must be a non-negative integer")
                run_at = selected["at_ms"] if at_ms is None else at_ms

                existing = conn.execute(
                    "SELECT * FROM runs WHERE pipeline=? AND run_seq=?",
                    (pipeline, selected["run_seq"]),
                ).fetchone()
                if existing is not None:
                    if existing["status"] not in ("committed", "committed_with_errors"):
                        raise BusinessError(
                            E_STATE_CORRUPT,
                            "a non-committed run intent exists",
                            pipeline=pipeline,
                            run_seq=selected["run_seq"],
                            status=existing["status"],
                        )
                    result = self._run_output(
                        conn, config, pipeline, selected["run_seq"], idempotent=True
                    )
                else:
                    self._check_clock(conn, config, run_at, selected["run_seq"])
                    result = self._commit_run(
                        conn, config, pipe, selected["run_seq"], run_at
                    )
                    self._crash_if("mid_transaction")

            self._crash_if("after_run_commit")
            reclaimed = self._reclaim_pending(pipeline=pipeline, run_seq=selected["run_seq"])
            if reclaimed:
                with storage.transaction() as conn:
                    config = storage.active_config(conn)
                    if config is None:
                        raise BusinessError(E_STATE_NOT_LOADED, "active config disappeared")
                    result = self._run_output(
                        conn, config, pipeline, selected["run_seq"], idempotent=False
                    )
            return result

    def _commit_run(
        self,
        conn: sqlite3.Connection,
        config: ConfigSpec,
        pipe: dict[str, Any],
        run_seq: int,
        at_ms: int,
    ) -> dict[str, Any]:
        conn.execute(
            "INSERT INTO runs("
            "pipeline, run_seq, at_ms, status, config_version, task_results_json"
            ") VALUES(?, ?, ?, 'processing', ?, '[]')",
            (pipe["name"], run_seq, at_ms, config.version),
        )
        before_usage = self._usage(conn)
        task_results: list[dict[str, Any]] = []
        had_errors = False
        task_names = pipe["tasks"]

        for task_name in task_names:
            task = conn.execute("SELECT * FROM tasks WHERE name=?", (task_name,)).fetchone()
            if task is None:
                error = BusinessError(
                    E_TASK_MISSING,
                    "pipeline task is absent from the loaded task manifest",
                    run_seq=run_seq,
                    task_name=task_name,
                )
                self._record_error(conn, pipe["name"], run_seq, error)
                task_results.append(
                    {
                        "task_name": task_name,
                        "hit": False,
                        "reason": "task_missing",
                        "tier": None,
                        "tier_before": None,
                        "tier_after": None,
                        "error_bound": None,
                        "error": error.as_dict()["error"],
                    }
                )
                had_errors = True
                continue

            artifact = conn.execute(
                "SELECT * FROM artifacts WHERE task_name=?", (task_name,)
            ).fetchone()
            if artifact is None:
                error = BusinessError(
                    E_ARTIFACT_MISSING,
                    "task has no persisted artifact record",
                    run_seq=run_seq,
                    task_name=task_name,
                    tier=None,
                )
                self._record_error(conn, pipe["name"], run_seq, error)
                task_results.append(
                    {
                        "task_name": task_name,
                        "artifact_name": task["artifact_name"],
                        "hit": False,
                        "reason": "artifact_missing",
                        "tier": None,
                        "tier_before": None,
                        "tier_after": None,
                        "error_bound": None,
                        "error": error.as_dict()["error"],
                    }
                )
                had_errors = True
                continue

            tier_before = artifact["tier"]
            if artifact["evicted"]:
                reason = "evicted"
                self._record_ref(
                    conn, pipe["name"], run_seq, task_name, at_ms, False, reason
                )
                task_results.append(
                    self._miss_result(task_name, artifact, reason, tier_before)
                )
                continue

            if at_ms < artifact["written_at_ms"]:
                reason = "not_yet_written"
                self._record_ref(
                    conn, pipe["name"], run_seq, task_name, at_ms, False, reason
                )
                task_results.append(
                    self._miss_result(task_name, artifact, reason, tier_before)
                )
                continue

            try:
                self.storage.objects.verify(artifact["digest"], artifact["size_bytes"])
            except BusinessError as error:
                error.run_seq = run_seq
                error.task_name = task_name
                error.tier = tier_before
                self._record_error(conn, pipe["name"], run_seq, error)
                self._record_ref(
                    conn, pipe["name"], run_seq, task_name, at_ms, False, error.code
                )
                task_results.append(
                    self._miss_result(
                        task_name,
                        artifact,
                        "object_error",
                        tier_before,
                        error=error,
                    )
                )
                had_errors = True
                continue

            old_hits = int(artifact["hit_count"])
            new_hits = old_hits + 1
            old_rank = TIER_RANK[tier_before]
            new_rank = old_rank
            if new_hits >= config.promotion_min_hits and old_rank < 2:
                new_rank = old_rank + 1
            tier_after = TIERS[new_rank]

            conn.execute(
                "UPDATE artifacts SET hit_count=?, last_hit_ms=?, last_access_ms=?, "
                "last_access_run_seq=?, last_decision_run_seq=?, tier=?, "
                "error_scale=?, version=version+1 WHERE task_name=?",
                (
                    new_hits,
                    at_ms,
                    at_ms,
                    run_seq,
                    run_seq,
                    tier_after,
                    config.fixed_point_scale,
                    task_name,
                ),
            )
            self._record_ref(
                conn, pipe["name"], run_seq, task_name, at_ms, True, "hit"
            )
            if tier_after != tier_before:
                self._record_transition(
                    conn,
                    pipeline=pipe["name"],
                    run_seq=run_seq,
                    task_name=task_name,
                    trigger="promotion",
                    from_tier=tier_before,
                    to_tier=tier_after,
                    at_ms=at_ms,
                    start=run_seq,
                    end=run_seq,
                    details={
                        "hit_count": new_hits,
                        "promotion_min_hits": config.promotion_min_hits,
                    },
                )

            components = score_components(
                0,
                new_hits,
                max(1, new_hits),
                config,
            )
            cumulative = int(artifact["error_bound_num"])
            self._record_arithmetic(
                conn,
                pipe["name"],
                run_seq,
                task_name,
                operation="hit_count_add",
                scale=1,
                rounding="exact",
                raw_value=[old_hits, 1],
                converted_value=new_hits,
                error_num=0,
                cumulative_error_num=cumulative,
                details={"field": "hit_count"},
            )
            for operation in components["operations"]:
                self._record_arithmetic(
                    conn,
                    pipe["name"],
                    run_seq,
                    task_name,
                    operation=operation.operation,
                    scale=operation.scale,
                    rounding=operation.rounding,
                    raw_value=operation.raw_value,
                    converted_value=operation.converted_value,
                    error_num=operation.error_num,
                    cumulative_error_num=cumulative + operation.cumulative_error_num,
                    details={"field": "eviction_score_at_hit"},
                )
            cumulative += int(components["error_bound"]["numerator"])
            conn.execute(
                "UPDATE artifacts SET error_bound_num=? WHERE task_name=?",
                (cumulative, task_name),
            )

            task_results.append(
                {
                    "task_name": task_name,
                    "artifact_name": artifact["artifact_name"],
                    "digest": artifact["digest"],
                    "hit": True,
                    "reason": "hit",
                    "tier": tier_after,
                    "tier_before": tier_before,
                    "tier_after": tier_after,
                    "hit_count": new_hits,
                    "last_hit_ms": at_ms,
                    "last_access_ms": at_ms,
                    "idle_ms": 0,
                    "error_bound": fp_value(cumulative, config.fixed_point_scale),
                }
            )

        transitions = self._demote_idle(
            conn, config, pipe["name"], run_seq, at_ms
        )
        self._enforce_capacity(
            conn,
            config,
            pipeline=pipe["name"],
            run_seq=run_seq,
            at_ms=at_ms,
            forced_tasks=None,
        )
        self._refresh_accounts(
            conn,
            config,
            pipeline=pipe["name"],
            run_seq=run_seq,
            at_ms=at_ms,
            before=before_usage,
            write_ledger=True,
        )
        current_clock = storage_clock = 0
        raw_clock = self.storage.get_meta(conn, "logical_clock_ms")
        storage_clock = int(raw_clock) if raw_clock is not None else 0
        current_clock = max(storage_clock, at_ms)
        self.storage.set_meta(conn, "logical_clock_ms", current_clock)
        status = "committed_with_errors" if had_errors else "committed"
        conn.execute(
            "UPDATE runs SET status=?, task_results_json=? WHERE pipeline=? AND run_seq=?",
            (status, canonical_json(task_results), pipe["name"], run_seq),
        )
        return self._run_output(conn, config, pipe["name"], run_seq, idempotent=False)

    def _demote_idle(
        self,
        conn: sqlite3.Connection,
        config: ConfigSpec,
        pipeline: str,
        run_seq: int,
        at_ms: int,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE evicted=0 AND pinned=0 AND tier != 'cold' "
            "ORDER BY task_name"
        ).fetchall()
        changed: list[dict[str, Any]] = []
        for artifact in rows:
            idle_ms = at_ms - int(artifact["last_access_ms"])
            if idle_ms <= config.demotion_idle_ms:
                continue
            old_tier = artifact["tier"]
            new_tier = TIERS[TIER_RANK[old_tier] - 1]
            start = int(artifact["last_decision_run_seq"])
            conn.execute(
                "UPDATE artifacts SET tier=?, last_decision_run_seq=?, version=version+1 "
                "WHERE task_name=?",
                (new_tier, run_seq, artifact["task_name"]),
            )
            self._record_transition(
                conn,
                pipeline=pipeline,
                run_seq=run_seq,
                task_name=artifact["task_name"],
                trigger="demotion",
                from_tier=old_tier,
                to_tier=new_tier,
                at_ms=at_ms,
                start=start,
                end=run_seq,
                details={
                    "idle_ms": idle_ms,
                    "demotion_idle_ms": config.demotion_idle_ms,
                    "pinned": False,
                },
            )
            self._record_arithmetic(
                conn,
                pipeline,
                run_seq,
                artifact["task_name"],
                operation="idle_sub",
                scale=config.fixed_point_scale,
                rounding="exact",
                raw_value=[at_ms, artifact["last_access_ms"]],
                converted_value=idle_ms,
                error_num=0,
                cumulative_error_num=0,
                details={"field": "idle_ms", "trigger": "demotion"},
            )
            changed.append(
                {
                    "task_name": artifact["task_name"],
                    "from_tier": old_tier,
                    "to_tier": new_tier,
                    "idle_ms": idle_ms,
                    "run_seq_start": start,
                    "run_seq_end": run_seq,
                }
            )
        return changed

    # ------------------------------------------------------------------
    # Eviction and reclaim
    # ------------------------------------------------------------------
    def _enforce_capacity(
        self,
        conn: sqlite3.Connection,
        config: ConfigSpec,
        *,
        pipeline: str,
        run_seq: int,
        at_ms: int,
        forced_tasks: list[str] | None,
    ) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE evicted=0 ORDER BY tier, task_name"
        ).fetchall()
        max_hits = max((int(row["hit_count"]) for row in rows), default=0)
        candidates = [self._candidate(row, at_ms, max_hits, config) for row in rows]
        by_name = {candidate["task_name"]: candidate for candidate in candidates}
        usage = {tier: 0 for tier in TIERS}
        for candidate in candidates:
            usage[candidate["tier"]] += candidate["size_bytes"]
        over = self._over_capacity(usage, config)

        if forced_tasks is not None:
            missing = [name for name in forced_tasks if name not in by_name]
            if missing:
                name = missing[0]
                persisted = conn.execute(
                    "SELECT tier, evicted FROM artifacts WHERE task_name=?", (name,)
                ).fetchone()
                raise BusinessError(
                    E_EVICT_CANDIDATE_MISSING,
                    "eviction candidate does not reference an active artifact",
                    run_seq=run_seq,
                    task_name=name,
                    tier=None if persisted is None else persisted["tier"],
                    evicted=None if persisted is None else bool(persisted["evicted"]),
                )
            ordered = sorted(
                (by_name[name] for name in forced_tasks),
                key=lambda item: (
                    TIER_RANK[item["tier"]],
                    -item["score"],
                    item["hit_count"],
                    item["task_name"],
                ),
            )
        else:
            ordered = sorted(
                (candidate for candidate in candidates if not candidate["pinned"]),
                key=lambda item: (
                    TIER_RANK[item["tier"]],
                    -item["score"],
                    item["hit_count"],
                    item["task_name"],
                ),
            )

        if not over and forced_tasks is None:
            return {
                "evicted": [],
                "abandoned": [],
                "precision_events": [],
                "unresolved": [],
            }

        for candidate in candidates:
            if candidate["pinned"]:
                continue
            cumulative = int(candidate["error_base"])
            for operation in candidate["operations"]:
                self._record_arithmetic(
                    conn,
                    pipeline,
                    run_seq,
                    candidate["task_name"],
                    operation=operation.operation,
                    scale=operation.scale,
                    rounding=operation.rounding,
                    raw_value=operation.raw_value,
                    converted_value=operation.converted_value,
                    error_num=operation.error_num,
                    cumulative_error_num=cumulative + operation.cumulative_error_num,
                    details={
                        "field": "eviction_score",
                        "tier": candidate["tier"],
                        "idle_ms": candidate["idle_ms"],
                    },
                )
            cumulative += int(candidate["error_bound"]["numerator"])
            conn.execute(
                "UPDATE artifacts SET error_bound_num=? WHERE task_name=?",
                (cumulative, candidate["task_name"]),
            )

        selected: list[dict[str, Any]] = []
        selected_names: set[str] = set()
        selected_bytes = 0
        stop_reason: str | None = None
        remaining: list[dict[str, Any]] = []
        for index, candidate in enumerate(ordered):
            if not over and forced_tasks is None:
                break
            if candidate["pinned"]:
                continue
            if len(selected) >= config.eviction_batch_size:
                stop_reason = "batch_limit_reached"
                remaining = ordered[index:]
                break
            if selected_bytes + candidate["size_bytes"] > config.reclaim_byte_budget:
                stop_reason = "reclaim_budget_exhausted"
                remaining = ordered[index:]
                break
            if candidate["task_name"] in selected_names:
                continue

            conn.execute(
                "UPDATE artifacts SET evicted=1, evicted_at_ms=?, evicted_run_seq=?, "
                "version=version+1 WHERE task_name=? AND evicted=0",
                (at_ms, run_seq, candidate["task_name"]),
            )
            active_refs = conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE evicted=0 AND digest=?",
                (candidate["digest"],),
            ).fetchone()[0]
            queue_created = False
            if active_refs == 0:
                file_existed = self.storage.objects.path_for(candidate["digest"]).is_file()
                conn.execute(
                    "INSERT INTO reclaim_queue("
                    "digest, size_bytes, status, reclaimed_bytes, pipeline, run_seq, "
                    "at_ms, created_order, details_json"
                    ") VALUES(?, ?, 'pending', 0, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(digest) DO UPDATE SET "
                    "pipeline=excluded.pipeline, run_seq=excluded.run_seq, "
                    "at_ms=excluded.at_ms, details_json=excluded.details_json",
                    (
                        candidate["digest"],
                        candidate["size_bytes"],
                        pipeline,
                        run_seq,
                        at_ms,
                        self.storage.next_counter(conn, "reclaim_order"),
                        canonical_json(
                            {
                                "file_existed_at_queue": file_existed,
                                "task_name": candidate["task_name"],
                            }
                        ),
                    ),
                )
                reclaim_status = "pending_reclaim"
                queue_created = True
            else:
                reclaim_status = "retained_refs"

            conn.execute(
                "INSERT INTO evictions("
                "pipeline, run_seq, at_ms, task_name, artifact_name, tier, size_bytes, "
                "digest, score_num, error_num, decision, reason, reclaim_status, "
                "reclaimed_bytes, details_json"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'evict', ?, ?, 0, ?)",
                (
                    pipeline,
                    run_seq,
                    at_ms,
                    candidate["task_name"],
                    candidate["artifact_name"],
                    candidate["tier"],
                    candidate["size_bytes"],
                    candidate["digest"],
                    candidate["score"],
                    candidate["error_bound"]["numerator"],
                    "forced_candidate" if forced_tasks is not None else "capacity_overflow",
                    reclaim_status,
                    canonical_json(
                        {
                            "idle_ms": candidate["idle_ms"],
                            "hit_count": candidate["hit_count"],
                            "pinned": False,
                            "active_refs_after_eviction": active_refs,
                            "queue_created": queue_created,
                        }
                    ),
                ),
            )
            selected.append(candidate)
            selected_names.add(candidate["task_name"])
            selected_bytes += candidate["size_bytes"]
            usage[candidate["tier"]] -= candidate["size_bytes"]
            over = self._over_capacity(usage, config)

        precision_events = self._record_precision_events(
            conn,
            config,
            pipeline,
            run_seq,
            at_ms,
            ordered,
            selected_names,
        )

        abandoned: list[dict[str, Any]] = []
        if stop_reason is not None:
            for candidate in remaining:
                if candidate["task_name"] in selected_names:
                    continue
                reason = (
                    "pinned_fixed_retention"
                    if candidate["pinned"]
                    else stop_reason
                )
                abandoned.append(
                    self._record_abandonment(
                        conn,
                        pipeline,
                        run_seq,
                        at_ms,
                        candidate,
                        reason=reason,
                        code="E_RECLAIM_PARTIAL" if reason != "pinned_fixed_retention" else "E_PINNED",
                    )
                )
        if over:
            for candidate in candidates:
                if candidate["task_name"] in selected_names:
                    continue
                if candidate["pinned"]:
                    already = any(
                        item.get("task_name") == candidate["task_name"] for item in abandoned
                    )
                    if not already:
                        abandoned.append(
                            self._record_abandonment(
                                conn,
                                pipeline,
                                run_seq,
                                at_ms,
                                candidate,
                                reason="pinned_fixed_retention",
                                code="E_PINNED",
                            )
                        )

        unresolved = [
            {
                "tier": tier,
                "used_bytes": usage[tier],
                "capacity_bytes": config.tier_capacity_bytes[tier],
                "over_by": usage[tier] - config.tier_capacity_bytes[tier],
                "reason": "pinned_retention" if any(
                    candidate["pinned"] and candidate["tier"] == tier
                    for candidate in candidates
                ) else "batch_or_budget_limit",
            }
            for tier in TIERS
            if usage[tier] > config.tier_capacity_bytes[tier]
        ]
        return {
            "evicted": selected,
            "abandoned": abandoned,
            "precision_events": precision_events,
            "unresolved": unresolved,
        }

    def _candidate(
        self,
        row: sqlite3.Row,
        at_ms: int,
        max_hits: int,
        config: ConfigSpec,
    ) -> dict[str, Any]:
        idle_ms = max(0, at_ms - int(row["last_access_ms"]))
        components = score_components(
            idle_ms, int(row["hit_count"]), max_hits, config
        )
        return {
            "task_name": row["task_name"],
            "artifact_name": row["artifact_name"],
            "tier": row["tier"],
            "size_bytes": int(row["size_bytes"]),
            "digest": row["digest"],
            "hit_count": int(row["hit_count"]),
            "last_access_ms": int(row["last_access_ms"]),
            "idle_ms": idle_ms,
            "pinned": bool(row["pinned"]),
            "max_hit_count": max_hits,
            "score": components["score"],
            "score_fp": components["score_fp"],
            "error_bound": fp_value(
                int(row["error_bound_num"]), config.fixed_point_scale
            ),
            "error_base": int(row["error_bound_num"]),
            "operations": components["operations"],
        }

    def _record_precision_events(
        self,
        conn: sqlite3.Connection,
        config: ConfigSpec,
        pipeline: str,
        run_seq: int,
        at_ms: int,
        ordered: list[dict[str, Any]],
        selected_names: set[str],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        high_order = high_precision_order(ordered, config)
        high_position = {name: index for index, name in enumerate(high_order)}
        for left, right in zip(ordered, ordered[1:]):
            if left["tier"] != right["tier"]:
                continue
            gap = abs(int(left["score"]) - int(right["score"]))
            if gap >= config.comparison_tolerance or gap == 0:
                continue
            changes = (
                high_position[left["task_name"]] > high_position[right["task_name"]]
            )
            affects = (left["task_name"] in selected_names) != (
                right["task_name"] in selected_names
            )
            details = {
                "scale": config.fixed_point_scale,
                "higher_scale": config.fixed_point_scale * 1000,
                "higher_scale_order": high_order,
                "candidates": [
                    {
                        "task_name": left["task_name"],
                        "tier": left["tier"],
                        "score": left["score_fp"],
                        "error_bound": left["error_bound"],
                        "idle_ms": left["idle_ms"],
                        "hit_count": left["hit_count"],
                    },
                    {
                        "task_name": right["task_name"],
                        "tier": right["tier"],
                        "score": right["score_fp"],
                        "error_bound": right["error_bound"],
                        "idle_ms": right["idle_ms"],
                        "hit_count": right["hit_count"],
                    },
                ],
            }
            conn.execute(
                "INSERT INTO precision_events("
                "pipeline, run_seq, at_ms, tier, candidate_a, candidate_b, "
                "score_a_num, score_b_num, gap_num, tolerance, error_a_num, "
                "error_b_num, higher_scale_changes, affects_selection, details_json"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pipeline,
                    run_seq,
                    at_ms,
                    left["tier"],
                    left["task_name"],
                    right["task_name"],
                    left["score"],
                    right["score"],
                    gap,
                    config.comparison_tolerance,
                    left["error_bound"]["numerator"],
                    right["error_bound"]["numerator"],
                    int(changes),
                    int(affects),
                    canonical_json(details),
                ),
            )
            event = {
                "tier": left["tier"],
                "candidate_a": left["task_name"],
                "candidate_b": right["task_name"],
                "score_a": left["score_fp"],
                "score_b": right["score_fp"],
                "score_gap": fp_value(gap, config.fixed_point_scale),
                "tolerance": config.comparison_tolerance,
                "error_a": left["error_bound"],
                "error_b": right["error_bound"],
                "higher_scale_changes": changes,
                "affects_selection": affects,
                "precision_sensitive": affects,
            }
            events.append(event)
        return events

    def _record_abandonment(
        self,
        conn: sqlite3.Connection,
        pipeline: str,
        run_seq: int,
        at_ms: int,
        candidate: dict[str, Any],
        *,
        reason: str,
        code: str,
    ) -> dict[str, Any]:
        details = {
            "idle_ms": candidate["idle_ms"],
            "hit_count": candidate["hit_count"],
            "pinned": candidate["pinned"],
            "score": candidate["score_fp"],
            "error_bound": candidate["error_bound"],
        }
        conn.execute(
            "INSERT INTO abandonments("
            "pipeline, run_seq, at_ms, task_name, tier, size_bytes, reason, code, details_json"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pipeline,
                run_seq,
                at_ms,
                candidate["task_name"],
                candidate["tier"],
                candidate["size_bytes"],
                reason,
                code,
                canonical_json(details),
            ),
        )
        return {
            "task_name": candidate["task_name"],
            "tier": candidate["tier"],
            "size_bytes": candidate["size_bytes"],
            "reason": reason,
            "code": code,
            "score": candidate["score_fp"],
            "error_bound": candidate["error_bound"],
        }

    def evict(
        self,
        *,
        at_ms: int | None = None,
        pipeline: str | None = None,
        run_seq: int | None = None,
        candidates: list[str] | None = None,
    ) -> dict[str, Any]:
        storage = self.storage
        with storage.lock():
            storage.initialize()
            with storage.transaction() as conn:
                if not storage.state_loaded(conn):
                    raise BusinessError(E_STATE_NOT_LOADED, "state has not been loaded")
                config = storage.active_config(conn)
                if config is None:
                    raise BusinessError(E_STATE_NOT_LOADED, "active capacity config is missing")
                clock = storage.logical_clock(conn) if at_ms is None else at_ms
                if not isinstance(clock, int) or isinstance(clock, bool) or clock < 0:
                    raise BusinessError(E_BAD_CONFIG, "at_ms must be a non-negative integer")
                if clock < storage.logical_clock(conn) - config.clock_rollback_tolerance_ms:
                    raise BusinessError(
                        E_CLOCK_ROLLBACK,
                        "eviction clock is earlier than the persisted watermark",
                        run_seq=run_seq,
                        at_ms=clock,
                        watermark_ms=storage.logical_clock(conn),
                    )
                context_pipeline = pipeline or "__manual__"
                if pipeline is not None:
                    self._pipeline(conn, pipeline)
                if run_seq is None:
                    context_run_seq = storage.next_counter(
                        conn, "standalone_eviction_seq"
                    )
                else:
                    context_run_seq = run_seq
                before_usage = self._usage(conn)
                outcome = self._enforce_capacity(
                    conn,
                    config,
                    pipeline=context_pipeline,
                    run_seq=context_run_seq,
                    at_ms=clock,
                    forced_tasks=candidates,
                )
                self._refresh_accounts(
                    conn,
                    config,
                    pipeline=context_pipeline,
                    run_seq=context_run_seq,
                    at_ms=clock,
                    before=before_usage,
                    write_ledger=True,
                )
                storage.set_meta(
                    conn, "logical_clock_ms", max(storage.logical_clock(conn), clock)
                )
                result = self._eviction_output(
                    conn,
                    config,
                    context_pipeline,
                    context_run_seq,
                    clock,
                    outcome,
                )
            self._crash_if("after_evict_commit")
            reclaimed = self._reclaim_pending(
                pipeline=context_pipeline, run_seq=context_run_seq
            )
            if reclaimed:
                with storage.transaction() as conn:
                    config = storage.active_config(conn)
                    if config is None:
                        raise BusinessError(E_STATE_NOT_LOADED, "active config disappeared")
                    result = self._eviction_output(
                        conn,
                        config,
                        context_pipeline,
                        context_run_seq,
                        clock,
                        outcome,
                    )
            return result

    def _eviction_output(
        self,
        conn: sqlite3.Connection,
        config: ConfigSpec,
        pipeline: str,
        run_seq: int,
        at_ms: int,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        evictions = [
            self._eviction_row(row, config)
            for row in conn.execute(
                "SELECT * FROM evictions WHERE pipeline=? AND run_seq=? ORDER BY id",
                (pipeline, run_seq),
            )
        ]
        abandonments = [
            self._abandonment_row(row, config)
            for row in conn.execute(
                "SELECT * FROM abandonments WHERE pipeline=? AND run_seq=? ORDER BY id",
                (pipeline, run_seq),
            )
        ]
        precision = [
            self._precision_row(row, config)
            for row in conn.execute(
                "SELECT * FROM precision_events WHERE pipeline=? AND run_seq=? ORDER BY id",
                (pipeline, run_seq),
            )
        ]
        reclaimed_total = sum(int(item["reclaimed_bytes"]) for item in evictions)
        return {
            "ok": True,
            "command": "evict",
            "pipeline": pipeline,
            "run_seq": run_seq,
            "at_ms": at_ms,
            "evicted": evictions,
            "evicted_count": len(evictions),
            "abandoned": abandonments,
            "abandoned_count": len(abandonments),
            "precision_events": precision,
            "reclaimed_bytes": reclaimed_total,
            "unresolved": outcome["unresolved"],
            "capacity": self._capacity_output(conn),
        }

    def _reclaim_pending(self, *, pipeline: str, run_seq: int) -> list[dict[str, Any]]:
        pending = []
        read_conn = self.storage.connection()
        try:
            pending = read_conn.execute(
                "SELECT * FROM reclaim_queue WHERE status='pending' "
                "ORDER BY created_order, digest"
            ).fetchall()
        finally:
            read_conn.close()
        reclaimed: list[dict[str, Any]] = []
        for row in pending:
            active_refs = 0
            read_conn = self.storage.connection()
            try:
                active_refs = read_conn.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE evicted=0 AND digest=?",
                    (row["digest"],),
                ).fetchone()[0]
            finally:
                read_conn.close()
            if active_refs:
                with self.storage.transaction() as conn:
                    conn.execute(
                        "UPDATE reclaim_queue SET status='cancelled', details_json=? "
                        "WHERE digest=? AND status='pending'",
                        (
                            canonical_json(
                                {
                                    "reason": "active_refs",
                                    "active_refs": active_refs,
                                }
                            ),
                            row["digest"],
                        ),
                    )
                    conn.execute(
                        "UPDATE evictions SET reclaim_status='cancelled_refs' "
                        "WHERE digest=? AND reclaim_status='pending_reclaim'",
                        (row["digest"],),
                    )
                continue

            details = safe_json_load(row["details_json"], {})
            existed_at_queue = bool(details.get("file_existed_at_queue"))
            target = self.storage.objects.path_for(row["digest"])
            existed_before = target.is_file()
            result = self.storage.objects.reclaim(row["digest"])
            reclaimed_bytes = int(result["reclaimed_bytes"])
            if not existed_before and not target.exists() and existed_at_queue:
                # A previous process deleted the file after commit but died
                # before recording completion. The queued size is the durable
                # evidence of what that interrupted reclaim removed.
                reclaimed_bytes = int(row["size_bytes"])
                result["status"] = "reclaimed_after_interrupted_unlink"
            self._crash_if("after_object_delete")
            with self.storage.transaction() as conn:
                conn.execute(
                    "UPDATE reclaim_queue SET status=?, reclaimed_bytes=?, details_json=? "
                    "WHERE digest=? AND status='pending'",
                    (
                        "reclaimed",
                        reclaimed_bytes,
                        canonical_json({**details, "reclaim": result}),
                        row["digest"],
                    ),
                )
                conn.execute(
                    "UPDATE evictions SET reclaim_status='reclaimed', reclaimed_bytes=? "
                    "WHERE digest=? AND reclaim_status='pending_reclaim'",
                    (reclaimed_bytes, row["digest"]),
                )
            reclaimed.append(
                {
                    "digest": row["digest"],
                    "reclaimed_bytes": reclaimed_bytes,
                    "status": result["status"],
                }
            )
        return reclaimed

    # ------------------------------------------------------------------
    # Judge, resume, status, verify
    # ------------------------------------------------------------------
    def judge(
        self,
        pipeline: str,
        *,
        run_seq: int | None = None,
        at_ms: int | None = None,
    ) -> dict[str, Any]:
        storage = self.storage
        with storage.lock():
            storage.initialize()
            conn = storage.connection()
            try:
                if not storage.state_loaded(conn):
                    raise BusinessError(E_STATE_NOT_LOADED, "state has not been loaded")
                config = storage.active_config(conn)
                if config is None:
                    raise BusinessError(E_STATE_NOT_LOADED, "active capacity config is missing")
                pipe = self._pipeline(conn, pipeline)
                selected = self._select_run(
                    pipe, conn, run_seq, allow_all_committed=True
                )
                clock = selected["at_ms"] if at_ms is None else at_ms
                existing = conn.execute(
                    "SELECT * FROM runs WHERE pipeline=? AND run_seq=?",
                    (pipeline, selected["run_seq"]),
                ).fetchone()
                precision_marks = self._precision_marks(conn, config, clock)
                if existing is not None and existing["status"] in (
                    "committed",
                    "committed_with_errors",
                ):
                    stored = safe_json_load(existing["task_results_json"], [])
                    tasks = []
                    for item in sorted(stored, key=lambda value: value["task_name"]):
                        current = conn.execute(
                            "SELECT tier, evicted, hit_count, last_hit_ms, "
                            "error_bound_num, digest FROM artifacts WHERE task_name=?",
                            (item["task_name"],),
                        ).fetchone()
                        enriched = dict(item)
                        if current is not None:
                            enriched["tier_current"] = current["tier"]
                            enriched["evicted"] = bool(current["evicted"])
                            enriched["hit_count_current"] = current["hit_count"]
                            enriched["error_bound"] = fp_value(
                                int(current["error_bound_num"]),
                                config.fixed_point_scale,
                            )
                        enriched.setdefault("error_bound", None)
                        enriched["precision_sensitive"] = (
                            item["task_name"] in precision_marks
                        )
                        tasks.append(enriched)
                    errors = self._run_errors(conn, pipeline, selected["run_seq"])
                    committed = True
                else:
                    self._check_clock(conn, config, clock, selected["run_seq"])
                    tasks = self._preview_tasks(
                        conn,
                        config,
                        pipe,
                        clock,
                        precision_marks,
                        selected["run_seq"],
                    )
                    errors = [
                        item["error"]
                        for item in tasks
                        if isinstance(item.get("error"), dict)
                    ]
                    committed = False
                precision_events = [
                    self._precision_row(row, config)
                    for row in conn.execute(
                        "SELECT * FROM precision_events WHERE pipeline=? AND run_seq=? "
                        "ORDER BY id",
                        (pipeline, selected["run_seq"]),
                    )
                ]
                return {
                    "ok": True,
                    "command": "judge",
                    "pipeline": pipeline,
                    "run_seq": selected["run_seq"],
                    "at_ms": clock,
                    "committed": committed,
                    "tasks": tasks,
                    "errors": sorted(
                        errors,
                        key=lambda item: (
                            item.get("task_name") or "",
                            item.get("code") or "",
                        ),
                    ),
                    "precision_events": precision_events,
                }
            finally:
                conn.close()

    def _preview_tasks(
        self,
        conn: sqlite3.Connection,
        config: ConfigSpec,
        pipe: dict[str, Any],
        at_ms: int,
        precision_marks: set[str],
        run_seq: int,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for task_name in pipe["tasks"]:
            task = conn.execute("SELECT * FROM tasks WHERE name=?", (task_name,)).fetchone()
            if task is None:
                error = BusinessError(
                    E_TASK_MISSING,
                    "pipeline task is absent from the loaded task manifest",
                    run_seq=run_seq,
                    task_name=task_name,
                )
                payload = error.as_dict()["error"]
                payload["run_seq"] = run_seq
                payload["task_name"] = task_name
                results.append(
                    {
                        "task_name": task_name,
                        "hit": False,
                        "reason": "task_missing",
                        "tier": None,
                        "error_bound": None,
                        "error": payload,
                    }
                )
                continue
            artifact = conn.execute(
                "SELECT * FROM artifacts WHERE task_name=?", (task_name,)
            ).fetchone()
            if artifact is None:
                error = BusinessError(
                    E_ARTIFACT_MISSING,
                    "task has no persisted artifact record",
                    run_seq=run_seq,
                    task_name=task_name,
                )
                payload = error.as_dict()["error"]
                payload["run_seq"] = run_seq
                payload["task_name"] = task_name
                results.append(
                    {
                        "task_name": task_name,
                        "hit": False,
                        "reason": "artifact_missing",
                        "tier": None,
                        "error_bound": None,
                        "error": payload,
                    }
                )
                continue
            tier = artifact["tier"]
            base = {
                "task_name": task_name,
                "artifact_name": task["artifact_name"],
                "digest": artifact["digest"],
                "tier": tier,
                "tier_before": tier,
                "error_bound": fp_value(
                    int(artifact["error_bound_num"]), config.fixed_point_scale
                ),
                "precision_sensitive": task_name in precision_marks,
            }
            if artifact["evicted"]:
                results.append({**base, "hit": False, "reason": "evicted"})
                continue
            if at_ms < artifact["written_at_ms"]:
                results.append(
                    {**base, "hit": False, "reason": "not_yet_written"}
                )
                continue
            try:
                self.storage.objects.verify(artifact["digest"], artifact["size_bytes"])
            except BusinessError as error:
                error.run_seq = run_seq
                error.task_name = task_name
                error.tier = tier
                payload = error.as_dict()["error"]
                payload["run_seq"] = run_seq
                payload["task_name"] = task_name
                payload["tier"] = tier
                results.append(
                    {
                        **base,
                        "hit": False,
                        "reason": "object_error",
                        "error": payload,
                    }
                )
                continue
            new_hits = int(artifact["hit_count"]) + 1
            new_rank = TIER_RANK[tier]
            if new_hits >= config.promotion_min_hits and new_rank < 2:
                new_rank += 1
            results.append(
                {
                    **base,
                    "hit": True,
                    "reason": "hit",
                    "tier_after": TIERS[new_rank],
                    "hit_count": new_hits,
                    "last_hit_ms": at_ms,
                    "last_access_ms": at_ms,
                    "idle_ms": 0,
                }
            )
        return sorted(results, key=lambda item: item["task_name"])

    def _precision_marks(
        self, conn: sqlite3.Connection, config: ConfigSpec, at_ms: int
    ) -> set[str]:
        rows = conn.execute("SELECT * FROM artifacts WHERE evicted=0").fetchall()
        if len(rows) < 2:
            return set()
        max_hits = max(int(row["hit_count"]) for row in rows)
        candidates = sorted(
            (
                self._candidate(row, at_ms, max_hits, config)
                for row in rows
            ),
            key=lambda item: (
                TIER_RANK[item["tier"]],
                -item["score"],
                item["hit_count"],
                item["task_name"],
            ),
        )
        marks: set[str] = set()
        for left, right in zip(candidates, candidates[1:]):
            if left["tier"] != right["tier"]:
                continue
            gap = abs(left["score"] - right["score"])
            if 0 < gap < config.comparison_tolerance:
                marks.add(left["task_name"])
                marks.add(right["task_name"])
        return marks

    def resume(self) -> dict[str, Any]:
        storage = self.storage
        with storage.lock():
            storage.initialize()
            cleaned = storage.objects.clean_temp_files()
            recovered: list[dict[str, Any]] = []
            stale_runs = 0
            with storage.transaction() as conn:
                if not storage.state_loaded(conn):
                    raise BusinessError(E_STATE_NOT_LOADED, "state has not been loaded")
                config = storage.active_config(conn)
                if config is None:
                    raise BusinessError(E_STATE_NOT_LOADED, "active config is missing")
                stale = conn.execute(
                    "SELECT pipeline, run_seq FROM runs WHERE status='processing'"
                ).fetchall()
                stale_runs = len(stale)
                conn.execute("DELETE FROM runs WHERE status='processing'")
                active = conn.execute(
                    "SELECT * FROM artifacts WHERE evicted=0 ORDER BY task_name"
                ).fetchall()
                for artifact in active:
                    target = storage.objects.path_for(artifact["digest"])
                    if target.is_file():
                        try:
                            storage.objects.verify(
                                artifact["digest"], artifact["size_bytes"]
                            )
                        except BusinessError as error:
                            recovered.append(
                                {
                                    "task_name": artifact["task_name"],
                                    "digest": artifact["digest"],
                                    "recovered": False,
                                    "error": error.as_dict()["error"],
                                }
                            )
                        continue
                    try:
                        storage.objects.materialize_generated(
                            artifact["task_name"],
                            artifact["written_at_ms"],
                            artifact["size_bytes"],
                            artifact["digest"],
                        )
                        recovered.append(
                            {
                                "task_name": artifact["task_name"],
                                "digest": artifact["digest"],
                                "recovered": True,
                            }
                        )
                    except BusinessError as error:
                        recovered.append(
                            {
                                "task_name": artifact["task_name"],
                                "digest": artifact["digest"],
                                "recovered": False,
                                "error": error.as_dict()["error"],
                            }
                        )
                self._refresh_accounts(
                    conn,
                    config,
                    pipeline="__resume__",
                    run_seq=0,
                    at_ms=storage.logical_clock(conn),
                    before=None,
                    write_ledger=False,
                )
            reclaimed = self._reclaim_pending(
                pipeline="__resume__", run_seq=0
            )
            conn = storage.connection()
            try:
                pending = conn.execute(
                    "SELECT COUNT(*) FROM reclaim_queue WHERE status='pending'"
                ).fetchone()[0]
            finally:
                conn.close()
            return {
                "ok": True,
                "command": "resume",
                "temporary_files_removed": cleaned,
                "stale_run_intents_removed": stale_runs,
                "objects_recovered": recovered,
                "reclaimed": reclaimed,
                "pending_reclaims": pending,
            }

    def status(self) -> dict[str, Any]:
        storage = self.storage
        with storage.lock():
            storage.initialize()
            conn = storage.connection()
            try:
                loaded = storage.state_loaded(conn)
                config = storage.active_config(conn)
                artifacts = []
                runs = []
                errors = []
                transitions = []
                records: list[dict[str, Any]] = []
                if loaded:
                    for row in conn.execute(
                        "SELECT * FROM artifacts ORDER BY task_name"
                    ):
                        refs = [
                            {
                                "pipeline": ref["pipeline"],
                                "run_seq": ref["run_seq"],
                                "hit": bool(ref["hit"]),
                                "reason": ref["reason"],
                            }
                            for ref in conn.execute(
                                "SELECT * FROM artifact_refs WHERE task_name=? "
                                "ORDER BY pipeline, run_seq",
                                (row["task_name"],),
                            )
                        ]
                        artifacts.append(
                            {
                                "task_name": row["task_name"],
                                "artifact_name": row["artifact_name"],
                                "digest": row["digest"],
                                "size_bytes": row["size_bytes"],
                                "written_at_ms": row["written_at_ms"],
                                "tier": row["tier"],
                                "hit_count": row["hit_count"],
                                "last_hit_ms": row["last_hit_ms"],
                                "last_access_ms": row["last_access_ms"],
                                "pinned": bool(row["pinned"]),
                                "evicted": bool(row["evicted"]),
                                "evicted_at_ms": row["evicted_at_ms"],
                                "error_bound": fp_value(
                                    int(row["error_bound_num"]),
                                    int(row["error_scale"]),
                                ),
                                "referencing_runs": refs,
                            }
                        )
                    runs = [
                        {
                            "pipeline": row["pipeline"],
                            "run_seq": row["run_seq"],
                            "at_ms": row["at_ms"],
                            "status": row["status"],
                        }
                        for row in conn.execute(
                            "SELECT * FROM runs ORDER BY pipeline, run_seq"
                        )
                    ]
                    errors = [
                        {
                            "pipeline": row["pipeline"],
                            "run_seq": row["run_seq"],
                            "task_name": row["task_name"],
                            "tier": row["tier"],
                            "code": row["code"],
                            "message": row["message"],
                        }
                        for row in conn.execute(
                            "SELECT * FROM run_errors ORDER BY id DESC LIMIT 100"
                        )
                    ]
                    transitions = [
                        {
                            "pipeline": row["pipeline"],
                            "run_seq_start": row["run_seq_start"],
                            "run_seq_end": row["run_seq_end"],
                            "task_name": row["task_name"],
                            "trigger": row["trigger"],
                            "from_tier": row["from_tier"],
                            "to_tier": row["to_tier"],
                            "at_ms": row["at_ms"],
                            "details": safe_json_load(row["details_json"], {}),
                        }
                        for row in conn.execute(
                            "SELECT * FROM tier_transitions ORDER BY id"
                        )
                    ]
                    records = [
                        {
                            "digest": row["digest"],
                            "size_bytes": row["size_bytes"],
                            "status": row["status"],
                            "reclaimed_bytes": row["reclaimed_bytes"],
                            "pipeline": row["pipeline"],
                            "run_seq": row["run_seq"],
                            "at_ms": row["at_ms"],
                        }
                        for row in conn.execute(
                            "SELECT * FROM reclaim_queue ORDER BY created_order, digest"
                        )
                    ]
                return {
                    "ok": True,
                    "command": "status",
                    "state_loaded": loaded,
                    "state_dir": str(self.state_dir),
                    "config": None if config is None else config.as_dict(),
                    "logical_clock_ms": storage.logical_clock(conn),
                    "capacity": self._capacity_output(conn),
                    "artifact_count": len(artifacts),
                    "active_artifact_count": sum(
                        1 for artifact in artifacts if not artifact["evicted"]
                    ),
                    "artifacts": artifacts,
                    "runs": runs,
                    "recent_errors": errors,
                    "tier_transitions": transitions,
                    "reclaim_queue": records,
                }
            finally:
                conn.close()

    def verify(self, *, full_hash: bool = True) -> dict[str, Any]:
        storage = self.storage
        with storage.lock():
            storage.initialize()
            conn = storage.connection()
            checks: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            warnings: list[dict[str, Any]] = []
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                checks.append({"check": "sqlite_integrity", "ok": integrity == "ok"})
                if integrity != "ok":
                    errors.append({"code": E_STATE_CORRUPT, "message": integrity})
                if not storage.state_loaded(conn):
                    errors.append(
                        {"code": E_STATE_NOT_LOADED, "message": "state has not been loaded"}
                    )
                    return {
                        "ok": False,
                        "command": "verify",
                        "checks": checks,
                        "errors": errors,
                        "warnings": warnings,
                    }
                config = storage.active_config(conn)
                if config is None:
                    errors.append(
                        {"code": E_STATE_CORRUPT, "message": "active config missing"}
                    )
                    config = None

                artifacts = conn.execute(
                    "SELECT a.*, t.expected_digest FROM artifacts a "
                    "JOIN tasks t ON t.name=a.task_name ORDER BY a.task_name"
                ).fetchall()
                for missing in conn.execute(
                    "SELECT t.name FROM tasks t LEFT JOIN artifacts a "
                    "ON a.task_name=t.name WHERE a.task_name IS NULL ORDER BY t.name"
                ):
                    errors.append(
                        {
                            "code": E_STATE_CORRUPT,
                            "task_name": missing["name"],
                            "message": "task has no artifact record",
                        }
                    )
                for artifact in artifacts:
                    item_errors: list[str] = []
                    if artifact["size_bytes"] <= 0:
                        item_errors.append("non-positive artifact size")
                    if artifact["hit_count"] < 0:
                        item_errors.append("negative hit count")
                    if artifact["last_hit_ms"] < artifact["written_at_ms"]:
                        item_errors.append("last_hit_ms earlier than written_at_ms")
                    if (
                        artifact["expected_digest"] is not None
                        and artifact["expected_digest"] != artifact["digest"]
                    ):
                        item_errors.append("expected digest differs from persisted digest")
                    if item_errors:
                        errors.append(
                            {
                                "code": E_STATE_CORRUPT,
                                "task_name": artifact["task_name"],
                                "message": "; ".join(item_errors),
                            }
                        )
                    if artifact["evicted"]:
                        # Evicted metadata remains for diagnostics and history;
                        # the CAS object may legitimately be gone.
                        continue
                    target = storage.objects.path_for(artifact["digest"])
                    if not target.is_file():
                        errors.append(
                            {
                                "code": E_OBJECT_MISSING,
                                "task_name": artifact["task_name"],
                                "tier": artifact["tier"],
                                "digest": artifact["digest"],
                                "message": "object file is missing",
                            }
                        )
                        continue
                    try:
                        if full_hash:
                            storage.objects.verify(
                                artifact["digest"], artifact["size_bytes"]
                            )
                        else:
                            actual_size = target.stat().st_size
                            if actual_size != artifact["size_bytes"]:
                                raise BusinessError(
                                    E_STATE_CORRUPT,
                                    "object size differs from metadata",
                                    task_name=artifact["task_name"],
                                    tier=artifact["tier"],
                                    expected_size=artifact["size_bytes"],
                                    actual_size=actual_size,
                                )
                    except BusinessError as error:
                        payload = error.as_dict()["error"]
                        payload.update(
                            {
                                "task_name": artifact["task_name"],
                                "tier": artifact["tier"],
                            }
                        )
                        errors.append(payload)
                checks.append(
                    {
                        "check": "cas_objects",
                        "ok": not any(
                            item.get("code") in (E_OBJECT_MISSING, E_DIGEST_MISMATCH)
                            for item in errors
                        ),
                    }
                )

                recomputed = self._usage(conn)
                if config is not None:
                    for tier in TIERS:
                        row = conn.execute(
                            "SELECT used_bytes, capacity_bytes FROM tier_accounts WHERE tier=?",
                            (tier,),
                        ).fetchone()
                        if row is None:
                            errors.append(
                                {
                                    "code": E_STATE_CORRUPT,
                                    "tier": tier,
                                    "message": "tier account is missing",
                                }
                            )
                            continue
                        if row["used_bytes"] != recomputed[tier]:
                            errors.append(
                                {
                                    "code": E_STATE_CORRUPT,
                                    "tier": tier,
                                    "message": "tier account does not match artifact bytes",
                                    "accounted": row["used_bytes"],
                                    "recomputed": recomputed[tier],
                                }
                            )
                        if row["capacity_bytes"] != config.tier_capacity_bytes[tier]:
                            errors.append(
                                {
                                    "code": E_STATE_CORRUPT,
                                    "tier": tier,
                                    "message": "tier capacity does not match active config",
                                }
                            )
                checks.append(
                    {
                        "check": "tier_accounts",
                        "ok": not any(
                            item.get("message") == "tier account does not match artifact bytes"
                            for item in errors
                        ),
                    }
                )

                for row in conn.execute(
                    "SELECT q.*, (SELECT COUNT(*) FROM artifacts a "
                    "WHERE a.evicted=0 AND a.digest=q.digest) AS refs "
                    "FROM reclaim_queue q WHERE q.status='pending'"
                ):
                    if row["refs"]:
                        errors.append(
                            {
                                "code": E_STATE_CORRUPT,
                                "digest": row["digest"],
                                "message": "pending reclaim has active artifact references",
                            }
                        )
                checks.append(
                    {
                        "check": "reclaim_refcounts",
                        "ok": not any(
                            item.get("message")
                            == "pending reclaim has active artifact references"
                            for item in errors
                        ),
                    }
                )

                active_digests = {
                    row["digest"]
                    for row in conn.execute(
                        "SELECT DISTINCT digest FROM artifacts WHERE evicted=0"
                    )
                }
                pending_digests = {
                    row["digest"]
                    for row in conn.execute(
                        "SELECT digest FROM reclaim_queue WHERE status='pending'"
                    )
                }
                for path in storage.objects.iter_objects():
                    digest = path.name
                    if digest not in active_digests and digest not in pending_digests:
                        warnings.append(
                            {
                                "code": "W_ORPHAN_OBJECT",
                                "digest": digest,
                                "path": str(path),
                            }
                        )
                checks.append(
                    {
                        "check": "run_state",
                        "ok": all(
                            row["status"] in ("committed", "committed_with_errors")
                            for row in conn.execute("SELECT status FROM runs")
                        ),
                    }
                )
                return {
                    "ok": not errors,
                    "command": "verify",
                    "checks": checks,
                    "errors": errors,
                    "warnings": warnings,
                }
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Shared output and state helpers
    # ------------------------------------------------------------------
    def _run_output(
        self,
        conn: sqlite3.Connection,
        config: ConfigSpec,
        pipeline: str,
        run_seq: int,
        *,
        idempotent: bool,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM runs WHERE pipeline=? AND run_seq=?",
            (pipeline, run_seq),
        ).fetchone()
        if row is None:
            raise BusinessError(
                E_RUN_NOT_FOUND, "run is not committed", run_seq=run_seq, pipeline=pipeline
            )
        tasks = safe_json_load(row["task_results_json"], [])
        precision = [
            self._precision_row(event, config)
            for event in conn.execute(
                "SELECT * FROM precision_events WHERE pipeline=? AND run_seq=? ORDER BY id",
                (pipeline, run_seq),
            )
        ]
        precision_marks = {
            name
            for event in precision
            if event["precision_sensitive"]
            for name in (event["candidate_a"], event["candidate_b"])
        }
        for task in tasks:
            task.setdefault("error_bound", None)
            task["precision_sensitive"] = task["task_name"] in precision_marks
        evictions = [
            self._eviction_row(event, config)
            for event in conn.execute(
                "SELECT * FROM evictions WHERE pipeline=? AND run_seq=? ORDER BY id",
                (pipeline, run_seq),
            )
        ]
        abandoned = [
            self._abandonment_row(event, config)
            for event in conn.execute(
                "SELECT * FROM abandonments WHERE pipeline=? AND run_seq=? ORDER BY id",
                (pipeline, run_seq),
            )
        ]
        errors = self._run_errors(conn, pipeline, run_seq)
        transitions = [
            {
                "task_name": transition["task_name"],
                "trigger": transition["trigger"],
                "from_tier": transition["from_tier"],
                "to_tier": transition["to_tier"],
                "run_seq_start": transition["run_seq_start"],
                "run_seq_end": transition["run_seq_end"],
                "at_ms": transition["at_ms"],
                "details": safe_json_load(transition["details_json"], {}),
            }
            for transition in conn.execute(
                "SELECT * FROM tier_transitions WHERE pipeline=? AND run_seq_end=? "
                "ORDER BY id",
                (pipeline, run_seq),
            )
        ]
        return {
            "ok": True,
            "command": "run",
            "pipeline": pipeline,
            "run_seq": run_seq,
            "at_ms": row["at_ms"],
            "status": row["status"],
            "idempotent": idempotent,
            "tasks": tasks,
            "errors": errors,
            "transitions": transitions,
            "evictions": evictions,
            "evicted_count": len(evictions),
            "abandoned": abandoned,
            "reclaimed_bytes": sum(
                int(event["reclaimed_bytes"]) for event in evictions
            ),
            "precision_events": precision,
            "unresolved": self._unresolved_capacity(conn, config),
            "capacity": self._capacity_output(conn),
        }

    def _capacity_output(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM tier_accounts ORDER BY CASE tier "
            "WHEN 'cold' THEN 0 WHEN 'warm' THEN 1 ELSE 2 END"
        ).fetchall()
        return [
            {
                "tier": row["tier"],
                "capacity_bytes": row["capacity_bytes"],
                "used_bytes": row["used_bytes"],
                "remaining_bytes": row["capacity_bytes"] - row["used_bytes"],
                "error_bound": fp_value(
                    int(row["error_num"]), int(row["scale"])
                ),
            }
            for row in rows
        ]

    def _usage(self, conn: sqlite3.Connection) -> dict[str, int]:
        usage = {tier: 0 for tier in TIERS}
        for row in conn.execute(
            "SELECT tier, SUM(size_bytes) AS bytes FROM artifacts "
            "WHERE evicted=0 GROUP BY tier"
        ):
            usage[row["tier"]] = int(row["bytes"] or 0)
        return usage

    def _unresolved_capacity(
        self, conn: sqlite3.Connection, config: ConfigSpec
    ) -> list[dict[str, Any]]:
        usage = self._usage(conn)
        pinned_tiers = {
            row["tier"]
            for row in conn.execute(
                "SELECT DISTINCT tier FROM artifacts WHERE evicted=0 AND pinned=1"
            )
        }
        return [
            {
                "tier": tier,
                "used_bytes": usage[tier],
                "capacity_bytes": config.tier_capacity_bytes[tier],
                "over_by": usage[tier] - config.tier_capacity_bytes[tier],
                "reason": (
                    "pinned_retention"
                    if tier in pinned_tiers
                    else "batch_or_budget_limit"
                ),
            }
            for tier in TIERS
            if usage[tier] > config.tier_capacity_bytes[tier]
        ]

    def _refresh_accounts(
        self,
        conn: sqlite3.Connection,
        config: ConfigSpec,
        *,
        pipeline: str,
        run_seq: int,
        at_ms: int,
        before: dict[str, int] | None,
        write_ledger: bool,
    ) -> None:
        after = self._usage(conn)
        raw_artifacts = [
            {
                "task_name": row["task_name"],
                "size_bytes": row["size_bytes"],
                "tier": row["tier"],
            }
            for row in conn.execute(
                "SELECT task_name, size_bytes, tier FROM artifacts "
                "WHERE evicted=0 ORDER BY task_name"
            )
        ]
        for tier in TIERS:
            if before is not None:
                before_bytes = before[tier]
            else:
                account = conn.execute(
                    "SELECT used_bytes FROM tier_accounts WHERE tier=?", (tier,)
                ).fetchone()
                before_bytes = int(account["used_bytes"]) if account else after[tier]
            conn.execute(
                "INSERT INTO tier_accounts("
                "tier, capacity_bytes, used_bytes, error_num, scale, updated_run_seq"
                ") VALUES(?, ?, ?, 0, ?, ?) "
                "ON CONFLICT(tier) DO UPDATE SET "
                "capacity_bytes=excluded.capacity_bytes, "
                "used_bytes=excluded.used_bytes, "
                "error_num=excluded.error_num, scale=excluded.scale, "
                "updated_run_seq=excluded.updated_run_seq",
                (
                    tier,
                    config.tier_capacity_bytes[tier],
                    after[tier],
                    config.fixed_point_scale,
                    run_seq,
                ),
            )
            if write_ledger:
                conn.execute(
                    "INSERT INTO capacity_ledger("
                    "pipeline, run_seq, at_ms, tier, before_bytes, after_bytes, "
                    "capacity_bytes, scale, rounding, raw_json, error_num"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'exact', ?, 0)",
                    (
                        pipeline,
                        run_seq,
                        at_ms,
                        tier,
                        before_bytes,
                        after[tier],
                        config.tier_capacity_bytes[tier],
                        config.fixed_point_scale,
                        canonical_json(
                            {
                                "summands": [
                                    item
                                    for item in raw_artifacts
                                    if item["tier"] == tier
                                ],
                                "raw_used_bytes": after[tier],
                            }
                        ),
                    ),
                )
                self._record_arithmetic(
                    conn,
                    pipeline,
                    run_seq,
                    f"__tier__:{tier}",
                    operation="capacity_sum",
                    scale=config.fixed_point_scale,
                    rounding="exact",
                    raw_value=[
                        item["size_bytes"]
                        for item in raw_artifacts
                        if item["tier"] == tier
                    ],
                    converted_value=after[tier],
                    error_num=0,
                    cumulative_error_num=0,
                    details={
                        "field": "tier_used_bytes",
                        "tier": tier,
                        "before_bytes": before_bytes,
                        "after_bytes": after[tier],
                    },
                )

    def _record_ref(
        self,
        conn: sqlite3.Connection,
        pipeline: str,
        run_seq: int,
        task_name: str,
        at_ms: int,
        hit: bool,
        reason: str,
    ) -> None:
        conn.execute(
            "INSERT INTO artifact_refs("
            "pipeline, run_seq, task_name, at_ms, hit, reason"
            ") VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(pipeline, run_seq, task_name) DO NOTHING",
            (pipeline, run_seq, task_name, at_ms, int(hit), reason),
        )

    def _record_error(
        self,
        conn: sqlite3.Connection,
        pipeline: str,
        run_seq: int,
        error: BusinessError,
    ) -> None:
        payload = error.as_dict()["error"]
        conn.execute(
            "INSERT INTO run_errors("
            "pipeline, run_seq, task_name, tier, code, message, details_json"
            ") VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                pipeline,
                run_seq,
                error.task_name,
                error.tier,
                error.code,
                error.message,
                canonical_json(payload["fields"]),
            ),
        )

    def _record_transition(
        self,
        conn: sqlite3.Connection,
        *,
        pipeline: str,
        run_seq: int,
        task_name: str,
        trigger: str,
        from_tier: str,
        to_tier: str,
        at_ms: int,
        start: int,
        end: int,
        details: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO tier_transitions("
            "pipeline, run_seq_start, run_seq_end, task_name, trigger, "
            "from_tier, to_tier, at_ms, details_json"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pipeline,
                start,
                end,
                task_name,
                trigger,
                from_tier,
                to_tier,
                at_ms,
                canonical_json(details),
            ),
        )

    def _record_arithmetic(
        self,
        conn: sqlite3.Connection,
        pipeline: str,
        run_seq: int,
        task_name: str,
        *,
        operation: str,
        scale: int,
        rounding: str,
        raw_value: Any,
        converted_value: int,
        error_num: int,
        cumulative_error_num: int,
        details: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO arithmetic_ledger("
            "pipeline, run_seq, task_name, operation, scale, rounding, raw_json, "
            "converted_value, error_num, cumulative_error_num, details_json"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pipeline,
                run_seq,
                task_name,
                operation,
                scale,
                rounding,
                canonical_json(raw_value),
                converted_value,
                error_num,
                cumulative_error_num,
                canonical_json(details),
            ),
        )

    @staticmethod
    def _miss_result(
        task_name: str,
        artifact: sqlite3.Row,
        reason: str,
        tier: str,
        *,
        error: BusinessError | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "task_name": task_name,
            "artifact_name": artifact["artifact_name"],
            "digest": artifact["digest"],
            "hit": False,
            "reason": reason,
            "tier": tier,
            "tier_before": tier,
            "tier_after": tier,
            "hit_count": artifact["hit_count"],
            "last_hit_ms": artifact["last_hit_ms"],
            "last_access_ms": artifact["last_access_ms"],
            "idle_ms": None,
            "error_bound": None,
        }
        if error is not None:
            item["error"] = error.as_dict()["error"]
        return item

    def _run_errors(
        self, conn: sqlite3.Connection, pipeline: str, run_seq: int
    ) -> list[dict[str, Any]]:
        return [
            {
                "task_name": row["task_name"],
                "tier": row["tier"],
                "code": row["code"],
                "message": row["message"],
                "fields": safe_json_load(row["details_json"], {}),
                "run_seq": run_seq,
            }
            for row in conn.execute(
                "SELECT * FROM run_errors WHERE pipeline=? AND run_seq=? ORDER BY id",
                (pipeline, run_seq),
            )
        ]

    def _eviction_row(
        self, row: sqlite3.Row, config: ConfigSpec
    ) -> dict[str, Any]:
        return {
            "task_name": row["task_name"],
            "artifact_name": row["artifact_name"],
            "tier": row["tier"],
            "size_bytes": row["size_bytes"],
            "digest": row["digest"],
            "score": fp_value(int(row["score_num"]), config.fixed_point_scale),
            "error_bound": fp_value(
                int(row["error_num"]), config.fixed_point_scale
            ),
            "decision": row["decision"],
            "reason": row["reason"],
            "reclaim_status": row["reclaim_status"],
            "reclaimed_bytes": row["reclaimed_bytes"],
            "details": safe_json_load(row["details_json"], {}),
        }

    def _abandonment_row(
        self, row: sqlite3.Row, config: ConfigSpec
    ) -> dict[str, Any]:
        return {
            "task_name": row["task_name"],
            "tier": row["tier"],
            "size_bytes": row["size_bytes"],
            "reason": row["reason"],
            "code": row["code"],
            "details": safe_json_load(row["details_json"], {}),
        }

    def _precision_row(
        self, row: sqlite3.Row, config: ConfigSpec
    ) -> dict[str, Any]:
        return {
            "tier": row["tier"],
            "candidate_a": row["candidate_a"],
            "candidate_b": row["candidate_b"],
            "score_a": fp_value(
                int(row["score_a_num"]), config.fixed_point_scale
            ),
            "score_b": fp_value(
                int(row["score_b_num"]), config.fixed_point_scale
            ),
            "score_gap": fp_value(
                int(row["gap_num"]), config.fixed_point_scale
            ),
            "tolerance": row["tolerance"],
            "error_a": fp_value(
                int(row["error_a_num"]), config.fixed_point_scale
            ),
            "error_b": fp_value(
                int(row["error_b_num"]), config.fixed_point_scale
            ),
            "higher_scale_changes": bool(row["higher_scale_changes"]),
            "affects_selection": bool(row["affects_selection"]),
            "precision_sensitive": bool(row["affects_selection"]),
            "details": safe_json_load(row["details_json"], {}),
        }

    def _pipeline(self, conn: sqlite3.Connection, name: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM pipelines WHERE name=?", (name,)).fetchone()
        if row is None:
            raise BusinessError(
                E_BAD_PIPELINE, "pipeline does not exist", pipeline=name
            )
        return {
            "name": row["name"],
            "tasks": safe_json_load(row["tasks_json"], []),
            "runs": safe_json_load(row["runs_json"], []),
        }

    def _select_run(
        self,
        pipe: dict[str, Any],
        conn: sqlite3.Connection,
        run_seq: int | None,
        *,
        allow_all_committed: bool,
    ) -> dict[str, Any]:
        runs = [dict(run) for run in pipe["runs"]]
        if not runs:
            raise BusinessError(
                E_BAD_PIPELINE, "pipeline has no runs", pipeline=pipe["name"]
            )
        if run_seq is not None:
            for run in runs:
                if run["run_seq"] == run_seq:
                    return run
            raise BusinessError(
                E_RUN_NOT_FOUND,
                "run_seq is not present in the pipeline description",
                run_seq=run_seq,
                pipeline=pipe["name"],
            )
        committed = {
            row["run_seq"]
            for row in conn.execute(
                "SELECT run_seq FROM runs WHERE pipeline=? AND status IN "
                "('committed', 'committed_with_errors')",
                (pipe["name"],),
            )
        }
        for run in runs:
            if run["run_seq"] not in committed:
                return run
        if allow_all_committed:
            return runs[-1]
        raise BusinessError(
            E_ALL_RUNS_COMMITTED,
            "all described runs are already committed",
            pipeline=pipe["name"],
        )

    def _check_clock(
        self,
        conn: sqlite3.Connection,
        config: ConfigSpec,
        at_ms: int,
        run_seq: int,
    ) -> None:
        watermark = self.storage.logical_clock(conn)
        if at_ms < watermark - config.clock_rollback_tolerance_ms:
            raise BusinessError(
                E_CLOCK_ROLLBACK,
                "logical clock would move backwards beyond tolerance",
                run_seq=run_seq,
                at_ms=at_ms,
                watermark_ms=watermark,
                tolerance_ms=config.clock_rollback_tolerance_ms,
            )

    @staticmethod
    def _over_capacity(usage: dict[str, int], config: ConfigSpec) -> bool:
        return any(
            usage[tier] > config.tier_capacity_bytes[tier] for tier in TIERS
        )

    @staticmethod
    def _crash_if(point: str) -> None:
        if os.environ.get("CI_CACHE_TEST_CRASH_AT") == point:
            os.kill(os.getpid(), signal.SIGKILL)
