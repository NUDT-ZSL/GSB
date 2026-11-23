from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .errors import ErrorCode, GraphStoreError, fail
from .models import TraversalRequest, UnifiedIndex
from .store import StateStore
from .traversal import (
    estimate_start_entries,
    execute_batch_traversals,
    execute_query,
)
from .util import canonical_json, digest_json


def make_run_id(index: UnifiedIndex, request: TraversalRequest) -> str:
    value = digest_json(
        {
            "index_version": index.index_version,
            "index_digest": index.digest,
            "request_digest": request.digest,
        }
    )
    return f"run-{value[:24]}"


def _batch_content_digest(
    index: UnifiedIndex,
    request: TraversalRequest,
    batch_index: int,
    starts: list[str],
) -> str:
    return digest_json(
        {
            "schema": "graphstore.batch-content/1",
            "index_version": index.index_version,
            "index_digest": index.digest,
            "request_digest": request.digest,
            "batch_index": batch_index,
            "starts": starts,
        }
    )


def plan_batches(
    index: UnifiedIndex,
    request: TraversalRequest,
) -> list[dict[str, Any]]:
    if request.mode == "reach":
        groups = [
            list(request.starts[offset : offset + request.batch_size])
            for offset in range(0, len(request.starts), request.batch_size)
        ]
        estimates = [
            sum(estimate_start_entries(index, start, request) for start in group)
            for group in groups
        ]
    else:
        groups = [[]]
        estimates = [1]

    plans: list[dict[str, Any]] = []
    for batch_index, (start_group, estimate) in enumerate(zip(groups, estimates)):
        content_digest = _batch_content_digest(
            index, request, batch_index, start_group
        )
        plans.append(
            {
                "batch_index": batch_index,
                "batch_key": f"b{batch_index:04d}-{content_digest[:12]}",
                "starts": start_group,
                "content_digest": content_digest,
                "estimate_entries": estimate,
            }
        )
    return plans


def _execute_plan(
    index: UnifiedIndex,
    request: TraversalRequest,
    plan: dict[str, Any],
) -> dict[str, Any]:
    delay_ms = int(os.environ.get("GRAPHSTORE_TEST_BATCH_DELAY_MS", "0"))
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)
    if request.mode == "reach":
        result = execute_batch_traversals(index, request, plan["starts"])
    else:
        result = execute_query(index, request)
    result = {
        "batch_key": plan["batch_key"],
        "batch_index": plan["batch_index"],
        "content_digest": plan["content_digest"],
        **result,
    }
    result["result_digest"] = digest_json(result)
    return result


def execute_run(
    store: StateStore,
    index: UnifiedIndex,
    request: TraversalRequest,
    run_id: str,
    *,
    max_waves: int = 100000,
) -> dict[str, Any]:
    plans = plan_batches(index, request)
    store.ensure_run(
        run_id=run_id,
        index_version=index.index_version,
        request=request,
        plans=plans,
    )
    expected_plans = {
        plan["batch_key"]: plan for plan in plans
    }
    store.validate_batch_plans(run_id, plans)

    for _wave in range(max_waves):
        run = store.get_run(run_id)
        if run["state"] in {"completed", "completed_with_failures"}:
            return store.status(run_id)
        batches = store.list_batches(run_id)
        terminal = {"succeeded", "failed", "abandoned"}
        if all(batch["state"] in terminal for batch in batches):
            store.finalize_run(run_id)
            return store.status(run_id)

        selected, abandoned_count = store.reserve_wave(
            run_id=run_id,
            request=request,
            expected_plans=expected_plans,
        )
        if not selected:
            if abandoned_count:
                continue
            remaining = [
                batch
                for batch in store.list_batches(run_id)
                if batch["state"] not in terminal
            ]
            if remaining:
                raise fail(
                    ErrorCode.BATCH_FAILED,
                    "scheduler made no progress",
                    pending=[batch["batch_key"] for batch in remaining],
                )
            store.finalize_run(run_id)
            return store.status(run_id)

        with ThreadPoolExecutor(
            max_workers=request.max_parallel_tasks,
            thread_name_prefix=f"graphstore-{run_id[-6:]}",
        ) as executor:
            futures = [
                (
                    batch,
                    executor.submit(_execute_plan, index, request, batch),
                )
                for batch in selected
            ]
            outcomes: list[tuple[dict[str, Any], dict[str, Any] | None, GraphStoreError | None]] = []
            for batch, future in futures:
                try:
                    outcomes.append((batch, future.result(), None))
                except GraphStoreError as exc:
                    outcomes.append((batch, None, exc))
                except BaseException as exc:
                    outcomes.append(
                        (
                            batch,
                            None,
                            GraphStoreError(
                                code=ErrorCode.BATCH_FAILED,
                                message="batch worker failed",
                                details={
                                    "batch_key": batch["batch_key"],
                                    "error_type": type(exc).__name__,
                                    "error": str(exc),
                                },
                            ),
                        )
                    )
        for batch, result, error in outcomes:
            if error is None and result is not None:
                store.commit_batch(
                    run_id=run_id,
                    batch_key=batch["batch_key"],
                    content_digest=batch["content_digest"],
                    state="succeeded",
                    result=result,
                    reason=None,
                )
            else:
                assert error is not None
                failure_result = {
                    "batch_key": batch["batch_key"],
                    "batch_index": batch["batch_index"],
                    "content_digest": batch["content_digest"],
                    "state": "failed",
                    "error": error.to_dict()["error"],
                }
                store.commit_batch(
                    run_id=run_id,
                    batch_key=batch["batch_key"],
                    content_digest=batch["content_digest"],
                    state="failed",
                    result=failure_result,
                    reason=error.code,
                )

    raise fail(
        ErrorCode.BATCH_FAILED,
        "scheduler exceeded maximum wave count",
        run_id=run_id,
        max_waves=max_waves,
    )


def describe_plan(plans: list[dict[str, Any]]) -> str:
    return canonical_json(
        [
            {
                "batch_key": plan["batch_key"],
                "batch_index": plan["batch_index"],
                "starts": plan["starts"],
                "estimate_entries": plan["estimate_entries"],
            }
            for plan in plans
        ]
    )

