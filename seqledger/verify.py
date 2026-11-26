"""Independent serial replay and canonical ledger comparison."""

from __future__ import annotations

from typing import Any, Iterable

from .engine import (
    _apply_action,
    _validate_patch_against_state,
    apply_merge_state,
    apply_patch_state,
    append_event,
    load_configuration,
    plan_next_action,
    refresh_conclusions,
)
from .errors import VERIFY_MISMATCH, VerifyError, InputError
from .store import LedgerStore, empty_state
from .util import canonical_json, sha256_text
from .validation import validate_patch


def _replay_in_memory(
    state: dict[str, Any],
    *,
    logical_clock_ms: int,
    step_limit: int = 10_000_000,
) -> int:
    actions = 0
    while actions < step_limit:
        action = plan_next_action(state, logical_clock_ms)
        if action is None:
            break
        _apply_action(state, action)
        state["logical_clock_ms"] = max(int(state["logical_clock_ms"]), logical_clock_ms)
        actions += 1
    if actions >= step_limit and plan_next_action(state, logical_clock_ms) is not None:
        raise VerifyError(
            "VERIFY_STEP_LIMIT",
            "Deterministic verification replay exceeded its step limit",
            diagnostics={"step_limit": step_limit},
        )
    state["logical_clock_ms"] = max(int(state["logical_clock_ms"]), logical_clock_ms)
    refresh_conclusions(state, logical_clock_ms, None)
    return actions


def _build_expected(
    *,
    topology: dict[str, Any],
    events: list[dict[str, Any]],
    subscriptions: list[dict[str, Any]],
    v1: dict[str, Any],
    v2: dict[str, Any],
    logical_clock_ms: int,
    merge_logical_clock_ms: int,
    patch_documents: Iterable[dict[str, Any]],
    patches_before_replay: bool,
) -> tuple[dict[str, Any], int, str]:
    state = empty_state()
    load_configuration(
        state,
        topology=topology,
        subscriptions=subscriptions,
    )
    for event in events:
        append_event(state, event)
    apply_merge_state(
        state,
        v1=v1,
        v2=v2,
        logical_clock_ms=merge_logical_clock_ms,
    )
    if patches_before_replay:
        for patch_raw in patch_documents:
            patch = validate_patch(
                patch_raw,
                current_v2_generation=int(
                    state["semantics"]["v2_current_generation"]
                ),
            )
            _validate_patch_against_state(state, patch)
            apply_patch_state(state, patch)
    action_count = _replay_in_memory(
        state,
        logical_clock_ms=logical_clock_ms,
    )
    if not patches_before_replay:
        for patch_raw in patch_documents:
            patch = validate_patch(
                patch_raw,
                current_v2_generation=int(
                    state["semantics"]["v2_current_generation"]
                ),
            )
            _validate_patch_against_state(state, patch)
            apply_patch_state(state, patch)
    state["logical_clock_ms"] = max(int(state["logical_clock_ms"]), logical_clock_ms)
    refresh_conclusions(state, int(state["logical_clock_ms"]), None)
    mode = "patch-before-replay" if patches_before_replay else "patch-after-replay"
    return state, action_count, mode


def _projection(state: dict[str, Any]) -> dict[str, Any]:
    partitions: dict[str, Any] = {}
    for pid, partition in sorted(state["partitions"].items()):
        partitions[pid] = {
            "topic": partition["topic"],
            "partition": partition["partition"],
            "partition_count": partition["partition_count"],
            "offsets": partition["offsets"],
            "committed_offset": partition["committed_offset"],
            "delivery_generation": partition["delivery_generation"],
            "inflight": partition["inflight"],
            "decisions": partition["decisions"],
            "dead_letters": partition["dead_letters"],
            "invalidations": partition["invalidations"],
            "recompute_pass": partition["recompute_pass"],
            "next_available_ms": partition["next_available_ms"],
            "conclusion": partition["conclusion"],
        }
    return {
        "schema": state["schema"],
        "logical_clock_ms": state["logical_clock_ms"],
        "topology": state["topology"],
        "subscriptions": state["subscriptions"],
        "event_index": state["event_index"],
        "last_event_seq": state["last_event_seq"],
        "semantics": state["semantics"],
        "delivery_claims": state["delivery_claims"],
        "dead_letter_index": state["dead_letter_index"],
        "invalidation_history": state["invalidation_history"],
        "partitions": partitions,
    }


def _event_status(state: dict[str, Any], event_seq: int) -> dict[str, Any]:
    decision = None
    for partition in state["partitions"].values():
        for candidate in partition["decisions"]:
            if int(candidate["event_seq"]) == event_seq:
                if decision is None or candidate["decision_id"] >= decision["decision_id"]:
                    decision = candidate
    claim = None
    for candidate in state["delivery_claims"].values():
        if int(candidate["event_seq"]) == event_seq:
            claim = candidate
    return {
        "status": decision["status"] if decision else (claim["status"] if claim else "appended"),
        "delivery_generation": (
            int(decision["delivery_generation"])
            if decision
            else (int(claim["delivery_generation"]) if claim else None)
        ),
        "reason": decision.get("reason") if decision else None,
    }


def verify_ledger(
    store: LedgerStore,
    *,
    topology: dict[str, Any],
    events: list[dict[str, Any]],
    subscriptions: list[dict[str, Any]],
    v1: dict[str, Any],
    v2: dict[str, Any],
    logical_clock_ms: int,
    merge_logical_clock_ms: int,
    patch_documents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    patch_documents = patch_documents or []
    inspection = store.inspect()
    actual_state, metadata = store.read_state()
    if int(logical_clock_ms) < int(actual_state["logical_clock_ms"]):
        raise InputError(
            "LOGICAL_CLOCK_INVALID",
            "Verification clock is behind the persisted logical clock watermark",
            diagnostics={
                "ledger_logical_clock_ms": actual_state["logical_clock_ms"],
                "verification_logical_clock_ms": logical_clock_ms,
            },
        )
    actual_projection = _projection(actual_state)
    actual_bytes = canonical_json(actual_projection)
    actual_sha = sha256_text(actual_bytes)

    candidate_modes = [False] if not patch_documents else [False, True]
    expected_state: dict[str, Any] | None = None
    expected_sha: str | None = None
    action_count = 0
    mode = "no-patch"
    failures: list[dict[str, Any]] = []
    for patches_before in candidate_modes:
        candidate, candidate_actions, candidate_mode = _build_expected(
            topology=topology,
            events=events,
            subscriptions=subscriptions,
            v1=v1,
            v2=v2,
            logical_clock_ms=logical_clock_ms,
            merge_logical_clock_ms=merge_logical_clock_ms,
            patch_documents=patch_documents,
            patches_before_replay=patches_before,
        )
        candidate_projection = _projection(candidate)
        candidate_bytes = canonical_json(candidate_projection)
        candidate_sha = sha256_text(candidate_bytes)
        if candidate_sha == actual_sha:
            expected_state = candidate
            expected_sha = candidate_sha
            action_count = candidate_actions
            mode = candidate_mode
            break
        failures.append(
            {
                "mode": candidate_mode,
                "expected_sha256": candidate_sha,
            }
        )

    if expected_state is None or expected_sha is None:
        raise VerifyError(
            VERIFY_MISMATCH,
            "Persisted ledger does not match deterministic serial replay",
            diagnostics={
                "actual_sha256": actual_sha,
                "candidate_modes": failures,
                "ledger_version": metadata.version,
            },
        )

    partition_checks: list[dict[str, Any]] = []
    for pid in sorted(actual_state["partitions"]):
        actual_partition = actual_state["partitions"][pid]
        expected_partition = expected_state["partitions"][pid]
        actual_part_sha = sha256_text(canonical_json(actual_partition))
        expected_part_sha = sha256_text(canonical_json(expected_partition))
        partition_checks.append(
            {
                "partition_id": pid,
                "match": actual_part_sha == expected_part_sha,
                "actual_sha256": actual_part_sha,
                "expected_sha256": expected_part_sha,
                "committed_offset": actual_partition["committed_offset"],
                "decision_count": len(actual_partition["decisions"]),
                "dead_letter_count": len(actual_partition["dead_letters"]),
            }
        )
    event_checks: list[dict[str, Any]] = []
    for event in events:
        expected_status = _event_status(expected_state, int(event["event_seq"]))
        actual_status = _event_status(actual_state, int(event["event_seq"]))
        event_checks.append(
            {
                "event_seq": int(event["event_seq"]),
                "partition_key": event["partition_key"],
                "expected": expected_status,
                "actual": actual_status,
                "match": expected_status == actual_status,
            }
        )
    if not all(check["match"] for check in partition_checks + event_checks):
        raise VerifyError(
            VERIFY_MISMATCH,
            "Partition or event-level ledger comparison failed",
            diagnostics={
                "partition_mismatches": [
                    check for check in partition_checks if not check["match"]
                ],
                "event_mismatches": [
                    check for check in event_checks if not check["match"]
                ],
            },
        )
    return {
        "ok": True,
        "schema": "seqledger-verify-result/v1",
        "bitwise_match": actual_bytes == canonical_json(_projection(expected_state)),
        "reconciliation_mode": mode,
        "ledger_version": metadata.version,
        "ledger_versions_checked": inspection["version_count"],
        "logical_clock_ms": logical_clock_ms,
        "serial_action_count": action_count,
        "actual_projection_sha256": actual_sha,
        "expected_projection_sha256": expected_sha,
        "partition_checks": partition_checks,
        "event_checks": event_checks,
        "conclusion": "serial replay and persisted ledger are bitwise-identical",
    }
