from __future__ import annotations

import hashlib
from typing import Any

from .batching import build_job_plan, cross_worker_duplicates, serial_truth
from .determinism import shard_manifest
from .errors import BusinessError, ErrorCode
from .locking import FileLock
from .models import CacheKey
from .storage import StateStore
from .util import canonical_json, sha256_text


def _prefix_digest(values: list[str]) -> str:
    return hashlib.sha256(
        canonical_json(values).encode("utf-8")
    ).hexdigest()


def verify_run(
    store: StateStore,
    *,
    run_id: str | None,
    allow_incomplete: bool = False,
) -> dict[str, Any]:
    run = store.get_run(run_id)
    actual_run_id = run["run_id"]
    run_lock_path = (
        store.lock_dir / "runs" / f"{sha256_text(actual_run_id)}.lock"
    )
    run_lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(run_lock_path, blocking=False):
        return _verify_run_locked(
            store,
            run_id=actual_run_id,
            allow_incomplete=allow_incomplete,
        )


def _verify_run_locked(
    store: StateStore,
    *,
    run_id: str,
    allow_incomplete: bool,
) -> dict[str, Any]:
    run = store.get_run(run_id)
    actual_run_id = run["run_id"]
    workload = store.get_workload(run["workload_digest"])
    job_map = workload.job_map()
    job = job_map.get(run["job_name"])
    if job is None:
        raise BusinessError(
            ErrorCode.STATE_CORRUPT,
            "run references a job absent from its workload",
            details={"run_id": actual_run_id, "job": run["job_name"]},
        )
    plan = build_job_plan(workload, job)
    snapshot = store.get_snapshot(actual_run_id)
    state = snapshot["state"]
    rounds = store.get_rounds(actual_run_id)
    run_shards = store.list_run_shards(actual_run_id)
    run_batches = store.list_run_batches(actual_run_id)
    ledger = store.memory_ledger(actual_run_id)

    checks: dict[str, bool] = {}
    failures: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: Any = None) -> None:
        checks[name] = passed
        if not passed:
            failures.append({"check": name, "detail": detail})

    duplicates = cross_worker_duplicates(plan)
    check("cross_worker_samples_disjoint", not duplicates, duplicates[:10])

    truth = serial_truth(workload, plan)
    committed_by_id = {row["batch_id"]: row for row in run_batches}
    missing = sorted(set(truth) - set(committed_by_id))
    extra = sorted(set(committed_by_id) - set(truth))
    check(
        "batch_id_set_matches_truth",
        not extra and (allow_incomplete or not missing),
        {"missing": missing, "extra": extra},
    )

    first_difference: dict[str, Any] | None = None
    compared = 0
    for batch_id in sorted(truth):
        expected = truth[batch_id]
        row = committed_by_id.get(batch_id)
        if row is None:
            if not allow_incomplete and first_difference is None:
                first_difference = {
                    "kind": "missing_batch",
                    "batch_id": batch_id,
                    "expected": expected.as_dict(),
                    "actual": None,
                }
            continue
        compared += 1
        result = store.get_batch_result(row["batch_key"])
        if result is None:
            result = store.get_batch_result_by_digest(expected.batch_digest)
        if result is None:
            if first_difference is None:
                first_difference = {
                    "kind": "missing_batch_result",
                    "batch_id": batch_id,
                    "expected": expected.as_dict(),
                    "actual": None,
                }
            continue
        actual = result["state"]
        if (
            row["batch_digest"] == expected.batch_digest
            and result["batch_digest"] == expected.batch_digest
            and row["batch_key"] == expected.batch_key
            and actual.get("batch_digest") == expected.batch_digest
            and actual.get("sample_digests") == list(expected.sample_digests)
            and actual.get("sample_refs") == list(expected.sample_refs)
            and actual.get("source_shards") == list(expected.source_shards)
        ):
            continue
        if first_difference is None:
            expected_digests = list(expected.sample_digests)
            actual_digests = list(actual.get("sample_digests", []))
            divergence_index: int | None = None
            for index in range(max(len(expected_digests), len(actual_digests))):
                left = (
                    expected_digests[index]
                    if index < len(expected_digests)
                    else None
                )
                right = (
                    actual_digests[index]
                    if index < len(actual_digests)
                    else None
                )
                if left != right:
                    divergence_index = index
                    break
            if divergence_index is None and (
                expected.sample_refs != tuple(actual.get("sample_refs", []))
            ):
                divergence_index = 0
            prefix = divergence_index or 0
            first_difference = {
                "kind": "batch_mismatch",
                "batch_id": batch_id,
                "sample_index_in_batch": divergence_index,
                "expected": expected.as_dict(),
                "actual": actual,
                "expected_path_intermediate": {
                    "batch_prefix_digest": _prefix_digest(
                        expected_digests[:prefix]
                    ),
                    "sample_digest": (
                        expected_digests[divergence_index]
                        if divergence_index is not None
                        and divergence_index < len(expected_digests)
                        else None
                    ),
                    "sample_ref": (
                        expected.sample_refs[divergence_index]
                        if divergence_index is not None
                        and divergence_index < len(expected.sample_refs)
                        else None
                    ),
                },
                "actual_path_intermediate": {
                    "batch_prefix_digest": _prefix_digest(
                        actual_digests[:prefix]
                    ),
                    "sample_digest": (
                        actual_digests[divergence_index]
                        if divergence_index is not None
                        and divergence_index < len(actual_digests)
                        else None
                    ),
                    "sample_ref": (
                        actual.get("sample_refs", [])[divergence_index]
                        if divergence_index is not None
                        and divergence_index < len(actual.get("sample_refs", []))
                        else None
                    ),
                },
            }
    check("batch_sample_summaries_match_serial_truth", first_difference is None, first_difference)

    batch_key_conflicts: list[dict[str, Any]] = []
    seen_keys: dict[str, str] = {}
    for row in run_batches:
        batch_id = row["batch_id"]
        previous = seen_keys.get(row["batch_key"])
        if previous is not None and previous != batch_id:
            batch_key_conflicts.append(
                {"batch_key": row["batch_key"], "batch_ids": [previous, batch_id]}
            )
        seen_keys[row["batch_key"]] = batch_id
    check("batch_keys_idempotent", not batch_key_conflicts, batch_key_conflicts)
    connection = store._connect()
    try:
        duplicate_digests = connection.execute(
            """
            SELECT batch_digest, COUNT(*) AS count
            FROM batch_results
            GROUP BY batch_digest
            HAVING COUNT(*) > 1
            """
        ).fetchall()
    finally:
        connection.close()
    check(
        "batch_digest_globally_unique",
        not duplicate_digests,
        [dict(row) for row in duplicate_digests],
    )

    shard_total = sum(int(row["bytes"]) for row in run_shards)
    ledger_total = sum(int(row["bytes"]) for row in ledger)
    check(
        "memory_snapshot_matches_committed_shards",
        shard_total == int(snapshot["memory_bytes"]) == ledger_total,
        {
            "run_shards": shard_total,
            "memory_ledger": ledger_total,
            "snapshot": int(snapshot["memory_bytes"]),
        },
    )
    check(
        "memory_peak_not_below_resident",
        int(snapshot["memory_peak_bytes"]) >= int(snapshot["memory_bytes"]),
        {
            "peak": int(snapshot["memory_peak_bytes"]),
            "resident": int(snapshot["memory_bytes"]),
        },
    )
    check(
        "memory_within_budget",
        int(snapshot["memory_bytes"])
        <= workload.config.global_memory_budget_bytes,
        {
            "used": int(snapshot["memory_bytes"]),
            "budget": workload.config.global_memory_budget_bytes,
        },
    )

    snapshot_versions = []
    connection = store._connect()
    try:
        snapshot_rows = connection.execute(
            "SELECT version FROM snapshots WHERE run_id=? ORDER BY version",
            (actual_run_id,),
        ).fetchall()
        snapshot_versions = [int(row["version"]) for row in snapshot_rows]
    finally:
        connection.close()
    check(
        "snapshot_versions_contiguous",
        snapshot_versions == list(range(1, len(snapshot_versions) + 1)),
        snapshot_versions,
    )
    check(
        "current_snapshot_is_latest",
        bool(snapshot_versions)
        and int(snapshot["version"]) == max(snapshot_versions),
        {
            "current": int(snapshot["version"]),
            "available": snapshot_versions,
        },
    )
    round_monotonic = all(
        int(left["logical_end_ms"]) <= int(right["logical_start_ms"])
        for left, right in zip(rounds, rounds[1:])
    )
    check("logical_clock_monotonic", round_monotonic, [
        {
            "round": int(row["round_index"]),
            "start": int(row["logical_start_ms"]),
            "end": int(row["logical_end_ms"]),
            "budget": int(row["state"].get("budget_ms", 0)),
        }
        for row in rounds
    ])
    round_budget_ok = all(
        int(row["logical_end_ms"]) - int(row["logical_start_ms"])
        <= int(row["state"].get("budget_ms", 0))
        for row in rounds
    )
    check("round_logical_time_budget_respected", round_budget_ok)

    cache_errors: list[dict[str, Any]] = []
    dataset_map = workload.dataset_map()
    for entry in store.list_cache_entries():
        dataset = dataset_map.get(entry["dataset"])
        if dataset is None:
            continue
        if int(entry["bytes"]) != dataset.shard_bytes:
            cache_errors.append(
                {
                    "dataset": entry["dataset"],
                    "shard_index": entry["shard_index"],
                    "reason": "byte_size_mismatch",
                }
            )
        if int(entry["shard_index"]) >= dataset.shard_count:
            cache_errors.append(
                {
                    "dataset": entry["dataset"],
                    "shard_index": entry["shard_index"],
                    "reason": "shard_index_out_of_range",
                }
            )
        manifest = shard_manifest(
            global_seed=workload.global_seed,
            dataset=dataset,
            dataset_fingerprint_value=entry["generation_fingerprint"],
            shard_index=int(entry["shard_index"]),
        )
        if manifest["digest"] != entry["digest"]:
            cache_errors.append(
                {
                    "dataset": entry["dataset"],
                    "shard_index": entry["shard_index"],
                    "reason": "digest_mismatch",
                    "expected_digest": manifest["digest"],
                    "stored_digest": entry["digest"],
                }
            )
    check("cache_entry_declarations", not cache_errors, cache_errors)

    content_errors: list[dict[str, Any]] = []
    for shard in run_shards:
        key = CacheKey(
            dataset=shard["dataset"],
            shard_index=int(shard["shard_index"]),
            cache_generation=int(shard["generation"]),
            generation_fingerprint=shard["generation_fingerprint"],
        )
        content = store.get_content(key)
        if content is None or content["state"] != "ready":
            continue
        if content["digest"] != shard["digest"]:
            content_errors.append(
                {
                    "dataset": shard["dataset"],
                    "shard_index": shard["shard_index"],
                    "reason": "content_digest_mismatch",
                }
            )
            continue
        try:
            loaded = store.blobs.read(key)
        except BusinessError as exc:
            content_errors.append(
                {
                    "dataset": shard["dataset"],
                    "shard_index": shard["shard_index"],
                    "reason": "blob_read_error",
                    "error": exc.as_dict()["error"],
                }
            )
            continue
        if loaded is None or loaded[1]["digest"] != shard["digest"]:
            content_errors.append(
                {
                    "dataset": shard["dataset"],
                    "shard_index": shard["shard_index"],
                    "reason": "blob_missing_or_mismatched",
                }
            )
    check("content_addressed_blobs", not content_errors, content_errors)

    state_committed = set(state.get("committed_batches", []))
    check(
        "snapshot_batch_list_matches_run_batches",
        state_committed == set(committed_by_id),
        {
            "snapshot_only": sorted(state_committed - set(committed_by_id)),
            "run_only": sorted(set(committed_by_id) - state_committed),
        },
    )
    state_abandoned = {
        (item["dataset"], int(item["shard_index"]))
        for item in state.get("abandoned", [])
    }
    state_failed = {
        (item["dataset"], int(item["shard_index"]))
        for item in state.get("failures", [])
    }
    run_shard_keys = {
        (item["dataset"], int(item["shard_index"])) for item in run_shards
    }
    overlapping = sorted((state_abandoned | state_failed) & run_shard_keys)
    check(
        "failed_or_abandoned_not_committed",
        not overlapping,
        overlapping,
    )

    ok = all(checks.values())
    result = {
        "ok": ok,
        "run_id": actual_run_id,
        "status": run["status"],
        "snapshot_version": int(snapshot["version"]),
        "truth": {
            "batch_count": len(truth),
            "sample_count": sum(len(batch.samples) for batch in plan.batches),
            "worker_count": job.data_parallelism,
            "shard_count": plan.dataset.shard_count,
        },
        "scheduled": {
            "batch_count": len(run_batches),
            "compared_batches": compared,
            "missing_batches": missing,
            "extra_batches": extra,
            "run_shard_count": len(run_shards),
            "memory_bytes": int(snapshot["memory_bytes"]),
            "memory_peak_bytes": int(snapshot["memory_peak_bytes"]),
            "round_count": len(rounds),
            "clock_watermark_ms": int(snapshot["logical_clock_ms"]),
        },
        "checks": checks,
        "failures": failures,
        "first_difference": first_difference,
        "cross_worker_duplicates": duplicates[:10],
    }
    return result
