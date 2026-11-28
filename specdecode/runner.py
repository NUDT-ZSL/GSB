"""Policy-run orchestration, deterministic run keys and resume support."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .decoder import aggregate_results, decode_request
from .loader import LoadedDataset, Policy
from .storage import (
    commit_request,
    ensure_run,
    finalize_run,
    open_database,
    run_metadata,
    stored_request_results,
)


class SimulatedCrash(RuntimeError):
    """Test hook: stop after a committed prefix without writing the next request."""


def make_run_key(dataset: LoadedDataset, policy: Policy) -> str:
    payload = json.dumps(
        {
            "dataset_fingerprint": dataset.fingerprint,
            "policy": policy.as_dict(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def _execute(
    dataset: LoadedDataset,
    policy: Policy,
    *,
    crash_after_new_commits: int | None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for request in dataset.requests:
        result = decode_request(dataset, request, policy)
        if (
            crash_after_new_commits is not None
            and len(results) >= crash_after_new_commits
        ):
            raise SimulatedCrash(
                f"simulated interruption after {len(results)} new commits"
            )
        results.append(result)
    return results


def run_policy(
    dataset: LoadedDataset,
    policy: Policy,
    *,
    database_path: str | None = None,
    source_schema: int | None = None,
    crash_after_new_commits: int | None = None,
) -> dict[str, Any]:
    run_key = make_run_key(dataset, policy)
    if database_path is None:
        existing_count = 0
        results = _execute(
            dataset,
            policy,
            crash_after_new_commits=crash_after_new_commits,
        )
        persistence = {
            "database": None,
            "run_key": run_key,
            "resumed_from_committed_count": 0,
            "newly_committed_count": len(results),
            "status": "complete",
        }
    else:
        connection = open_database(database_path)
        try:
            metadata = ensure_run(
                connection,
                run_key=run_key,
                dataset=dataset,
                policy=policy,
                source_schema=source_schema,
            )
            existing_count = int(metadata["completed_requests"])
            if existing_count > len(dataset.requests):
                raise ValueError("stored committed prefix exceeds request stream")

            results = stored_request_results(connection, run_key)
            if len(results) != existing_count:
                raise ValueError(
                    "stored committed prefix is not contiguous with runs metadata"
                )
            for index, result in enumerate(results):
                expected_id = dataset.requests[index].request_id
                if result["request_id"] != expected_id:
                    raise ValueError(
                        f"stored request sequence mismatch at index {index}: "
                        f"{result['request_id']} != {expected_id}"
                    )

            new_commits = 0
            for request_seq in range(existing_count, len(dataset.requests)):
                result = decode_request(
                    dataset, dataset.requests[request_seq], policy
                )
                if (
                    crash_after_new_commits is not None
                    and new_commits >= crash_after_new_commits
                ):
                    raise SimulatedCrash(
                        "simulated interruption before committing request "
                        f"{request_seq}"
                    )
                commit_request(
                    connection,
                    run_key=run_key,
                    request_seq=request_seq,
                    result=result,
                    total_requests=len(dataset.requests),
                )
                new_commits += 1
                results.append(result)

            aggregate = finalize_run(
                connection, run_key=run_key, results=results
            )
            metadata = run_metadata(connection, run_key)
            persistence = {
                "database": database_path,
                "run_key": run_key,
                "resumed_from_committed_count": existing_count,
                "newly_committed_count": new_commits,
                "status": metadata["status"],
            }
        finally:
            connection.close()

    aggregate = aggregate_results(results)
    return {
        "run_key": run_key,
        "dataset_fingerprint": dataset.fingerprint,
        "seed": dataset.seed,
        "tick_ms": dataset.tick_ms,
        "source_schema": source_schema,
        "policy": policy.as_dict(),
        "persistence": persistence,
        "requests": results,
        "aggregate": aggregate,
    }


def summarize_run(run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_key": run["run_key"],
        "dataset_fingerprint": run["dataset_fingerprint"],
        "seed": run["seed"],
        "tick_ms": run["tick_ms"],
        "policy": run["policy"],
        "persistence": run["persistence"],
        "aggregate": run["aggregate"],
        "requests": [
            {
                "request_id": result["request_id"],
                "request_class": result["request_class"],
                "end_reason": result["end_reason"],
                "final_token_count": result["final_token_count"],
                "stats": result["stats"],
            }
            for result in run["requests"]
        ],
    }


__all__ = [
    "SimulatedCrash",
    "make_run_key",
    "run_policy",
    "summarize_run",
]
