"""Deterministic routing, replay, acknowledgement and invalidation engine."""

from __future__ import annotations

import hashlib
import math
from copy import deepcopy
from typing import Any, Iterable

from .errors import (
    ACK_CONFLICT,
    DELIVERY_CLAIM_CONFLICT,
    EVENT_CONFLICT,
    EVENT_OUT_OF_ORDER,
    LEDGER_VERSION_REGRESSION,
    LEDGER_CONFIG_CONFLICT,
    LOGICAL_CLOCK_INVALID,
    LOGICAL_CLOCK_ROLLBACK,
    PARTITION_MISSING,
    PATCH_GENERATION_REGRESSION,
    PATCH_SCHEMA_INVALID,
    PATCH_TARGET_MISSING,
    PATCH_WATERMARK_INVALID,
    REPLAY_INFLIGHT_CONFLICT,
    REPLAY_NO_SEMANTICS,
    REPLAY_NO_TOPOLOGY,
    SEMANTIC_GENERATION_REGRESSION,
    StoreError,
    InputError,
    ReplayError,
)
from .merge import merge_snapshots
from .store import CommitResult, LedgerStore
from .util import canonical_json, sha256_text
from .validation import (
    topology_index,
    validate_event_against_topology,
    validate_logical_clock,
    validate_patch,
)


def partition_id(topic: str, partition: int) -> str:
    return f"{topic}/{partition}"


def route_partition(topic_definition: dict[str, Any], partition_key: str) -> tuple[int, str]:
    digest = hashlib.sha256(
        f"{topic_definition['name']}\x1f{partition_key}".encode("utf-8")
    ).hexdigest()
    return int(digest, 16) % topic_definition["partition_count"], digest


def _ensure_partitions(state: dict[str, Any]) -> None:
    topology = state.get("topology")
    if topology is None:
        return
    for topic in topology["topics"]:
        for number in range(topic["partition_count"]):
            key = partition_id(topic["name"], number)
            if key in state["partitions"]:
                continue
            state["partitions"][key] = {
                "topic": topic["name"],
                "partition": number,
                "partition_count": topic["partition_count"],
                "offsets": [],
                "committed_offset": -1,
                "inflight": None,
                "delivery_generation": 0,
                "decisions": [],
                "dead_letters": [],
                "delivery_claims": [],
                "invalidations": [],
                "recompute_pass": 0,
                "next_decision_seq": 0,
                "next_available_ms": 0,
                "conclusion": None,
            }


def _operation(state: dict[str, Any], kind: str, logical_clock_ms: int, **details: Any) -> None:
    sequence = int(state.get("next_operation_seq", 0))
    state["next_operation_seq"] = sequence + 1
    record = {
        "operation_seq": sequence,
        "kind": kind,
        "logical_clock_ms": logical_clock_ms,
    }
    record.update(details)
    state.setdefault("operations", []).append(record)


def load_configuration(
    state: dict[str, Any],
    *,
    topology: dict[str, Any],
    subscriptions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    existing_topology = state.get("topology")
    if existing_topology is not None and canonical_json(existing_topology) != canonical_json(topology):
        raise InputError(
            LEDGER_CONFIG_CONFLICT,
            "Ledger already contains a different topology declaration",
            diagnostics={
                "existing_topics": [topic["name"] for topic in existing_topology["topics"]],
                "incoming_topics": [topic["name"] for topic in topology["topics"]],
            },
        )
    existing_subscriptions = state.get("subscriptions") or []
    if existing_subscriptions and canonical_json(existing_subscriptions) != canonical_json(subscriptions):
        raise InputError(
            LEDGER_CONFIG_CONFLICT,
            "Ledger already contains different subscriber declarations",
            diagnostics={
                "existing_subscriber_ids": [
                    subscription["subscriber_id"]
                    for subscription in existing_subscriptions
                ],
                "incoming_subscriber_ids": [
                    subscription["subscriber_id"] for subscription in subscriptions
                ],
            },
        )
    changed = False
    if existing_topology is None:
        state["topology"] = deepcopy(topology)
        changed = True
    if not existing_subscriptions:
        state["subscriptions"] = deepcopy(subscriptions)
        changed = True
    _ensure_partitions(state)
    if changed:
        _operation(
            state,
            "load_configuration",
            int(state["logical_clock_ms"]),
            topics=len(topology["topics"]),
            subscriptions=len(subscriptions),
        )
        return state
    return None


def append_event(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    if state.get("topology") is None:
        raise ReplayError(
            REPLAY_NO_TOPOLOGY,
            "Cannot append events before the topology is loaded",
        )
    topics = topology_index(state["topology"])
    sequence = int(event["event_seq"])
    existing = state["event_index"].get(str(sequence))
    topic = str(event["topic"])
    if existing is not None:
        expected = {
            "event_seq": sequence,
            "topic": topic,
            "partition_key": event["partition_key"],
            "payload_digest": event["payload_digest"],
            "logical_ts_ms": event["logical_ts_ms"],
            "expected_delivery_generation": event["expected_delivery_generation"],
        }
        comparable = {
            key: existing.get(key)
            for key in expected
        }
        if comparable != expected:
            raise InputError(
                EVENT_CONFLICT,
                f"Conflicting duplicate event sequence: {sequence}",
                diagnostics={
                    "event_seq": sequence,
                    "existing": comparable,
                    "incoming": expected,
                },
            )
        return None
    if sequence <= int(state.get("last_event_seq", -1)):
        raise InputError(
            EVENT_OUT_OF_ORDER,
            f"Event sequence {sequence} is behind the append frontier",
            diagnostics={
                "event_seq": sequence,
                "append_frontier": int(state.get("last_event_seq", -1)),
            },
        )
    validate_event_against_topology(
        event,
        topics,
        sequence=sequence,
        path=f"event[{sequence}]",
    )
    topic_definition = topics[topic]
    number, routing_hash = route_partition(topic_definition, event["partition_key"])
    pid = partition_id(topic, number)
    partition = state["partitions"].get(pid)
    if partition is None:
        raise InputError(
            PARTITION_MISSING,
            f"Routed partition does not exist: {pid}",
            diagnostics={
                "topic": topic,
                "partition": number,
                "partition_key": event["partition_key"],
            },
        )
    offset = len(partition["offsets"])
    if offset and partition["offsets"][-1]["offset"] + 1 != offset:
        raise ReplayError(
            "PARTITION_OFFSET_GAP",
            f"Partition {pid} has a non-contiguous append frontier",
            diagnostics={"partition": pid, "offset": offset},
        )
    record = {
        "offset": offset,
        "event_seq": sequence,
        "partition_key": event["partition_key"],
        "payload_digest": event["payload_digest"],
        "expected_delivery_generation": event["expected_delivery_generation"],
        "logical_ts_ms": event["logical_ts_ms"],
        "routing_sha256": routing_hash,
        "routing_partition_count": topic_definition["partition_count"],
    }
    partition["offsets"].append(record)
    state["event_index"][str(sequence)] = {
        "event_seq": sequence,
        "topic": topic,
        "partition": number,
        "partition_id": pid,
        "offset": offset,
        "partition_key": event["partition_key"],
        "payload_digest": event["payload_digest"],
        "logical_ts_ms": event["logical_ts_ms"],
        "expected_delivery_generation": event["expected_delivery_generation"],
    }
    state["last_event_seq"] = sequence
    return state


def _semantics_index(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    view = state.get("semantics", {}).get("view")
    if not view:
        return {}
    return {entry["message_key"]: entry for entry in view.get("entries", [])}


def apply_merge_state(
    state: dict[str, Any],
    *,
    v1: dict[str, Any],
    v2: dict[str, Any],
    logical_clock_ms: int,
) -> dict[str, Any]:
    current_generation = int(state.get("semantics", {}).get("v2_current_generation", 0))
    if int(v2["current_generation"]) < current_generation:
        raise ReplayError(
            SEMANTIC_GENERATION_REGRESSION,
            "Merged v2 snapshot would move the semantic generation backwards",
            diagnostics={
                "current_v2_generation": current_generation,
                "incoming_v2_generation": int(v2["current_generation"]),
            },
        )
    if logical_clock_ms < int(state["logical_clock_ms"]):
        raise ReplayError(
            LOGICAL_CLOCK_ROLLBACK,
            "Merge logical clock would move the ledger watermark backwards",
            diagnostics={
                "current_logical_clock_ms": int(state["logical_clock_ms"]),
                "requested_logical_clock_ms": logical_clock_ms,
            },
        )
    view = merge_snapshots(
        v1,
        v2,
        injected_logical_clock_ms=logical_clock_ms,
    )
    old_view = state.get("semantics", {}).get("view")
    changed = old_view is None or canonical_json(old_view) != canonical_json(view)
    state["semantics"]["view"] = view
    state["semantics"]["v2_current_generation"] = int(v2["current_generation"])
    state["semantics"]["last_merge_clock_ms"] = logical_clock_ms
    state["logical_clock_ms"] = max(int(state["logical_clock_ms"]), logical_clock_ms)
    conclusion_changed = refresh_conclusions(state, logical_clock_ms, None)
    if changed or conclusion_changed:
        _operation(
            state,
            "semantic_merge",
            logical_clock_ms,
            v2_current_generation=int(v2["current_generation"]),
            entry_count=len(view["entries"]),
            conflict_count=int(view["decision_counts"]["conflicts"]),
            view_sha256=sha256_text(canonical_json(view)),
        )
        return state
    return None


def merge_ledger(
    store: LedgerStore,
    *,
    v1: dict[str, Any],
    v2: dict[str, Any],
    logical_clock_ms: Any,
    expected_version: int | None = None,
) -> dict[str, Any]:
    clock = validate_logical_clock(logical_clock_ms)
    state, metadata = store.read_state()

    def mutator(working: dict[str, Any]) -> dict[str, Any] | None:
        return apply_merge_state(
            working,
            v1=v1,
            v2=v2,
            logical_clock_ms=clock,
        )

    result = store.commit(mutator, expected_version=expected_version)
    state_after, metadata_after = store.read_state()
    view = state_after["semantics"]["view"]
    return {
        "ok": True,
        "schema": "seqledger-merge-result/v1",
        "idempotent": not result.changed,
        "ledger_version": metadata_after.version,
        "injected_logical_clock_ms": clock,
        "format_migration": {
            "decision_counts": view["decision_counts"],
            "priority": view["priority"],
            "v1_conversion_count": len(view["conversions"]),
            "v1_source_clock_ms": view["v1_source"]["logical_clock_ms"],
            "v2_current_generation": view["v2_source"]["current_generation"],
            "shards": view["v2_source"]["shards"],
        },
        "unified_view": view,
    }


def _subscription_for_topic(
    state: dict[str, Any],
    topic: str,
) -> dict[str, Any] | None:
    candidates = [
        subscription
        for subscription in state.get("subscriptions", [])
        if subscription["topic"] == topic
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item["subscriber_id"])[0]


def _rate_interval_ms(subscription: dict[str, Any]) -> int:
    return max(1, int(math.ceil(1000.0 / float(subscription["max_rate_per_sec"]))))


def _claim_key(event_seq: int, generation: int) -> str:
    return f"{event_seq}:{generation}"


def _dependency_status(
    state: dict[str, Any],
    entry: dict[str, Any],
) -> tuple[bool, str | None]:
    dependency = entry.get("dependency")
    if not dependency:
        return True, None
    pid = partition_id(dependency["topic"], dependency["partition"])
    partition = state["partitions"].get(pid)
    if partition is None:
        return False, "upstream_missing"
    offset = int(dependency["upstream_offset"])
    if offset < 0 or offset >= len(partition["offsets"]):
        return False, "upstream_missing"
    if partition["committed_offset"] < offset:
        return False, "upstream_not_caught_up"
    return True, None


def evaluate_partition(
    state: dict[str, Any],
    pid: str,
    now_ms: int,
    semantics: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    partition = state["partitions"][pid]
    committed = int(partition["committed_offset"])
    next_offset = committed + 1
    backlog = len(partition["offsets"]) - next_offset
    base = {
        "partition_id": pid,
        "committed_offset": committed,
        "next_offset": next_offset,
        "backlog_depth": max(0, backlog),
        "delivery_generation": int(partition["delivery_generation"]),
    }
    inflight = partition.get("inflight")
    if inflight is not None:
        if int(inflight["ack_due_ms"]) <= now_ms:
            return {**base, "kind": "ack_due", "inflight": inflight}
        return {
            **base,
            "kind": "blocked",
            "blocked_reason": "awaiting_ack",
            "ack_due_ms": int(inflight["ack_due_ms"]),
        }
    if next_offset >= len(partition["offsets"]):
        return {**base, "kind": "done", "blocked_reason": None}

    event = partition["offsets"][next_offset]
    entry = (semantics or {}).get(event["partition_key"])
    subscriber = _subscription_for_topic(state, partition["topic"])
    result = {
        **base,
        "event": event,
        "semantic_entry": entry,
        "subscription": subscriber,
    }
    if event["logical_ts_ms"] > now_ms:
        return {
            **result,
            "kind": "blocked",
            "blocked_reason": "event_not_yet_effective",
        }
    if entry is None:
        return {
            **result,
            "kind": "blocked",
            "blocked_reason": "semantics_missing",
        }
    if int(entry["effective_ms"]) > now_ms:
        return {
            **result,
            "kind": "blocked",
            "blocked_reason": "semantics_not_effective",
        }
    if subscriber is None:
        return {
            **result,
            "kind": "blocked",
            "blocked_reason": "subscriber_missing",
        }
    dependency_ready, dependency_reason = _dependency_status(state, entry)
    if not dependency_ready:
        return {
            **result,
            "kind": "blocked",
            "blocked_reason": dependency_reason,
        }
    if entry.get("tombstone") is not None:
        start_ms = max(
            int(partition["next_available_ms"]),
            int(event["logical_ts_ms"]),
            int(entry["effective_ms"]),
        )
        return {
            **result,
            "kind": "dead_letter",
            "dead_letter_reason": "tombstone_active",
            "start_ms": start_ms,
        }
    generation = int(entry["semantic_generation"])
    if int(event["expected_delivery_generation"]) > generation:
        return {
            **result,
            "kind": "blocked",
            "blocked_reason": "awaiting_delivery_generation",
            "available_generation": generation,
        }
    claim = state["delivery_claims"].get(_claim_key(event["event_seq"], generation))
    if claim is not None:
        status = claim.get("status")
        if status == "invalidated":
            return {
                **result,
                "kind": "dead_letter",
                "dead_letter_reason": "delivery_claim_invalidated",
                "start_ms": max(
                    int(partition["next_available_ms"]),
                    int(event["logical_ts_ms"]),
                    int(entry["effective_ms"]),
                ),
                "generation": generation,
            }
        raise ReplayError(
            DELIVERY_CLAIM_CONFLICT,
            f"Event {event['event_seq']} already has a non-terminal claim at generation {generation}",
            diagnostics={
                "event_seq": event["event_seq"],
                "generation": generation,
                "claim": claim,
                "partition": pid,
            },
        )
    start_ms = max(
        int(partition["next_available_ms"]),
        int(event["logical_ts_ms"]),
        int(entry["effective_ms"]),
    )
    if start_ms > now_ms:
        return {
            **result,
            "kind": "blocked",
            "blocked_reason": "rate_limit_until",
            "available_at_ms": start_ms,
        }
    return {
        **result,
        "kind": "deliverable",
        "start_ms": start_ms,
        "generation": generation,
        "ack_due_ms": start_ms + _rate_interval_ms(subscriber),
        "subscriber_id": subscriber["subscriber_id"],
    }


def _selected_partitions(
    state: dict[str, Any],
    partition_filter: set[str] | None,
) -> list[str]:
    all_partitions = sorted(state["partitions"])
    if partition_filter is None:
        return all_partitions
    unknown = sorted(partition_filter - set(all_partitions))
    if unknown:
        raise ReplayError(
            PARTITION_MISSING,
            "Requested partition is not declared by the ledger topology",
            diagnostics={"unknown_partitions": unknown},
        )
    return sorted(partition_filter)


def plan_next_action(
    state: dict[str, Any],
    now_ms: int,
    *,
    partition_filter: set[str] | None = None,
) -> dict[str, Any] | None:
    semantics = _semantics_index(state)
    selected = _selected_partitions(state, partition_filter)
    ack_candidates: list[dict[str, Any]] = []
    other_candidates: list[dict[str, Any]] = []
    for pid in selected:
        evaluation = evaluate_partition(state, pid, now_ms, semantics)
        if evaluation["kind"] == "ack_due":
            ack_candidates.append(
                {
                    "kind": "ack",
                    "partition_id": pid,
                    "ack_due_ms": int(evaluation["inflight"]["ack_due_ms"]),
                }
            )
            continue
        if evaluation["kind"] == "deliverable":
            evaluation["kind"] = "start"
            evaluation["action_kind"] = "start"
            other_candidates.append(evaluation)
        elif evaluation["kind"] == "dead_letter":
            evaluation["kind"] = "dead_letter"
            evaluation["action_kind"] = "dead_letter"
            other_candidates.append(evaluation)
    if ack_candidates:
        return min(ack_candidates, key=lambda item: (item["ack_due_ms"], item["partition_id"]))
    if other_candidates:
        return min(
            other_candidates,
            key=lambda item: (
                int(item["start_ms"]),
                item["partition_id"],
                0 if item["action_kind"] == "dead_letter" else 1,
            ),
        )
    return None


def _next_decision_id(state: dict[str, Any], pid: str) -> str:
    partition = state["partitions"][pid]
    sequence = int(partition.get("next_decision_seq", 0))
    partition["next_decision_seq"] = sequence + 1
    return f"{pid}:d{sequence:08d}"


def _apply_action(state: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    pid = action["partition_id"]
    partition = state["partitions"][pid]
    if action["kind"] == "ack":
        inflight = partition.get("inflight")
        if inflight is None:
            raise ReplayError(
                REPLAY_INFLIGHT_CONFLICT,
                f"Partition {pid} has no in-flight delivery to acknowledge",
                diagnostics={"partition": pid},
            )
        decision_id = _next_decision_id(state, pid)
        decision = {
            "decision_id": decision_id,
            "event_seq": int(inflight["event_seq"]),
            "offset": int(inflight["offset"]),
            "delivery_generation": int(inflight["delivery_generation"]),
            "expected_delivery_generation": int(inflight["expected_delivery_generation"]),
            "payload_digest": inflight["payload_digest"],
            "semantic_source_version": inflight["semantic_source_version"],
            "subscriber_id": inflight["subscriber_id"],
            "status": "acked",
            "started_at_ms": int(inflight["started_at_ms"]),
            "acked_at_ms": int(inflight["ack_due_ms"]),
            "recompute_pass": int(partition["recompute_pass"]),
        }
        partition["decisions"].append(decision)
        partition["committed_offset"] = int(inflight["offset"])
        partition["delivery_generation"] = max(
            int(partition["delivery_generation"]),
            int(inflight["delivery_generation"]),
        )
        partition["next_available_ms"] = max(
            int(partition["next_available_ms"]),
            int(inflight["ack_due_ms"]),
        )
        claim_key = inflight["claim_key"]
        claim = state["delivery_claims"][claim_key]
        claim["status"] = "acked"
        claim["acked_at_ms"] = int(inflight["ack_due_ms"])
        claim["decision_id"] = decision_id
        partition["inflight"] = None
        _operation(
            state,
            "delivery_acked",
            int(inflight["ack_due_ms"]),
            partition=pid,
            offset=int(inflight["offset"]),
            event_seq=int(inflight["event_seq"]),
            delivery_generation=int(inflight["delivery_generation"]),
            decision_id=decision_id,
        )
        return state

    if action["kind"] in {"start", "dead_letter"}:
        event = action["event"]
        entry = action["semantic_entry"]
        generation = int(action.get("generation", entry["semantic_generation"]))
        claim_key = _claim_key(int(event["event_seq"]), generation)
        if action["kind"] == "start":
            if partition.get("inflight") is not None:
                raise ReplayError(
                    REPLAY_INFLIGHT_CONFLICT,
                    f"Partition {pid} would have two in-flight deliveries",
                    diagnostics={"partition": pid, "inflight": partition["inflight"]},
                )
            if claim_key in state["delivery_claims"]:
                raise ReplayError(
                    DELIVERY_CLAIM_CONFLICT,
                    f"Delivery claim already exists: {claim_key}",
                    diagnostics={"claim_key": claim_key, "partition": pid},
                )
            decision_id = _next_decision_id(state, pid)
            inflight = {
                "decision_id": decision_id,
                "claim_key": claim_key,
                "event_seq": int(event["event_seq"]),
                "offset": int(event["offset"]),
                "partition_key": event["partition_key"],
                "payload_digest": event["payload_digest"],
                "expected_delivery_generation": int(event["expected_delivery_generation"]),
                "delivery_generation": generation,
                "semantic_source_version": entry["source_version"],
                "subscriber_id": action["subscriber_id"],
                "started_at_ms": int(action["start_ms"]),
                "ack_due_ms": int(action["ack_due_ms"]),
            }
            partition["inflight"] = inflight
            state["delivery_claims"][claim_key] = {
                "claim_key": claim_key,
                "event_seq": int(event["event_seq"]),
                "offset": int(event["offset"]),
                "delivery_generation": generation,
                "status": "inflight",
                "decision_id": decision_id,
            }
            _operation(
                state,
                "delivery_started",
                int(action["start_ms"]),
                partition=pid,
                offset=int(event["offset"]),
                event_seq=int(event["event_seq"]),
                delivery_generation=generation,
                ack_due_ms=int(action["ack_due_ms"]),
                subscriber_id=action["subscriber_id"],
                decision_id=decision_id,
            )
            return state

        decision_id = _next_decision_id(state, pid)
        reason = action["dead_letter_reason"]
        decision = {
            "decision_id": decision_id,
            "event_seq": int(event["event_seq"]),
            "offset": int(event["offset"]),
            "delivery_generation": generation,
            "expected_delivery_generation": int(event["expected_delivery_generation"]),
            "payload_digest": event["payload_digest"],
            "semantic_source_version": entry["source_version"],
            "status": "dead_letter",
            "reason": reason,
            "decided_at_ms": int(action["start_ms"]),
            "recompute_pass": int(partition["recompute_pass"]),
        }
        partition["decisions"].append(decision)
        partition["dead_letters"].append(decision)
        partition["committed_offset"] = int(event["offset"])
        partition["next_available_ms"] = max(
            int(partition["next_available_ms"]),
            int(action["start_ms"]),
        )
        state["delivery_claims"][claim_key] = {
            "claim_key": claim_key,
            "event_seq": int(event["event_seq"]),
            "offset": int(event["offset"]),
            "delivery_generation": generation,
            "status": "dead_letter",
            "decision_id": decision_id,
            "reason": reason,
        }
        state["dead_letter_index"][str(event["event_seq"])] = {
            "partition_id": pid,
            "offset": int(event["offset"]),
            "delivery_generation": generation,
            "reason": reason,
            "decision_id": decision_id,
        }
        _operation(
            state,
            "dead_letter",
            int(action["start_ms"]),
            partition=pid,
            offset=int(event["offset"]),
            event_seq=int(event["event_seq"]),
            delivery_generation=generation,
            reason=reason,
            decision_id=decision_id,
        )
        return state
    raise ReplayError(
        "INTERNAL_REPLAY_ACTION",
        f"Unknown replay action: {action['kind']}",
    )


def partition_conclusion(
    state: dict[str, Any],
    pid: str,
    now_ms: int,
) -> dict[str, Any]:
    evaluation = evaluate_partition(state, pid, now_ms, _semantics_index(state))
    partition = state["partitions"][pid]
    next_offset = int(partition["committed_offset"]) + 1
    deliverable_offset: int | None = None
    blocked_reason = evaluation.get("blocked_reason")
    if evaluation["kind"] in {"deliverable", "ack_due"}:
        deliverable_offset = next_offset if evaluation["kind"] == "deliverable" else None
    if evaluation["kind"] == "dead_letter":
        blocked_reason = f"dead_letter_ready:{evaluation['dead_letter_reason']}"
    if evaluation["kind"] == "done":
        blocked_reason = None
    entry = evaluation.get("semantic_entry")
    return {
        "clock_ms": now_ms,
        "deliverable_offset": deliverable_offset,
        "blocked_reason": blocked_reason,
        "backlog_depth": max(
            0,
            len(partition["offsets"]) - (int(partition["committed_offset"]) + 1),
        ),
        "inflight_offset": (
            int(partition["inflight"]["offset"])
            if partition.get("inflight") is not None
            else None
        ),
        "semantic_generation": (
            int(entry["semantic_generation"]) if entry is not None else None
        ),
        "delivery_generation": int(partition["delivery_generation"]),
    }


def refresh_conclusions(
    state: dict[str, Any],
    now_ms: int,
    partition_filter: set[str] | None,
) -> bool:
    changed = False
    for pid in _selected_partitions(state, partition_filter):
        conclusion = partition_conclusion(state, pid, now_ms)
        if state["partitions"][pid].get("conclusion") != conclusion:
            state["partitions"][pid]["conclusion"] = conclusion
            changed = True
    return changed


def replay_ledger(
    store: LedgerStore,
    *,
    logical_clock_ms: Any,
    partition_filter: Iterable[str] | None = None,
    max_steps: int | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    now_ms = validate_logical_clock(logical_clock_ms)
    if max_steps is not None and max_steps < 1:
        raise ReplayError(
            LOGICAL_CLOCK_INVALID,
            "max_steps must be at least one",
            diagnostics={"max_steps": max_steps},
        )
    filter_set = set(partition_filter) if partition_filter is not None else None
    state, metadata = store.read_state()
    if expected_version is not None and expected_version != metadata.version:
        raise StoreError(
            LEDGER_VERSION_REGRESSION,
            "Expected ledger version does not match the current pointer",
            diagnostics={
                "expected_version": expected_version,
                "current_version": metadata.version,
            },
        )
    if now_ms < int(state["logical_clock_ms"]):
        raise ReplayError(
            LOGICAL_CLOCK_ROLLBACK,
            "Injected logical clock would move the ledger watermark backwards",
            diagnostics={
                "current_logical_clock_ms": int(state["logical_clock_ms"]),
                "requested_logical_clock_ms": now_ms,
            },
        )
    if state.get("topology") is None:
        raise ReplayError(
            REPLAY_NO_TOPOLOGY,
            "Replay requires a loaded topology and event stream",
        )
    if not state.get("semantics", {}).get("view"):
        raise ReplayError(
            REPLAY_NO_SEMANTICS,
            "Replay requires a merged semantic view",
        )
    _selected_partitions(state, filter_set)

    actions: list[dict[str, Any]] = []
    completed = True
    while True:
        if max_steps is not None and len(actions) >= max_steps:
            completed = False
            break
        captured: dict[str, Any] = {}

        def mutator(working: dict[str, Any]) -> dict[str, Any] | None:
            action = plan_next_action(working, now_ms, partition_filter=filter_set)
            if action is None:
                return None
            captured["action"] = _action_summary(action)
            _apply_action(working, action)
            working["logical_clock_ms"] = max(
                int(working["logical_clock_ms"]),
                now_ms,
            )
            return working

        result = store.commit(mutator)
        if not result.changed:
            break
        if "action" in captured:
            actions.append(captured["action"])
        if len(actions) > 10_000_000:
            raise ReplayError(
                "REPLAY_STEP_LIMIT",
                "Replay exceeded the safety step limit",
                diagnostics={"actions": len(actions)},
            )

    def finalize(working: dict[str, Any]) -> dict[str, Any] | None:
        changed = False
        if now_ms > int(working["logical_clock_ms"]):
            working["logical_clock_ms"] = now_ms
            changed = True
        if refresh_conclusions(working, now_ms, filter_set):
            changed = True
        if changed:
            _operation(
                working,
                "replay_window",
                now_ms,
                partition_filter=sorted(filter_set) if filter_set is not None else "all",
                action_count=len(actions),
                completed=completed,
            )
        return working if changed else None

    final_result = store.commit(finalize)
    state_after, metadata_after = store.read_state()
    selected_after = _selected_partitions(state_after, filter_set)
    partition_summary = []
    for pid in selected_after:
        partition = state_after["partitions"][pid]
        conclusion = partition.get("conclusion") or partition_conclusion(
            state_after,
            pid,
            now_ms,
        )
        partition_summary.append(
            {
                "partition_id": pid,
                "committed_offset": int(partition["committed_offset"]),
                "committed_watermark": int(partition["committed_offset"]) + 1,
                "deliverable_offset": conclusion.get("deliverable_offset"),
                "backlog_depth": conclusion.get("backlog_depth"),
                "delivery_generation": int(partition["delivery_generation"]),
                "blocked_reason": conclusion.get("blocked_reason"),
            }
        )
    return {
        "ok": True,
        "schema": "seqledger-replay-result/v1",
        "logical_clock_ms": now_ms,
        "partition_filter": sorted(filter_set) if filter_set is not None else None,
        "completed": completed,
        "action_count": len(actions),
        "actions": actions,
        "ledger_version_before": metadata.version,
        "ledger_version_after": metadata_after.version,
        "final_commit_changed": final_result.changed,
        "partitions": partition_summary,
        "backlog_depth": sum(
            max(
                0,
                len(partition["offsets"]) - (int(partition["committed_offset"]) + 1),
            )
            for partition in state_after["partitions"].values()
            if filter_set is None or partition_id(partition["topic"], partition["partition"]) in filter_set
        ),
    }


def _action_summary(action: dict[str, Any]) -> dict[str, Any]:
    event = action.get("event")
    return {
        "kind": action["kind"],
        "partition_id": action["partition_id"],
        "offset": int(event["offset"]) if event is not None else action.get("offset"),
        "event_seq": int(event["event_seq"]) if event is not None else None,
        "delivery_generation": action.get("generation"),
        "reason": action.get("dead_letter_reason"),
        "start_ms": action.get("start_ms"),
        "ack_due_ms": action.get("ack_due_ms"),
    }


def acknowledge(
    store: LedgerStore,
    *,
    event_seq: int,
    delivery_generation: int,
    logical_clock_ms: Any,
) -> dict[str, Any]:
    clock = validate_logical_clock(logical_clock_ms)
    state, metadata = store.read_state()
    if clock < int(state["logical_clock_ms"]):
        raise ReplayError(
            LOGICAL_CLOCK_ROLLBACK,
            "Acknowledgment clock would move the ledger watermark backwards",
            diagnostics={
                "current_logical_clock_ms": state["logical_clock_ms"],
                "requested_logical_clock_ms": clock,
            },
        )

    existing = state["delivery_claims"].get(_claim_key(event_seq, delivery_generation))
    if existing is not None and existing.get("status") in {"acked", "dead_letter"}:
        return {
            "ok": True,
            "idempotent": True,
            "event_seq": event_seq,
            "delivery_generation": delivery_generation,
            "ledger_version": metadata.version,
        }

    captured: dict[str, Any] = {}

    def mutator(working: dict[str, Any]) -> dict[str, Any] | None:
        claim_key = _claim_key(event_seq, delivery_generation)
        claim = working["delivery_claims"].get(claim_key)
        if claim is not None and claim.get("status") in {"acked", "dead_letter"}:
            return None
        target_partition: dict[str, Any] | None = None
        for partition in working["partitions"].values():
            inflight = partition.get("inflight")
            if (
                inflight is not None
                and int(inflight["event_seq"]) == event_seq
                and int(inflight["delivery_generation"]) == delivery_generation
            ):
                target_partition = partition
                break
        if target_partition is None:
            raise ReplayError(
                ACK_CONFLICT,
                "No matching in-flight delivery can be acknowledged",
                diagnostics={
                    "event_seq": event_seq,
                    "delivery_generation": delivery_generation,
                },
            )
        inflight = target_partition["inflight"]
        acked_at = max(int(inflight["ack_due_ms"]), clock)
        decision_id = _next_decision_id(
            working,
            partition_id(target_partition["topic"], target_partition["partition"]),
        )
        decision = {
            "decision_id": decision_id,
            "event_seq": event_seq,
            "offset": int(inflight["offset"]),
            "delivery_generation": delivery_generation,
            "expected_delivery_generation": int(inflight["expected_delivery_generation"]),
            "payload_digest": inflight["payload_digest"],
            "semantic_source_version": inflight["semantic_source_version"],
            "subscriber_id": inflight["subscriber_id"],
            "status": "acked",
            "started_at_ms": int(inflight["started_at_ms"]),
            "acked_at_ms": acked_at,
            "recompute_pass": int(target_partition["recompute_pass"]),
        }
        target_partition["decisions"].append(decision)
        target_partition["committed_offset"] = int(inflight["offset"])
        target_partition["delivery_generation"] = max(
            int(target_partition["delivery_generation"]),
            delivery_generation,
        )
        target_partition["next_available_ms"] = max(
            int(target_partition["next_available_ms"]),
            acked_at,
        )
        target_partition["inflight"] = None
        claim = working["delivery_claims"][claim_key]
        claim["status"] = "acked"
        claim["acked_at_ms"] = acked_at
        claim["decision_id"] = decision_id
        working["logical_clock_ms"] = max(int(working["logical_clock_ms"]), acked_at)
        refresh_conclusions(
            working,
            acked_at,
            {partition_id(target_partition["topic"], target_partition["partition"])},
        )
        _operation(
            working,
            "external_ack",
            acked_at,
            partition=partition_id(target_partition["topic"], target_partition["partition"]),
            offset=int(inflight["offset"]),
            event_seq=event_seq,
            delivery_generation=delivery_generation,
            decision_id=decision_id,
        )
        captured["decision_id"] = decision_id
        captured["acked_at_ms"] = acked_at
        return working

    result = store.commit(mutator)
    return {
        "ok": True,
        "idempotent": not result.changed,
        "event_seq": event_seq,
        "delivery_generation": delivery_generation,
        "decision_id": captured.get("decision_id"),
        "acked_at_ms": captured.get("acked_at_ms"),
        "ledger_version": result.version,
    }


def _key_partition_ids(
    state: dict[str, Any],
    message_key: str,
    topic: str | None,
) -> set[str]:
    result: set[str] = set()
    for pid, partition in state["partitions"].items():
        for event in partition["offsets"]:
            if event["partition_key"] == message_key:
                result.add(pid)
    if topic and state.get("topology") is not None:
        topic_definition = topology_index(state["topology"]).get(topic)
        if topic_definition is not None:
            number, _digest = route_partition(topic_definition, message_key)
            result.add(partition_id(topic, number))
    return result


def _dependency_edges(
    state: dict[str, Any],
    view: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    edges: dict[str, list[dict[str, Any]]] = {}
    for entry in view.get("entries", []):
        dependency = entry.get("dependency")
        if not dependency:
            continue
        pid = partition_id(dependency["topic"], dependency["partition"])
        partition = state["partitions"].get(pid)
        if partition is None:
            continue
        offset = int(dependency["upstream_offset"])
        if offset < 0 or offset >= len(partition["offsets"]):
            continue
        upstream = partition["offsets"][offset]
        edge = {
            "upstream_message_key": upstream["partition_key"],
            "upstream_event_seq": int(upstream["event_seq"]),
            "upstream_topic": dependency["topic"],
            "upstream_partition": int(dependency["partition"]),
            "upstream_offset": offset,
            "dependent_message_key": entry["message_key"],
        }
        edges.setdefault(upstream["partition_key"], []).append(edge)
    for source in edges:
        edges[source].sort(key=lambda item: item["dependent_message_key"])
    return edges


def _closure_report(
    state: dict[str, Any],
    *,
    patch: dict[str, Any],
    view: dict[str, Any],
) -> tuple[dict[str, Any], set[str]]:
    origins: list[dict[str, Any]] = []
    for update in patch["updates"]:
        origins.append({"type": "semantic_update", "message_key": update["message_key"]})
    for watermark in patch["upstream_watermarks"]:
        origins.append({"type": "upstream_watermark", "message_key": watermark["message_key"]})
    for tombstone in patch["tombstones"]:
        origins.append({"type": "tombstone", "message_key": tombstone["message_key"]})
    origins.sort(key=lambda item: (item["message_key"], item["type"]))

    edges = _dependency_edges(state, view)
    affected_keys: set[str] = set()
    chains: list[dict[str, Any]] = []
    referenced_upstream_partitions: set[str] = set()
    for origin in origins:
        key = origin["message_key"]
        affected_keys.add(key)
        chains.append(
            {
                "origin": origin,
                "path": [key],
                "edges": [],
                "reason": "direct-change",
            }
        )
        queue: list[tuple[str, list[str], list[dict[str, Any]]]] = [
            (key, [key], [])
        ]
        visited = {key}
        while queue:
            current, path, traversal_edges = queue.pop(0)
            for edge in edges.get(current, []):
                dependent = edge["dependent_message_key"]
                referenced_upstream_partitions.add(
                    partition_id(edge["upstream_topic"], edge["upstream_partition"])
                )
                if dependent in visited:
                    continue
                visited.add(dependent)
                affected_keys.add(dependent)
                next_edges = traversal_edges + [edge]
                chains.append(
                    {
                        "origin": origin,
                        "path": path + [dependent],
                        "edges": next_edges,
                        "reason": "upstream-watermark-reference",
                    }
                )
                queue.append((dependent, path + [dependent], next_edges))

    affected_partitions: set[str] = set()
    entries = {entry["message_key"]: entry for entry in view.get("entries", [])}
    for key in affected_keys:
        entry = entries.get(key)
        affected_partitions.update(
            _key_partition_ids(state, key, entry.get("topic") if entry else None)
        )
    all_partitions = set(state["partitions"])
    reused_partitions = all_partitions - affected_partitions
    report = {
        "patch_id": patch["patch_id"],
        "semantic_generation": patch["semantic_generation"],
        "logical_clock_ms": patch["logical_clock_ms"],
        "changed_keys": sorted(
            {
                item["message_key"]
                for item in origins
            }
        ),
        "affected_keys": sorted(affected_keys),
        "affected_partitions": sorted(affected_partitions),
        "reused_partitions": sorted(reused_partitions),
        "referenced_upstream_partitions": sorted(referenced_upstream_partitions),
        "chains": sorted(
            chains,
            key=lambda item: (
                item["origin"]["message_key"],
                item["origin"]["type"],
                item["path"],
            ),
        ),
    }
    return report, affected_partitions


def _validate_patch_against_state(
    state: dict[str, Any],
    patch: dict[str, Any],
) -> None:
    view = state.get("semantics", {}).get("view")
    if not view:
        raise InputError(
            REPLAY_NO_SEMANTICS,
            "A merged semantic view is required before applying a patch",
        )
    entries = _semantics_index(state)
    targets = (
        [update["message_key"] for update in patch["updates"]]
        + [watermark["message_key"] for watermark in patch["upstream_watermarks"]]
        + [tombstone["message_key"] for tombstone in patch["tombstones"]]
    )
    missing = sorted(set(targets) - set(entries))
    if missing:
        raise InputError(
            PATCH_TARGET_MISSING,
            "Patch references semantic entries that do not exist",
            diagnostics={"missing_message_keys": missing, "patch_id": patch["patch_id"]},
        )
    if int(patch["logical_clock_ms"]) < int(state["logical_clock_ms"]):
        raise ReplayError(
            LOGICAL_CLOCK_ROLLBACK,
            "Patch logical clock would move the ledger watermark backwards",
            diagnostics={
                "current_logical_clock_ms": state["logical_clock_ms"],
                "patch_logical_clock_ms": patch["logical_clock_ms"],
            },
        )
    topics = topology_index(state.get("topology") or {"topics": []})
    for watermark in patch["upstream_watermarks"]:
        topic = topics.get(watermark["topic"])
        if topic is None or watermark["partition"] >= topic["partition_count"]:
            raise InputError(
                PATCH_WATERMARK_INVALID,
                "Patch upstream watermark references a missing partition",
                diagnostics={"watermark": watermark},
            )
        pid = partition_id(watermark["topic"], watermark["partition"])
        partition = state["partitions"].get(pid)
        if partition is None or watermark["upstream_offset"] >= len(partition["offsets"]):
            raise InputError(
                PATCH_WATERMARK_INVALID,
                "Patch upstream watermark references a missing offset",
                diagnostics={
                    "watermark": watermark,
                    "available_offsets": len(partition["offsets"]) if partition else 0,
                },
            )
    existing_tombstone_ids = {
        entry["tombstone"]["tombstone_id"]
        for entry in entries.values()
        if entry.get("tombstone")
    }
    collisions = sorted(
        {
            tombstone["tombstone_id"]
            for tombstone in patch["tombstones"]
            if tombstone["tombstone_id"] in existing_tombstone_ids
        }
    )
    if collisions:
        raise InputError(
            PATCH_SCHEMA_INVALID,
            "Patch tombstone IDs already exist in the semantic view",
            diagnostics={"tombstone_ids": collisions},
        )
    for entry_update in patch["updates"]:
        if entry_update["semantic_generation"] > patch["semantic_generation"]:
            raise InputError(
                PATCH_SCHEMA_INVALID,
                "Patch entry generation exceeds the patch generation",
                diagnostics={
                    "message_key": entry_update["message_key"],
                    "entry_generation": entry_update["semantic_generation"],
                    "patch_generation": patch["semantic_generation"],
                },
            )


def _apply_patch_view(
    state: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    view = deepcopy(state["semantics"]["view"])
    entries = {entry["message_key"]: entry for entry in view["entries"]}
    patch_tombstones = {item["tombstone_id"]: item for item in patch["tombstones"]}
    for update in patch["updates"]:
        entry = entries[update["message_key"]]
        previous = {
            "source_version": entry["source_version"],
            "source_shard_id": entry.get("source_shard_id"),
            "logical_clock_ms": entry["logical_clock_ms"],
            "semantic_generation": entry["semantic_generation"],
            "effective_ms": entry["effective_ms"],
        }
        origin = {
            "source_version": "v2",
            "source_shard_id": f"patch:{patch['patch_id']}",
            "logical_clock_ms": patch["logical_clock_ms"],
            "semantic_generation": update["semantic_generation"],
            "effective_ms": update["absolute_effective_ms"],
            "source_priority": 2,
        }
        entry["delivery_alias"] = update["delivery_alias"]
        entry["semantic_generation"] = update["semantic_generation"]
        entry["effective_ms"] = update["absolute_effective_ms"]
        entry["source_version"] = "v2"
        entry["source_shard_id"] = f"patch:{patch['patch_id']}"
        entry["logical_clock_ms"] = patch["logical_clock_ms"]
        entry["original_time"] = {
            "type": "absolute_effective_ms",
            "value": update["absolute_effective_ms"],
            "logical_clock_ms": patch["logical_clock_ms"],
            "patch_id": patch["patch_id"],
        }
        entry["conversion"] = {
            "kind": "identity",
            "absolute_effective_ms": update["absolute_effective_ms"],
            "rounding": "none",
            "discarded_remainder_ms": 0,
        }
        entry["arbitration"] = {
            "message_key": update["message_key"],
            "decision": "semantic-patch",
            "basis": "explicit semantic patch",
            "patch_id": patch["patch_id"],
            "winner": origin,
            "overridden": [previous],
        }
        entry.setdefault("source_origins", []).append(
            {**origin, "patch_id": patch["patch_id"]}
        )
    for watermark in patch["upstream_watermarks"]:
        entry = entries[watermark["message_key"]]
        entry["dependency"] = {
            "topic": watermark["topic"],
            "partition": watermark["partition"],
            "upstream_offset": watermark["upstream_offset"],
        }
        entry.setdefault("patch_evidence", []).append(
            {
                "patch_id": patch["patch_id"],
                "kind": "upstream_watermark",
                "dependency": entry["dependency"],
            }
        )
    for tombstone in patch["tombstones"]:
        entry = entries[tombstone["message_key"]]
        entry["tombstone_ref"] = tombstone["tombstone_id"]
        entry["tombstone"] = dict(tombstone)
        entry.setdefault("patch_evidence", []).append(
            {
                "patch_id": patch["patch_id"],
                "kind": "tombstone",
                "tombstone_id": tombstone["tombstone_id"],
            }
        )
    view["entries"] = [entries[key] for key in sorted(entries)]
    view["decisions"] = [
        decision
        for decision in view.get("decisions", [])
        if decision["message_key"] not in {item["message_key"] for item in patch["updates"]}
    ]
    for update in patch["updates"]:
        entry = entries[update["message_key"]]
        view["decisions"].append(
            {
                "message_key": update["message_key"],
                "decision": "semantic-patch",
                "basis": "explicit semantic patch",
                "patch_id": patch["patch_id"],
                "winner": entry["arbitration"]["winner"],
                "overridden": entry["arbitration"]["overridden"],
            }
        )
    view["decisions"].sort(key=lambda item: item["message_key"])
    view["v2_source"]["current_generation"] = patch["semantic_generation"]
    view.setdefault("patch_applications", []).append(
        {
            "patch": deepcopy(patch),
            "report": None,
        }
    )
    return view


def apply_patch_state(
    state: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    if int(state["logical_clock_ms"]) > int(patch["logical_clock_ms"]):
        raise ReplayError(
            LOGICAL_CLOCK_ROLLBACK,
            "Patch logical clock is behind another committed operation",
            diagnostics={
                "current_logical_clock_ms": state["logical_clock_ms"],
                "patch_logical_clock_ms": patch["logical_clock_ms"],
            },
        )
    new_view = _apply_patch_view(state, patch)
    report, affected_partitions = _closure_report(
        state,
        patch=patch,
        view=new_view,
    )
    state["semantics"]["view"] = new_view
    state["semantics"]["v2_current_generation"] = patch["semantic_generation"]
    state["semantics"].setdefault("patch_history", []).append(
        {
            "patch_id": patch["patch_id"],
            "semantic_generation": patch["semantic_generation"],
            "logical_clock_ms": patch["logical_clock_ms"],
            "affected_partitions": report["affected_partitions"],
            "affected_keys": report["affected_keys"],
        }
    )
    state["logical_clock_ms"] = max(
        int(state["logical_clock_ms"]),
        int(patch["logical_clock_ms"]),
    )
    for pid in sorted(affected_partitions):
        partition = state["partitions"][pid]
        partition["recompute_pass"] = int(partition["recompute_pass"]) + 1
        affected_decision_records = []
        event_keys = {
            int(event["event_seq"]): event["partition_key"]
            for event in partition["offsets"]
        }
        for decision in partition["decisions"]:
            if event_keys.get(int(decision["event_seq"])) not in set(
                report["affected_keys"]
            ):
                continue
            affected_decision_records.append(
                {
                    "decision_id": decision["decision_id"],
                    "event_seq": int(decision["event_seq"]),
                    "offset": int(decision["offset"]),
                    "status": decision["status"],
                    "delivery_generation": int(decision["delivery_generation"]),
                }
            )
        partition["invalidations"].append(
            {
                "patch_id": patch["patch_id"],
                "clock_ms": patch["logical_clock_ms"],
                "affected_keys": sorted(
                    {
                        event["partition_key"]
                        for event in partition["offsets"]
                        if event["partition_key"] in set(report["affected_keys"])
                    }
                ),
                "chains": [
                    chain
                    for chain in report["chains"]
                    if chain["origin"]["message_key"] in report["affected_keys"]
                ],
                "affected_conclusions": affected_decision_records,
            }
        )
        report.setdefault("affected_conclusions", []).extend(
            [
                {
                    "partition_id": pid,
                    **record,
                }
                for record in affected_decision_records
            ]
        )
        inflight = partition.get("inflight")
        if inflight is not None and inflight["partition_key"] in set(report["affected_keys"]):
            claim = state["delivery_claims"].get(inflight["claim_key"])
            if claim is not None:
                claim["status"] = "invalidated"
                claim["invalidated_by_patch"] = patch["patch_id"]
            invalidated_decision = {
                "decision_id": inflight["decision_id"],
                "event_seq": int(inflight["event_seq"]),
                "offset": int(inflight["offset"]),
                "delivery_generation": int(inflight["delivery_generation"]),
                "status": "invalidated",
                "reason": "semantic-patch-closure",
                "patch_id": patch["patch_id"],
                "decided_at_ms": patch["logical_clock_ms"],
            }
            partition["decisions"].append(invalidated_decision)
            report.setdefault("invalidated_inflight", []).append(
                {
                    "partition_id": pid,
                    "event_seq": int(inflight["event_seq"]),
                    "offset": int(inflight["offset"]),
                    "delivery_generation": int(inflight["delivery_generation"]),
                    "claim_key": inflight["claim_key"],
                }
            )
            partition["inflight"] = None
        partition["conclusion"] = partition_conclusion(
            state,
            pid,
            int(patch["logical_clock_ms"]),
        )
    report["recomputed_partitions"] = sorted(affected_partitions)
    report["reused_conclusion_count"] = sum(
        len(state["partitions"][pid]["decisions"])
        for pid in sorted(set(state["partitions"]) - affected_partitions)
    )
    new_view["patch_applications"][-1]["report"] = report
    state["invalidation_history"].append(report)
    _operation(
        state,
        "semantic_patch",
        int(patch["logical_clock_ms"]),
        patch_id=patch["patch_id"],
        semantic_generation=patch["semantic_generation"],
        affected_partitions=report["affected_partitions"],
        reused_partitions=report["reused_partitions"],
        affected_key_count=len(report["affected_keys"]),
    )
    return report


def patch_ledger(
    store: LedgerStore,
    raw_patch: Any,
    *,
    expected_version: int | None = None,
) -> dict[str, Any]:
    state, metadata = store.read_state()
    view = state.get("semantics", {}).get("view")
    if not view:
        raise InputError(
            REPLAY_NO_SEMANTICS,
            "A merged semantic view is required before applying a patch",
        )
    current_generation = int(state["semantics"].get("v2_current_generation", 0))
    patch = validate_patch(raw_patch, current_v2_generation=current_generation)
    for application in view.get("patch_applications", []):
        if application.get("patch", {}).get("patch_id") != patch["patch_id"]:
            continue
        if canonical_json(application["patch"]) != canonical_json(patch):
            raise InputError(
                PATCH_SCHEMA_INVALID,
                "Patch ID was already used with different content",
                diagnostics={"patch_id": patch["patch_id"]},
            )
        report = application.get("report") or {}
        return {
            "ok": True,
            "schema": "seqledger-patch-result/v1",
            "idempotent": True,
            "patch_id": patch["patch_id"],
            "ledger_version": metadata.version,
            "invalidation": report,
        }
    _validate_patch_against_state(state, patch)
    captured: dict[str, Any] = {}

    def mutator(working: dict[str, Any]) -> dict[str, Any] | None:
        current_view = working.get("semantics", {}).get("view") or {}
        for application in current_view.get("patch_applications", []):
            if application.get("patch", {}).get("patch_id") != patch["patch_id"]:
                continue
            if canonical_json(application["patch"]) != canonical_json(patch):
                raise InputError(
                    PATCH_SCHEMA_INVALID,
                    "Patch ID was concurrently applied with different content",
                    diagnostics={"patch_id": patch["patch_id"]},
                )
            captured["report"] = application.get("report") or {}
            return None
        if int(working["semantics"]["v2_current_generation"]) > int(
            patch["semantic_generation"]
        ):
            raise InputError(
                PATCH_GENERATION_REGRESSION,
                "Patch generation would regress a concurrently updated semantic pointer",
                diagnostics={
                    "current_v2_generation": int(
                        working["semantics"]["v2_current_generation"]
                    ),
                    "patch_generation": int(patch["semantic_generation"]),
                },
            )
        _validate_patch_against_state(working, patch)
        captured["report"] = apply_patch_state(working, patch)
        return working

    result = store.commit(mutator, expected_version=expected_version)
    return {
        "ok": True,
        "schema": "seqledger-patch-result/v1",
        "idempotent": not result.changed,
        "patch_id": patch["patch_id"],
        "ledger_version": result.version,
        "invalidation": captured["report"],
    }


def status_payload(
    state: dict[str, Any],
    metadata: CommitResult,
) -> dict[str, Any]:
    view = state.get("semantics", {}).get("view")
    partitions: list[dict[str, Any]] = []
    for pid in sorted(state["partitions"]):
        partition = state["partitions"][pid]
        conclusion = partition.get("conclusion")
        if conclusion is None:
            conclusion = partition_conclusion(
                state,
                pid,
                int(state["logical_clock_ms"]),
            )
        inflight = partition.get("inflight")
        backlog_depth = max(
            0,
            len(partition["offsets"]) - (int(partition["committed_offset"]) + 1),
        )
        partitions.append(
            {
                "partition_id": pid,
                "topic": partition["topic"],
                "partition": int(partition["partition"]),
                "appended_offsets": len(partition["offsets"]),
                "committed_offset": int(partition["committed_offset"]),
                "committed_watermark": int(partition["committed_offset"]) + 1,
                "deliverable_offset": conclusion.get("deliverable_offset"),
                "backlog_depth": backlog_depth,
                "delivery_generation": int(partition["delivery_generation"]),
                "inflight": (
                    {
                        "event_seq": int(inflight["event_seq"]),
                        "offset": int(inflight["offset"]),
                        "delivery_generation": int(inflight["delivery_generation"]),
                        "ack_due_ms": int(inflight["ack_due_ms"]),
                    }
                    if inflight is not None
                    else None
                ),
                "dead_letter_count": len(partition["dead_letters"]),
                "blocked_reason": conclusion.get("blocked_reason"),
                "conclusion_clock_ms": conclusion.get("clock_ms"),
                "recompute_pass": int(partition["recompute_pass"]),
            }
        )
    return {
        "ok": True,
        "schema": "seqledger-status/v1",
        "ledger_version": metadata.version,
        "ledger_revision": metadata.revision,
        "ledger_sha256": metadata.sha256,
        "logical_clock_ms": int(state["logical_clock_ms"]),
        "format_migration": {
            "view_present": bool(view),
            "v2_current_generation": int(
                state.get("semantics", {}).get("v2_current_generation", 0)
            ),
            "entry_count": len(view.get("entries", [])) if view else 0,
            "v1_won_entries": (
                sum(1 for entry in view["entries"] if entry["source_version"] == "v1")
                if view
                else 0
            ),
            "v2_won_entries": (
                sum(1 for entry in view["entries"] if entry["source_version"] == "v2")
                if view
                else 0
            ),
            "conflict_entries": (
                int(view.get("decision_counts", {}).get("conflicts", 0)) if view else 0
            ),
            "patch_count": len(state.get("semantics", {}).get("patch_history", [])),
        },
        "partitions": partitions,
        "totals": {
            "partition_count": len(partitions),
            "backlog_depth": sum(partition["backlog_depth"] for partition in partitions),
            "inflight_count": sum(1 for partition in partitions if partition["inflight"]),
            "dead_letter_count": sum(
                partition["dead_letter_count"] for partition in partitions
            ),
            "delivered_decision_count": sum(
                len(partition["decisions"]) for partition in state["partitions"].values()
            ),
        },
    }
