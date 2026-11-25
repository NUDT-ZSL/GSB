from __future__ import annotations

import copy
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from .cache import CacheStore
from .errors import E_LATENCY_BUDGET, EngineError, bad_input, param_error, resource_error
from .timing import (
    LogicalClock,
    PerfClock,
    QUERY_COST_MODEL,
    estimate_query_latency_ns,
)


def _timeout_error(completed_steps: list[str], timeout_ms: float, row_id: str) -> EngineError:
    return EngineError(
        E_LATENCY_BUDGET,
        "query exceeded the injected latency budget",
        {
            "row_id": row_id,
            "timeout_ms": timeout_ms,
            "completed_steps": list(completed_steps),
            "partial_result_available": False,
        },
        completed_steps=list(completed_steps),
    )


class QueryService:
    def __init__(self, cache: CacheStore) -> None:
        self.cache = cache

    def query_row(
        self,
        row_id: str,
        *,
        timeout_ms: float | None = None,
        clock: PerfClock | LogicalClock | None = None,
        selection_key: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(row_id, str) or not row_id:
            raise bad_input("row_id must be a non-empty string", field="row_id")
        if timeout_ms is not None:
            if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (int, float)):
                raise param_error("timeout_ms must be numeric", field="timeout_ms")
            if float(timeout_ms) < 0.0:
                raise param_error("timeout_ms must not be negative", field="timeout_ms")
        clock = PerfClock() if clock is None else clock
        budget_ns = None if timeout_ms is None else int(float(timeout_ms) * 1_000_000)
        start_ns = clock.now_ns()
        completed_steps: list[str] = []

        def check_budget() -> None:
            if budget_ns is not None and clock.now_ns() - start_ns > budget_ns:
                raise _timeout_error(completed_steps, float(timeout_ms), row_id)

        def finish_step(name: str, logical_units_ns: int) -> None:
            completed_steps.append(name)
            if isinstance(clock, LogicalClock):
                clock.advance(logical_units_ns)
            check_budget()

        check_budget()
        finish_step("validate_request", 5_000)
        selection = self.cache.load_selection(selection_key)
        current_key = selection["selection_key"]
        finish_step("load_current_selection", 10_000)
        records = selection.get("row_records", [])
        selected_record = None
        for record in records:
            if record.get("row_id") == row_id:
                selected_record = record
                break
        if selected_record is None:
            raise bad_input(
                "row_id is not present in the committed batch result",
                field="row_id",
                row_id=row_id,
                selection_key=current_key,
            )
        finish_step("locate_row_record", 100 + len(records) * 2)
        record = copy.deepcopy(selected_record)
        feature_ids = list(record.get("feature_ids", []))
        vector = list(record.get("encoded", []))
        scores = list(record.get("selection_scores", []))
        retained = list(record.get("retained", []))
        if not (len(feature_ids) == len(vector) == len(scores) == len(retained)):
            raise bad_input(
                "committed row record has inconsistent vector lengths",
                field="row_record",
                row_id=row_id,
            )
        finish_step("decode_encoding_vector", 200 + len(feature_ids) * 30)
        projected = {
            feature_id: value
            for feature_id, value, kept in zip(feature_ids, vector, retained)
            if kept
        }
        finish_step("project_retained_features", 100 + len(projected) * 5)

        row_count = len(records)
        feature_count = len(feature_ids)
        retained_count = sum(1 for item in retained if item)
        estimated_ns = estimate_query_latency_ns(row_count, feature_count, retained_count)
        actual_ns = clock.now_ns() - start_ns
        return {
            "row_id": row_id,
            "record": record,
            "retained_projection": projected,
            "selection_key": current_key,
            "latency": {
                "clock": clock.name,
                "reproducible_clock": clock.reproducible,
                "actual_ns": int(actual_ns),
                "estimated_ns": int(estimated_ns),
                "timeout_ms": timeout_ms,
                "completed_steps": completed_steps,
                "row_count": row_count,
                "feature_count": feature_count,
                "retained_feature_count": retained_count,
                "estimate_formula": {
                    **QUERY_COST_MODEL,
                    "expression": "base_ns + per_row_ns*row_count + per_feature_ns*feature_count + per_retained_feature_ns*retained_feature_count",
                },
            },
        }


def _query_worker(args: tuple[str, int, str, str]) -> dict[str, Any]:
    cache_path, max_entries, row_id, clock_mode = args
    cache = CacheStore(Path(cache_path), max_entries, initialize=False)
    clock = LogicalClock() if clock_mode == "logical" else PerfClock()
    return QueryService(cache).query_row(row_id, clock=clock)


def query_many(
    cache_path: Path,
    max_entries: int,
    query_ids: list[str],
    *,
    requested_workers: int,
    max_workers: int,
    on_limit: str,
    clock_mode: str = "logical",
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if requested_workers < 1:
        raise param_error("requested_workers must be positive", field="requested_workers")
    if max_workers < 1:
        raise param_error("max_workers must be positive", field="max_query_workers")
    degradation = None
    if requested_workers > max_workers:
        if on_limit != "degrade_serial":
            raise resource_error(
                "requested query workers exceed max_query_workers",
                resource="query_workers",
                limit=max_workers,
                requested=requested_workers,
            )
        effective_workers = 1
        degradation = {
            "category": "query_workers_serialized",
            "resource": "query_workers",
            "requested": requested_workers,
            "applied_limit": max_workers,
            "effective_mode": "serial",
            "result_unchanged": True,
        }
    else:
        effective_workers = requested_workers
    arguments = [
        (str(cache_path), int(max_entries), row_id, clock_mode)
        for row_id in query_ids
    ]
    if effective_workers == 1:
        responses = [_query_worker(argument) for argument in arguments]
    else:
        try:
            context = multiprocessing.get_context("fork")
        except ValueError:
            context = multiprocessing.get_context()
        with ProcessPoolExecutor(max_workers=effective_workers, mp_context=context) as executor:
            responses = list(executor.map(_query_worker, arguments))
    return responses, degradation
