"""Service layer connecting validation, decisioning and persistence."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .aggregation import (
    Sample,
    WindowAggregate,
    aggregate_windows,
    check_clock_rollback,
    latest_window_per_metric,
    parse_samples,
    validate_sample_references,
)
from .decision import DecisionOutput, decide
from .errors import (
    CLOCK_ROLLBACK,
    NOT_LOADED,
    ROUND_CONFLICT,
    SCHEMA_ERROR,
    UNKNOWN_WORKLOAD,
    VERSION_CONFLICT,
    VERSION_ROLLBACK,
    ControlError,
)
from .graph import PolicyPatch
from .models import PolicyBundle, load_policy_from_directory
from .store import Store
from .util import canonical_json, load_json_file, sha256_text


@dataclass
class SampleBundle:
    samples: list[Sample]
    events: list[dict[str, Any]] = field(default_factory=list)
    rounds: list[dict[str, Any]] = field(default_factory=list)


def _validate_events(
    events: Any,
    *,
    bundle: PolicyBundle,
) -> list[dict[str, Any]]:
    if events is None:
        return []
    if not isinstance(events, list):
        raise ControlError(SCHEMA_ERROR, "samples.events must be an array")
    result: list[dict[str, Any]] = []
    seen_outcomes: dict[tuple[str, str], bool] = {}
    for index, raw in enumerate(events):
        if not isinstance(raw, Mapping):
            raise ControlError(
                SCHEMA_ERROR, f"event {index} must be an object", {"index": index}
            )
        kind = str(raw.get("type", raw.get("kind", ""))).lower()
        if kind not in {"action_outcome", "freeze", "set_failures", "clock"}:
            raise ControlError(
                SCHEMA_ERROR,
                f"unsupported event type {kind!r}",
                {"index": index, "type": kind},
            )
        workload = str(raw.get("workload", "*"))
        if kind != "clock":
            if workload == "*":
                raise ControlError(
                    SCHEMA_ERROR,
                    f"event {index} requires a concrete workload",
                    {"index": index},
                )
            if workload not in bundle.workloads:
                raise ControlError(
                    UNKNOWN_WORKLOAD,
                    f"event {index} references unknown workload {workload!r}",
                    {"index": index, "workload": workload},
                )
        round_id = str(raw.get("round_id", "*"))
        at_ms = raw.get("at_ms", 0)
        if isinstance(at_ms, bool) or not isinstance(at_ms, int) or at_ms < 0:
            raise ControlError(
                SCHEMA_ERROR,
                f"event {index} at_ms must be a non-negative integer",
                {"index": index},
            )
        if kind == "freeze":
            until_ms = raw.get("until_ms", at_ms)
            if (
                isinstance(until_ms, bool)
                or not isinstance(until_ms, int)
                or until_ms < at_ms
            ):
                raise ControlError(
                    SCHEMA_ERROR,
                    f"event {index} freeze until_ms must be at or after at_ms",
                    {"index": index},
                )
        if kind == "action_outcome":
            success = bool(raw.get("success", True))
            key = (round_id, workload)
            if key in seen_outcomes and seen_outcomes[key] != success:
                raise ControlError(
                    SCHEMA_ERROR,
                    "conflicting action outcomes for the same round and workload",
                    {"round_id": round_id, "workload": workload},
                )
            seen_outcomes[key] = success
        result.append(dict(raw))
    return result


def _validate_rounds(rounds: Any, bundle: PolicyBundle) -> list[dict[str, Any]]:
    if rounds is None:
        return []
    if not isinstance(rounds, list):
        raise ControlError(SCHEMA_ERROR, "samples.rounds must be an array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rounds):
        if not isinstance(raw, Mapping):
            raise ControlError(
                SCHEMA_ERROR, f"round {index} must be an object", {"index": index}
            )
        round_id = raw.get("round_id", raw.get("id"))
        if not isinstance(round_id, str) or not round_id:
            raise ControlError(
                SCHEMA_ERROR, f"round {index} requires round_id", {"index": index}
            )
        if round_id in seen:
            raise ControlError(
                SCHEMA_ERROR,
                f"duplicate round_id {round_id!r}",
                {"round_id": round_id},
            )
        seen.add(round_id)
        at_ms = raw.get("at_ms", raw.get("clock_ms"))
        if isinstance(at_ms, bool) or not isinstance(at_ms, int) or at_ms < 0:
            raise ControlError(
                SCHEMA_ERROR,
                f"round {round_id!r} requires a non-negative at_ms integer",
                {"round_id": round_id},
            )
        workloads = raw.get("workloads")
        if workloads is not None:
            if not isinstance(workloads, list) or not all(
                isinstance(item, str) for item in workloads
            ):
                raise ControlError(
                    SCHEMA_ERROR,
                    f"round {round_id!r} workloads must be an array of names",
                    {"round_id": round_id},
                )
            unknown = sorted(set(workloads) - set(bundle.workloads))
            if unknown:
                raise ControlError(
                    UNKNOWN_WORKLOAD,
                    f"round {round_id!r} references unknown workloads",
                    {"round_id": round_id, "workloads": unknown},
                )
        result.append(dict(raw))
    return result


def load_sample_file(
    path: str | os.PathLike[str], bundle: PolicyBundle
) -> SampleBundle:
    payload = load_json_file(path)
    samples = parse_samples(payload)
    validate_sample_references(samples, bundle.metrics)
    if bundle.budget is None:
        raise ControlError(SCHEMA_ERROR, "missing budget")
    check_clock_rollback(
        samples,
        tolerance_ms=bundle.budget.clock_rollback_tolerance_ms,
    )
    events_raw = payload.get("events", []) if isinstance(payload, Mapping) else []
    rounds_raw = payload.get("rounds", []) if isinstance(payload, Mapping) else []
    events = _validate_events(events_raw, bundle=bundle)
    rounds = _validate_rounds(rounds_raw, bundle)
    return SampleBundle(samples=samples, events=events, rounds=rounds)


def load_workspace(
    *,
    data_dir: str | os.PathLike[str],
    out_dir: str | os.PathLike[str] = "out",
    samples_file: str | os.PathLike[str] | None = None,
    db_path: str | os.PathLike[str] | None = None,
    reset: bool = False,
) -> dict[str, Any]:
    bundle = load_policy_from_directory(data_dir)
    sample_path = (
        Path(samples_file) if samples_file is not None else Path(data_dir) / "samples.json"
    )
    sample_bundle = load_sample_file(sample_path, bundle)
    # Construction occurs only after every input has been validated.
    store = Store(
        Path(db_path) if db_path is not None else Path(out_dir) / "control.db"
    )
    return store.initialize(
        bundle,
        sample_bundle.samples,
        events=sample_bundle.events,
        reset=reset,
    )


def _window_payload_for_hash(window: WindowAggregate | None) -> Any:
    return None if window is None else window.to_dict()


def _input_hash(
    *,
    workload: str,
    at_ms: int,
    policy_version: int,
    bundle: PolicyBundle,
    latest: Mapping[str, WindowAggregate],
    outcomes: Mapping[str, Mapping[str, Any]],
) -> str:
    metrics = sorted(bundle.metrics_for_workload(workload))
    request = {
        "workload": workload,
        "at_ms": at_ms,
        "policy_version": policy_version,
        "windows": {
            metric: _window_payload_for_hash(latest.get(metric)) for metric in metrics
        },
        "outcome": dict(outcomes.get(workload, {})),
        "rules": [
            rule.to_dict() for rule in bundle.rules_for_workload(workload)
        ],
    }
    return sha256_text(canonical_json(request))


@dataclass
class PreparedWindows:
    latest: dict[str, WindowAggregate]
    updates: dict[str, WindowAggregate]
    last_clock_ms: int


def _prepare_windows(
    store: Store,
    bundle: PolicyBundle,
    *,
    at_ms: int,
) -> PreparedWindows:
    if bundle.budget is None:
        raise ControlError(SCHEMA_ERROR, "missing budget")
    with store.transaction(write=False) as connection:
        watermarks = store.watermarks(connection)
        clock_row = connection.execute(
            "SELECT value FROM schema_meta WHERE key='last_decision_clock'"
        ).fetchone()
        if not watermarks:
            raise ControlError(NOT_LOADED, "no metric watermarks are loaded")
        max_last_clock = max(
            [
                int(clock_row["value"]) if clock_row is not None else 0,
                *(
                    int(item["last_clock_ms"])
                    for item in watermarks.values()
                ),
            ],
            default=0,
        )
        if at_ms < max_last_clock - bundle.budget.clock_rollback_tolerance_ms:
            raise ControlError(
                CLOCK_ROLLBACK,
                "round logical clock moved backwards beyond tolerance",
                {
                    "at_ms": at_ms,
                    "last_clock_ms": max_last_clock,
                    "tolerance_ms": bundle.budget.clock_rollback_tolerance_ms,
                },
            )
        samples_by_metric = store.load_samples_after_watermark(
            connection, at_ms=at_ms
        )
    samples = [
        sample
        for metric in sorted(samples_by_metric)
        for sample in samples_by_metric[metric]
    ]
    samples.sort(key=lambda item: (item.sequence, item.sample_id))
    new_windows = aggregate_windows(
        bundle.metrics,
        samples,
        clock_ms=at_ms,
        # The original stream's arrival order was validated at load time.  A
        # policy patch may deliberately rewind a metric watermark and replay
        # historical samples; the injected round clock is checked separately
        # against ``last_decision_clock``/watermarks above.
        last_clock_ms=0,
        rollback_tolerance_ms=bundle.budget.clock_rollback_tolerance_ms,
    )
    latest: dict[str, WindowAggregate] = {}
    for metric, item in watermarks.items():
        cached = item.get("last_window")
        if cached is not None and int(cached.get("window_end_ms", 0)) <= at_ms:
            latest[metric] = WindowAggregate.from_dict(cached)
    for window in new_windows:
        current = latest.get(window.metric)
        if current is None or (
            window.window_end_ms,
            window.window_start_ms,
        ) > (
            current.window_end_ms,
            current.window_start_ms,
        ):
            latest[window.metric] = window
    updates: dict[str, WindowAggregate] = {}
    for window in new_windows:
        watermark = int(watermarks[window.metric]["watermark_ms"])
        if window.window_end_ms > watermark:
            current = updates.get(window.metric)
            if current is None or window.window_end_ms > current.window_end_ms:
                updates[window.metric] = window
    return PreparedWindows(
        latest=latest,
        updates=updates,
        last_clock_ms=max_last_clock,
    )


def _execute_round(
    store: Store,
    round_id: str,
    *,
    crash_after_start: bool = False,
    force_exit: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    with store.round_lock(round_id):
        actions = store.start_round_actions(round_id, reclaim_running=True)
        if not actions:
            return {
                "round_id": round_id,
                "executed": [],
                "status": "no_pending_actions",
            }
        if crash_after_start:
            # The test/demo path models SIGKILL immediately after the started
            # phase is durably committed.  Budget was charged in the earlier
            # reservation transaction, so recovery must not charge again.
            (force_exit or (lambda code: os._exit(code)))(97)
        barrier = threading.Barrier(len(actions))

        def run(action: Mapping[str, Any]) -> dict[str, Any]:
            barrier.wait()
            claimed = store.claim_action(str(action["action_key"]))
            if claimed is None:
                return {
                    "action_key": action["action_key"],
                    "status": "already_completed_or_claimed",
                }
            success, reason = store.action_outcome(claimed)
            finish_at = int(claimed["start_at_ms"]) + int(claimed["duration_ms"])
            return store.complete_action(
                str(claimed["action_key"]),
                success=success,
                finish_at_ms=finish_at,
                reason=reason,
            )

        executed: list[dict[str, Any]] = []
        errors: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=len(actions)) as executor:
            futures = [executor.submit(run, action) for action in actions]
            for future in futures:
                try:
                    executed.append(future.result())
                except BaseException as exc:  # all futures are drained first
                    errors.append(exc)
        if errors:
            raise errors[0]
        return {
            "round_id": round_id,
            "executed": sorted(
                executed, key=lambda item: str(item.get("action_key", ""))
            ),
            "status": "executed",
        }


def decide_round(
    store: Store,
    *,
    round_id: str,
    at_ms: int,
    workloads: Sequence[str] | None = None,
    expected_version: int | None = None,
    outcome_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    crash_after_start: bool = False,
    force_exit: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    store.require_loaded()
    with store.decision_session():
        bundle = store.load_bundle()
        current_version = store.policy_version()
        if expected_version is not None:
            if expected_version < current_version:
                raise ControlError(
                    VERSION_ROLLBACK,
                    "requested policy version is older than the committed version",
                    {
                        "expected_version": expected_version,
                        "committed_version": current_version,
                    },
                )
            if expected_version > current_version:
                raise ControlError(
                    VERSION_CONFLICT,
                    "requested policy version does not exist",
                    {
                        "expected_version": expected_version,
                        "committed_version": current_version,
                    },
                )
        if workloads is None:
            requested = sorted(bundle.workloads)
        else:
            requested = sorted(set(workloads))
            unknown = sorted(set(requested) - set(bundle.workloads))
            if unknown:
                raise ControlError(
                    UNKNOWN_WORKLOAD,
                    "round references unknown workloads",
                    {"workloads": unknown},
                )
        if not requested:
            raise ControlError(SCHEMA_ERROR, "a round must contain at least one workload")
        existing_info = store.round_info(round_id)
        if existing_info is not None and int(existing_info["policy_version"]) != current_version:
            raise ControlError(
                ROUND_CONFLICT,
                "round was created under a different policy version",
                {
                    "round_id": round_id,
                    "round_policy_version": int(existing_info["policy_version"]),
                    "committed_version": current_version,
                },
            )
        if (
            existing_info is not None
            and int(existing_info["created_clock_ms"]) != at_ms
        ):
            raise ControlError(
                ROUND_CONFLICT,
                "round already exists at a different logical clock",
                {
                    "round_id": round_id,
                    "round_clock_ms": int(existing_info["created_clock_ms"]),
                    "requested_clock_ms": at_ms,
                },
            )
        prepared = _prepare_windows(store, bundle, at_ms=at_ms)
        outcomes = {
            name: dict(value) for name, value in (outcome_overrides or {}).items()
        }
        hashes = {
            workload: _input_hash(
                workload=workload,
                at_ms=at_ms,
                policy_version=current_version,
                bundle=bundle,
                latest=prepared.latest,
                outcomes=outcomes,
            )
            for workload in requested
        }
        existing = store.round_workload_hashes(round_id)
        for workload in requested:
            if workload in existing and existing[workload] != hashes[workload]:
                raise ControlError(
                    ROUND_CONFLICT,
                    "the workload already has a different decision for this round",
                    {"round_id": round_id, "workload": workload},
                )
        missing = [workload for workload in requested if workload not in existing]
        if missing:
            decision_output = decide(
                bundle,
                list(prepared.latest.values()),
                store.replicas(),
                at_ms=at_ms,
                ledger=store.load_ledger(),
                workload_filter=set(missing),
            )
            commit = store.commit_round(
                round_id=round_id,
                at_ms=at_ms,
                decisions=decision_output,
                input_hashes=hashes,
                watermark_updates=prepared.updates,
                expected_version=expected_version,
                outcome_overrides=outcomes,
            )
        else:
            commit = {
                "round_id": round_id,
                "idempotent": True,
                "planned_actions": [
                    action
                    for action in store.pending_actions(round_id)
                    if action["workload"] in set(requested)
                ],
                "policy_version": current_version,
            }
        execution = _execute_round(
            store,
            round_id,
            crash_after_start=crash_after_start,
            force_exit=force_exit,
        )
        return {
            "round_id": round_id,
            "at_ms": at_ms,
            "workloads": requested,
            "commit": commit,
            "execution": execution,
            "status": store.status(),
        }


def resume(store: Store, *, crash_after_start: bool = False) -> dict[str, Any]:
    with store.decision_session():
        pending = store.pending_rounds()
        results = []
        for round_id in pending:
            results.append(
                _execute_round(
                    store,
                    round_id,
                    crash_after_start=crash_after_start,
                )
            )
        return {
            "resumed_rounds": pending,
            "results": results,
            "status": store.status(),
        }


def apply_patch(
    store: Store,
    patch_payload: Mapping[str, Any],
    *,
    expected_version: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    patch = PolicyPatch.from_payload(patch_payload)
    return store.apply_patch(
        patch, expected_version=expected_version, dry_run=dry_run
    )


def edit(store: Store, patch_payload: Mapping[str, Any], *, commit: bool = False) -> dict[str, Any]:
    return apply_patch(store, patch_payload, dry_run=not commit)
