"""Independent verification against serial, single-thread expectations."""

from __future__ import annotations

from typing import Any

from .aggregate import build_aggregate
from .canonical import canonical_json, digest
from .planner import candidate_paths, compute_profile
from .samples import window_bounds
from .sql import normalize_sql
from .snapshots import snapshot_for_clock, statistics_digest


def _check(checks: list[dict[str, Any]], name: str, ok: bool, **details: Any) -> None:
    checks.append({"name": name, "ok": bool(ok), "details": details})


def verify_state(store: Any) -> dict[str, Any]:
    with store.read_transaction():
        return _verify_state(store)


def _verify_state(store: Any) -> dict[str, Any]:
    store.require_initialized()
    checks: list[dict[str, Any]] = []
    samples = store.list_samples()
    aggregate_rows = store.list_aggregates()
    profile_rows = store.list_profiles()
    windows = store.list_windows()
    snapshots = store.list_snapshots()
    model = store.get_db_model()
    watermark = store.watermark()
    aggregates_by_key = {
        (aggregate["template_id"], aggregate["snapshot_version"]): aggregate
        for aggregate in aggregate_rows
    }

    snapshot_ok = bool(snapshots)
    snapshot_mismatches = []
    previous_version = None
    for snapshot in snapshots:
        if (
            snapshot["version"] < 1
            or snapshot["effective_at_ms"] < 0
            or statistics_digest(snapshot["tables"]) != snapshot["digest"]
            or (
                previous_version is not None
                and snapshot["version"] <= previous_version
            )
        ):
            snapshot_ok = False
            snapshot_mismatches.append(
                {
                    "version": snapshot["version"],
                    "effective_at_ms": snapshot["effective_at_ms"],
                    "digest": snapshot["digest"],
                }
            )
        previous_version = snapshot["version"]
    _check(
        checks,
        "statistics_snapshots_are_integrity_checked",
        snapshot_ok,
        snapshot_count=len(snapshots),
        mismatches=snapshot_mismatches,
    )

    sequences = [sample["seq"] for sample in samples]
    _check(
        checks,
        "sample_sequences_strictly_increasing_unique",
        all(
            sequences[index] < sequences[index + 1]
            for index in range(len(sequences) - 1)
        ),
        sample_count=len(sequences),
        last_seq=watermark["last_seq"],
    )
    _check(
        checks,
        "watermark_matches_samples",
        watermark["last_seq"]
        == (sequences[-1] if sequences else None),
        watermark_last_seq=watermark["last_seq"],
        sample_last_seq=sequences[-1] if sequences else None,
    )

    normalization_ok = True
    window_binding_ok = True
    snapshot_binding_ok = True
    for sample in samples:
        first = normalize_sql(sample["sql"])
        second = normalize_sql(sample["sql"])
        if (
            first["template_id"] != sample["template_id"]
            or first["canonical"] != second["canonical"]
        ):
            normalization_ok = False
        start, end, key = window_bounds(
            sample["logical_clock_ms"], watermark["window_ms"]
        )
        if key != sample["window_key"]:
            window_binding_ok = False
        known_snapshots = [
            snapshot
            for snapshot in snapshots
            if snapshot["version"] <= sample["known_snapshot_version"]
        ]
        expected_snapshot = snapshot_for_clock(
            known_snapshots, sample["logical_clock_ms"]
        )
        if (
            expected_snapshot is None
            or expected_snapshot["version"] != sample["snapshot_version"]
        ):
            snapshot_binding_ok = False
    _check(
        checks,
        "normalization_is_deterministic",
        normalization_ok,
        sample_count=len(samples),
    )
    _check(
        checks,
        "each_sample_has_one_deterministic_window",
        window_binding_ok,
        window_size_ms=watermark["window_ms"],
    )
    _check(
        checks,
        "snapshot_binding_uses_effective_logical_clock",
        snapshot_binding_ok,
        snapshot_versions=[snapshot["version"] for snapshot in snapshots],
    )

    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    structures: dict[tuple[str, int], dict[str, Any]] = {}
    for sample in samples:
        key = (sample["template_id"], sample["snapshot_version"])
        grouped.setdefault(key, []).append(sample)
        structures[key] = sample["structure"]

    aggregates_match = True
    aggregate_mismatches = []
    for key, grouped_samples in sorted(grouped.items()):
        expected = build_aggregate(
            template_id=key[0],
            snapshot_version=key[1],
            structure=structures[key],
            samples=grouped_samples,
        )
        actual = aggregates_by_key.get(key)
        if (
            actual is None
            or actual["aggregate_digest"] != expected["aggregate_digest"]
            or canonical_json(actual) != canonical_json(expected)
        ):
            aggregates_match = False
            aggregate_mismatches.append(
                {
                    "template_id": key[0],
                    "snapshot_version": key[1],
                    "expected_digest": expected["aggregate_digest"],
                    "actual_digest": None if actual is None else actual["aggregate_digest"],
                }
            )
    _check(
        checks,
        "serial_aggregates_match_samples",
        aggregates_match,
        mismatches=aggregate_mismatches,
    )

    expected_windows: dict[str, dict[str, Any]] = {}
    for sample in samples:
        key = sample["window_key"]
        entry = expected_windows.setdefault(
            key,
            {
                "sample_count": 0,
                "first_seq": sample["seq"],
                "last_seq": sample["seq"],
                "min_clock_ms": sample["logical_clock_ms"],
                "max_clock_ms": sample["logical_clock_ms"],
                "snapshot_versions": set(),
            },
        )
        entry["sample_count"] += 1
        entry["last_seq"] = max(entry["last_seq"], sample["seq"])
        entry["min_clock_ms"] = min(entry["min_clock_ms"], sample["logical_clock_ms"])
        entry["max_clock_ms"] = max(entry["max_clock_ms"], sample["logical_clock_ms"])
        entry["snapshot_versions"].add(sample["snapshot_version"])
    windows_match = len(windows) == len(expected_windows)
    window_mismatches = []
    for window in windows:
        expected = expected_windows.get(window["window_key"])
        if expected is None:
            windows_match = False
            window_mismatches.append(
                {"window_key": window["window_key"], "reason": "unexpected"}
            )
            continue
        if (
            window["sample_count"] != expected["sample_count"]
            or window["first_seq"] != expected["first_seq"]
            or window["last_seq"] != expected["last_seq"]
            or window["min_clock_ms"] != expected["min_clock_ms"]
            or window["max_clock_ms"] != expected["max_clock_ms"]
            or window["snapshot_versions"] != sorted(expected["snapshot_versions"])
        ):
            windows_match = False
            window_mismatches.append(
                {"window_key": window["window_key"], "reason": "mismatch"}
            )
    _check(
        checks,
        "window_summaries_match_samples",
        windows_match,
        mismatches=window_mismatches,
    )

    profiles_match = True
    profile_mismatches = []
    for profile in profile_rows:
        key = (profile["template_id"], profile["snapshot_version"])
        aggregate = aggregates_by_key.get(key)
        snapshot = store.snapshot_by_version(profile["snapshot_version"])
        if aggregate is None or snapshot is None:
            profiles_match = False
            profile_mismatches.append(
                {
                    "template_id": key[0],
                    "snapshot_version": key[1],
                    "reason": "missing_aggregate_or_snapshot",
                }
            )
            continue
        expected = compute_profile(
            aggregate=aggregate,
            snapshot=snapshot,
            model=model,
        )
        if canonical_json(profile["result"]) != canonical_json(expected):
            profiles_match = False
            profile_mismatches.append(
                {
                    "template_id": key[0],
                    "snapshot_version": key[1],
                    "expected_digest": expected["result_digest"],
                    "actual_digest": profile["result"].get("result_digest"),
                }
            )
    _check(
        checks,
        "parallel_profiles_match_serial_estimates",
        profiles_match,
        profile_count=len(profile_rows),
        mismatches=profile_mismatches,
    )

    tasks_valid = True
    task_mismatches = []
    latest = store.latest_run()
    if latest is not None:
        tasks = store.list_tasks(latest["run_id"])
        keys = [(task["template_id"], task["snapshot_version"]) for task in tasks]
        if len(keys) != len(set(keys)):
            tasks_valid = False
            task_mismatches.append({"reason": "duplicate_task_keys"})
        charged_sum = sum(task["memory_units"] for task in tasks if task["charged"])
        if charged_sum != latest["memory_charged"]:
            tasks_valid = False
            task_mismatches.append(
                {
                    "reason": "memory_ledger_mismatch",
                    "expected": charged_sum,
                    "actual": latest["memory_charged"],
                }
            )
        for task in tasks:
            if task["status"] == "running":
                tasks_valid = False
                task_mismatches.append(
                    {
                        "template_id": task["template_id"],
                        "snapshot_version": task["snapshot_version"],
                        "reason": "unfinished_running_task",
                    }
                )
            if task["status"] == "abandoned" and not task["abandon_reason"]:
                tasks_valid = False
                task_mismatches.append(
                    {
                        "template_id": task["template_id"],
                        "snapshot_version": task["snapshot_version"],
                        "reason": "missing_abandon_reason",
                    }
                )
            if task["status"] == "failed" and not task["error"]:
                tasks_valid = False
                task_mismatches.append(
                    {
                        "template_id": task["template_id"],
                        "snapshot_version": task["snapshot_version"],
                        "reason": "missing_failure_reason",
                    }
                )
            if task["status"] == "committed":
                profile = store.get_profile(
                    task["template_id"], task["snapshot_version"]
                )
                if profile is None:
                    tasks_valid = False
                    task_mismatches.append(
                        {
                            "template_id": task["template_id"],
                            "snapshot_version": task["snapshot_version"],
                            "reason": "committed_without_profile",
                        }
                    )
    _check(
        checks,
        "task_ledger_and_terminal_states_are_consistent",
        tasks_valid,
        mismatches=task_mismatches,
    )

    invalidation_ok = True
    invalidation_mismatches = []
    for event in store.list_invalidation_events():
        if event["closure_digest"] != digest(event["closure"]):
            invalidation_ok = False
            invalidation_mismatches.append(
                {"event_id": event["event_id"], "reason": "closure_digest_mismatch"}
            )
    _check(
        checks,
        "invalidation_closures_are_integrity_checked",
        invalidation_ok,
        event_count=len(store.list_invalidation_events()),
        mismatches=invalidation_mismatches,
    )
    ok = all(check["ok"] for check in checks)
    return {
        "ok": ok,
        "checks": checks,
        "summary": {
            "samples": len(samples),
            "windows": len(windows),
            "aggregates": len(aggregate_rows),
            "profiles": len(profile_rows),
            "latest_run_id": None if latest is None else latest["run_id"],
        },
    }
