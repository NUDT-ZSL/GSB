"""Execution-plan profiling and bounded parallel scheduling."""

from __future__ import annotations

import math
import os
import sqlite3
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any

from .canonical import digest, stable_round
from .errors import EngineError
from .locking import ProcessLock
from .sql import predicate_columns, query_tables


def _round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def _column_ndv(table_stats: dict[str, Any], column: str) -> int | None:
    value = table_stats.get("columns", {}).get(column)
    if isinstance(value, dict) and "ndv" in value:
        return int(value["ndv"])
    return None


def _table_predicates(
    structure: dict[str, Any], model: dict[str, Any]
) -> dict[str, dict[str, str]]:
    tables = query_tables(structure)
    model_tables = model["tables"]
    predicates = {table: {} for table in tables}
    for predicate in predicate_columns(structure):
        table = predicate["table"]
        column = predicate["column"]
        if table in predicates:
            existing = predicates[table].get(column)
            if existing != "equality":
                predicates[table][column] = predicate["kind"]
            continue
        candidates = [
            candidate
            for candidate in tables
            if column
            in {item["name"] for item in model_tables.get(candidate, {}).get("columns", [])}
        ]
        if len(candidates) == 1:
            table = candidates[0]
            existing = predicates[table].get(column)
            if existing != "equality":
                predicates[table][column] = predicate["kind"]
    return predicates


def _index_selectivity(
    *,
    table_stats: dict[str, Any],
    index_name: str,
    index_model: dict[str, Any],
    predicates: dict[str, str],
) -> tuple[list[str], float, str, list[str]]:
    index_columns = list(index_model.get("columns", []))
    used: list[str] = []
    for column in index_columns:
        kind = predicates.get(column)
        if kind is None:
            break
        used.append(column)
        if kind == "range":
            break
    if not used:
        return [], 1.0, "unusable", []

    index_stats = table_stats.get("indexes", {}).get(index_name, {})
    selectivities = index_stats.get("prefix_selectivity", {})
    dependency_prefix: list[str] = []
    selectivity: float | None = None
    source = "index_prefix_selectivity"
    expected_prefix = ",".join(used)
    if expected_prefix in selectivities:
        selectivity = float(selectivities[expected_prefix])
        dependency_prefix.append(expected_prefix)
    else:
        for length in range(len(used), 0, -1):
            candidate_prefix = ",".join(used[:length])
            if candidate_prefix in selectivities:
                selectivity = float(selectivities[candidate_prefix])
                dependency_prefix.append(candidate_prefix)
                source = "index_prefix_selectivity"
                break
    if selectivity is None and index_model.get("unique") and len(used) == len(index_columns):
        row_count = max(1, int(table_stats["row_count"]))
        selectivity = 1.0 / row_count
        source = "unique_index"
        dependency_prefix.append("__unique__")
    if selectivity is None and "selectivity" in index_stats:
        selectivity = float(index_stats["selectivity"])
        source = "index_selectivity"
        dependency_prefix.append("__index__")
    if selectivity is None:
        selectivity = 1.0
        source = "column_ndv"
        for column in used:
            kind = predicates[column]
            ndv = _column_ndv(table_stats, column)
            if kind == "equality":
                column_selectivity = 1.0 / max(1, ndv) if ndv else 1.0 / 3.0
            else:
                column_selectivity = min(1.0, 3.0 / ndv) if ndv else 1.0 / 3.0
            selectivity *= column_selectivity
            dependency_prefix.append(column)
    selectivity = max(0.0, min(1.0, selectivity))
    return used, stable_round(selectivity), source, dependency_prefix


def candidate_paths(
    structure: dict[str, Any],
    snapshot: dict[str, Any],
    model: dict[str, Any],
) -> list[dict[str, Any]]:
    """Enumerate full scans and index paths in a stable order."""

    tables = query_tables(structure)
    if not tables:
        raise EngineError(
            "ERR_PROFILE_ESTIMATE", "template does not reference a table"
        )
    predicates = _table_predicates(structure, model)
    paths: list[dict[str, Any]] = []
    for table in tables:
        table_stats = snapshot.get("tables", {}).get(table)
        if table_stats is None:
            raise EngineError(
                "ERR_PROFILE_ESTIMATE",
                "statistics snapshot does not cover a referenced table",
                {
                    "table": table,
                    "snapshot_version": snapshot["version"],
                },
            )
        row_count = int(table_stats["row_count"])
        paths.append(
            {
                "path_id": f"full_scan:{table}",
                "kind": "full_scan",
                "table": table,
                "index": None,
                "index_columns_used": [],
                "estimated_rows_float": float(row_count),
                "estimated_rows": row_count,
                "estimated_selectivity": 1.0,
                "estimate_source": "table_row_count",
                "stats_dependencies": [f"table:{table}:row_count"],
            }
        )
        model_indexes = model["tables"].get(table, {}).get("indexes", {})
        for index_name in sorted(model_indexes):
            index_model = model_indexes[index_name]
            used, selectivity, source, dependency_prefix = _index_selectivity(
                table_stats=table_stats,
                index_name=index_name,
                index_model=index_model,
                predicates=predicates.get(table, {}),
            )
            if source == "unusable":
                continue
            estimated_float = selectivity * row_count
            estimated_rows = _round_half_up(estimated_float)
            if estimated_float > 0:
                estimated_rows = max(1, estimated_rows)
            dependencies = [f"table:{table}:row_count"]
            dependencies.extend(
                f"table:{table}:index:{index_name}:prefix:{prefix}"
                for prefix in dependency_prefix
            )
            for column in used:
                if _column_ndv(table_stats, column) is not None:
                    dependencies.append(f"table:{table}:column:{column}:ndv")
            paths.append(
                {
                    "path_id": f"index:{table}:{index_name}",
                    "kind": "index",
                    "table": table,
                    "index": index_name,
                    "index_columns_used": used,
                    "estimated_rows_float": stable_round(estimated_float),
                    "estimated_rows": estimated_rows,
                    "estimated_selectivity": selectivity,
                    "estimate_source": source,
                    "stats_dependencies": sorted(set(dependencies)),
                }
            )
    return paths


def _deviation(estimated_rows: int, measured_rows: float) -> dict[str, Any]:
    if measured_rows > 0:
        ratio: float | None = stable_round(estimated_rows / measured_rows)
        difference = stable_round(estimated_rows - measured_rows)
        if abs(difference) < 1e-12:
            direction = "exact"
        elif estimated_rows > measured_rows:
            direction = "overestimate"
        else:
            direction = "underestimate"
    elif estimated_rows == 0:
        ratio = 1.0
        difference = 0.0
        direction = "exact"
    else:
        ratio = None
        difference = float(estimated_rows)
        direction = "overestimate"
    return {"ratio": ratio, "difference_rows": difference, "direction": direction}


def estimate_signature(
    *,
    structure: dict[str, Any],
    snapshot: dict[str, Any],
    model: dict[str, Any],
    measured_rows: float,
) -> dict[str, Any]:
    paths = candidate_paths(structure, snapshot, model)
    enriched_paths = []
    for path in paths:
        enriched_paths.append(
            {
                **path,
                "measured_rows": measured_rows,
                "deviation": _deviation(path["estimated_rows"], measured_rows),
            }
        )

    order = {path["path_id"]: index for index, path in enumerate(enriched_paths)}

    def closeness(path: dict[str, Any]) -> tuple[float, int]:
        ratio = path["deviation"]["ratio"]
        if ratio is None:
            distance = float("inf")
        else:
            distance = abs(math.log(max(ratio, 1e-300)))
        return distance, order[path["path_id"]]

    chosen = min(enriched_paths, key=closeness)
    first_deviation = next(
        (
            {
                "path_id": path["path_id"],
                "stats_item": path["stats_dependencies"][0],
                "direction": path["deviation"]["direction"],
            }
            for path in enriched_paths
            if path["deviation"]["direction"] != "exact"
        ),
        None,
    )
    return {
        "candidate_paths": enriched_paths,
        "chosen_path_id": chosen["path_id"],
        "deviation": {
            "ratio": chosen["deviation"]["ratio"],
            "difference_rows": chosen["deviation"]["difference_rows"],
            "direction": chosen["deviation"]["direction"],
            "first_source": (
                first_deviation["stats_item"] if first_deviation is not None else None
            ),
            "first_deviation": first_deviation,
        },
    }


def compute_profile(
    *,
    aggregate: dict[str, Any],
    snapshot: dict[str, Any],
    model: dict[str, Any],
) -> dict[str, Any]:
    measured_rows = float(aggregate["avg_scanned_rows"])
    measured_returned = float(aggregate["avg_returned_rows"])
    signature = estimate_signature(
        structure=aggregate["structure"],
        snapshot=snapshot,
        model=model,
        measured_rows=measured_rows,
    )
    result = {
        "schema_version": 1,
        "template_id": aggregate["template_id"],
        "template_structure": aggregate["structure"],
        "snapshot_version": snapshot["version"],
        "snapshot_effective_at_ms": snapshot["effective_at_ms"],
        "snapshot_digest": snapshot["digest"],
        "aggregate_digest": aggregate["aggregate_digest"],
        "sample_count": aggregate["sample_count"],
        "measured": {
            "avg_scanned_rows": measured_rows,
            "avg_returned_rows": measured_returned,
            "total_scanned_rows": aggregate["total_scanned_rows"],
            "total_returned_rows": aggregate["total_returned_rows"],
            "p50_duration_ms": aggregate["p50"],
            "p90_duration_ms": aggregate["p90"],
            "p95_duration_ms": aggregate["p95"],
            "p99_duration_ms": aggregate["p99"],
        },
        "candidate_paths": signature["candidate_paths"],
        "chosen_path_id": signature["chosen_path_id"],
        "deviation": signature["deviation"],
        "estimate_basis": "statistics_snapshot",
    }
    result["result_digest"] = digest(result)
    return result


def profile_signature(profile: dict[str, Any]) -> dict[str, Any]:
    """Fields that indicate whether a new snapshot changes the estimate."""

    return {
        "candidate_paths": profile.get("candidate_paths", []),
        "chosen_path_id": profile.get("chosen_path_id"),
        "deviation": profile.get("deviation"),
    }


def _candidate_count_for_aggregate(
    aggregate: dict[str, Any],
    snapshot: dict[str, Any],
    model: dict[str, Any],
) -> tuple[int, int]:
    try:
        count = len(candidate_paths(aggregate["structure"], snapshot, model))
    except EngineError:
        count = 1
    return count, max(1, count)


def prepare_plan_tasks(
    store: Any,
    *,
    logical_clock_ms: int,
    memory_unit: int,
) -> list[dict[str, Any]]:
    aggregates = store.list_aggregates(max_clock_ms=logical_clock_ms)
    model = store.get_db_model()
    tasks = []
    for ordinal, aggregate in enumerate(aggregates):
        snapshot = store.snapshot_by_version(aggregate["snapshot_version"])
        if snapshot is None:
            raise EngineError(
                "ERR_SNAPSHOT_NOT_FOUND",
                "aggregate references a missing statistics snapshot",
                {"snapshot_version": aggregate["snapshot_version"]},
            )
        candidate_count, path_units = _candidate_count_for_aggregate(
            aggregate, snapshot, model
        )
        units = path_units * memory_unit
        profile = store.get_profile(
            aggregate["template_id"], aggregate["snapshot_version"]
        )
        if profile is not None:
            if profile["aggregate_digest"] != aggregate["aggregate_digest"]:
                raise EngineError(
                    "ERR_PROFILE_IMMUTABLE",
                    "aggregate changed after its profile was committed",
                    {
                        "template_id": aggregate["template_id"],
                        "snapshot_version": aggregate["snapshot_version"],
                    },
                )
            status = "committed"
            profile_id = f"{aggregate['template_id']}:{aggregate['snapshot_version']}"
        else:
            status = "pending"
            profile_id = None
        tasks.append(
            {
                "template_id": aggregate["template_id"],
                "snapshot_version": aggregate["snapshot_version"],
                "ordinal": ordinal,
                "candidate_count": candidate_count,
                "memory_units": units,
                "status": status,
                "profile_id": profile_id,
                "aggregate_digest": aggregate["aggregate_digest"],
            }
        )
    return tasks


_failing = {"after_claim": 0, "before_profile_commit": 0, "in_profile": 0}


def _failpoint(name: str, index: int = 1) -> None:
    raw = os.environ.get("SLOWQUERY_FAILPOINT", "")
    if not raw:
        return
    parts = raw.split(":", 1)
    if parts[0] != name:
        return
    target = int(parts[1]) if len(parts) == 2 else 1
    _failing[name] += 1
    if _failing[name] >= target:
        os._exit(86)


def _compute_task(
    aggregate: dict[str, Any],
    snapshot: dict[str, Any],
    model: dict[str, Any],
) -> dict[str, Any]:
    result = compute_profile(
        aggregate=aggregate,
        snapshot=snapshot,
        model=model,
    )
    delay_ms = int(os.environ.get("SLOWQUERY_TASK_DELAY_MS", "0"))
    if delay_ms > 0:
        import time

        time.sleep(delay_ms / 1000.0)
    _failpoint("in_profile")
    return result


def _abandon_remaining(
    store: Any,
    run_id: int,
    ordered_tasks: list[dict[str, Any]],
    start_index: int,
    reason: dict[str, Any],
) -> int:
    keys = [
        (task["template_id"], task["snapshot_version"])
        for task in ordered_tasks[start_index:]
    ]
    return store.abandon_pending_tasks(run_id, keys, reason)


def execute_run(store: Any, run: dict[str, Any]) -> dict[str, Any]:
    run_id = run["run_id"]
    tasks = store.list_tasks(run_id)
    memory_unit = run["memory_unit"]
    model = store.get_db_model()
    pending_indexes = [
        index for index, task in enumerate(tasks) if task["status"] == "pending"
    ]
    active: dict[Future[dict[str, Any]], dict[str, Any]] = {}
    cursor = 0
    abandoned_this_call = 0
    provider = ThreadPoolExecutor(max_workers=run["max_workers"])
    try:
        while cursor < len(pending_indexes) or active:
            while (
                cursor < len(pending_indexes)
                and len(active) < run["max_workers"]
            ):
                task = tasks[pending_indexes[cursor]]
                claim = store.claim_task(
                    run_id, task["template_id"], task["snapshot_version"]
                )
                if claim.get("claimed"):
                    aggregate = store.get_aggregate(
                        task["template_id"], task["snapshot_version"]
                    )
                    snapshot = store.snapshot_by_version(task["snapshot_version"])
                    if aggregate is None or snapshot is None:
                        store.fail_task(
                            run_id,
                            task["template_id"],
                            task["snapshot_version"],
                            {
                                "code": "ERR_TASK_INPUT_MISSING",
                                "message": "aggregate or snapshot disappeared",
                            },
                        )
                        cursor += 1
                        continue
                    future = provider.submit(
                        _compute_task,
                        aggregate,
                        snapshot,
                        model,
                    )
                    active[future] = task
                    cursor += 1
                    _failpoint("after_claim")
                    continue
                if claim.get("status") == "budget_exhausted":
                    reason = {
                        "code": "ERR_MEMORY_BUDGET",
                        "message": "remaining memory budget cannot fit the next task",
                        "required_units": claim["required_units"],
                        "remaining_units": claim["remaining_units"],
                        "budget_units": claim["budget_units"],
                        "charged_units": claim["charged_units"],
                        "template_id": task["template_id"],
                        "snapshot_version": task["snapshot_version"],
                    }
                    abandoned_this_call += _abandon_remaining(
                        store, run_id, tasks, pending_indexes[cursor], reason
                    )
                    cursor = len(pending_indexes)
                    break
                cursor += 1
            if not active:
                break
            completed, _ = wait(active.keys(), return_when=FIRST_COMPLETED)
            for future in sorted(
                completed,
                key=lambda item: active[item]["ordinal"],
            ):
                task = active.pop(future)
                try:
                    result = future.result()
                except BaseException as exc:
                    if isinstance(exc, EngineError):
                        error = {"code": exc.code, "message": exc.message}
                    else:
                        error = {
                            "code": "ERR_TASK_FAILED",
                            "message": str(exc),
                            "exception_type": type(exc).__name__,
                        }
                    store.fail_task(
                        run_id,
                        task["template_id"],
                        task["snapshot_version"],
                        error,
                    )
                    continue
                _failpoint("before_profile_commit")
                try:
                    store.commit_profile(
                        run_id,
                        template_id=task["template_id"],
                        snapshot_version=task["snapshot_version"],
                        aggregate_digest=result["aggregate_digest"],
                        result=result,
                    )
                except EngineError as exc:
                    store.fail_task(
                        run_id,
                        task["template_id"],
                        task["snapshot_version"],
                        {"code": exc.code, "message": exc.message},
                    )
    finally:
        provider.shutdown(wait=True, cancel_futures=False)
    status = store.finalize_run(run_id)
    finished_run = store.get_run(run_id)
    return {
        "run_id": run_id,
        "status": status,
        "tasks": store.list_tasks(run_id),
        "memory": {
            "budget_units": finished_run["memory_budget"],
            "charged_units": finished_run["memory_charged"],
            "active_units": sum(
                task["memory_units"]
                for task in store.list_tasks(run_id)
                if task["status"] == "running"
            ),
        },
        "abandoned_this_call": abandoned_this_call,
    }


def create_or_resume_run(
    store: Any,
    *,
    resume: bool,
    max_workers: int,
    memory_budget: int,
    memory_unit: int,
    logical_clock_ms: int,
    retry_abandoned: bool,
    retry_failed: bool,
) -> dict[str, Any]:
    for name, value in (
        ("max_workers", max_workers),
        ("memory_budget", memory_budget),
        ("memory_unit", memory_unit),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EngineError(
                "ERR_BUDGET_CONFIG", f"{name} must be a positive integer"
            )
    latest = store.latest_run() if resume else None
    if latest is not None:
        reopen = (
            latest["status"] in {"running", "interrupted"}
            or retry_abandoned
            or retry_failed
        )
        if not reopen:
            has_new_aggregates = False
            if latest["status"] == "completed":
                has_new_aggregates = any(
                    task["status"] == "pending"
                    for task in prepare_plan_tasks(
                        store,
                        logical_clock_ms=logical_clock_ms,
                        memory_unit=memory_unit,
                    )
                )
            if not has_new_aggregates:
                return {
                    "run_id": latest["run_id"],
                    "status": latest["status"],
                    "tasks": store.list_tasks(latest["run_id"]),
                    "memory": {
                        "budget_units": latest["memory_budget"],
                        "charged_units": latest["memory_charged"],
                        "active_units": 0,
                    },
                    "resumed": False,
                    "message": "no unfinished work",
                }
            latest = None
    if latest is not None:
        run = store.reopen_run(
            latest["run_id"],
            retry_abandoned=retry_abandoned,
            retry_failed=retry_failed,
            memory_budget=(
                memory_budget
                if memory_budget != latest["memory_budget"]
                else None
            ),
            memory_unit=memory_unit if memory_unit != latest["memory_unit"] else None,
        )
        outcome = execute_run(store, run)
        outcome["resumed"] = True
        return outcome

    tasks = prepare_plan_tasks(
        store,
        logical_clock_ms=logical_clock_ms,
        memory_unit=memory_unit,
    )
    run_id = store.create_plan_run_with_tasks(
        max_workers=max_workers,
        memory_budget=memory_budget,
        memory_unit=memory_unit,
        logical_clock_ms=logical_clock_ms,
        tasks=tasks,
    )
    run = store.get_run(run_id)
    outcome = execute_run(store, run)
    outcome["resumed"] = False
    return outcome


def run_plan(
    store: Any,
    *,
    resume: bool,
    max_workers: int,
    memory_budget: int,
    memory_unit: int,
    logical_clock_ms: int,
    retry_abandoned: bool = False,
    retry_failed: bool = False,
) -> dict[str, Any]:
    lock_path = store.path.with_name(store.path.name + ".plan.lock")
    with ProcessLock(lock_path):
        return create_or_resume_run(
            store,
            resume=resume,
            max_workers=max_workers,
            memory_budget=memory_budget,
            memory_unit=memory_unit,
            logical_clock_ms=logical_clock_ms,
            retry_abandoned=retry_abandoned,
            retry_failed=retry_failed,
        )
