from __future__ import annotations

import hashlib
import math
from typing import Any, Iterable, Mapping

from .routing import partition_id, stable_hash_int
from .validation import EventSpec, GatewayConfig, SubscriptionSpec, TopicSpec


TERMINAL_STATUSES = frozenset({"acked", "dead"})
UNRESOLVED_STATUSES = frozenset({"pending", "in_flight"})


def new_state(
    topics: Iterable[TopicSpec],
    subscriptions: Iterable[SubscriptionSpec],
) -> dict[str, Any]:
    subs_by_topic: dict[str, list[SubscriptionSpec]] = {}
    for subscription in subscriptions:
        subs_by_topic.setdefault(subscription.topic, []).append(subscription)

    partitions: dict[str, Any] = {}
    for topic in topics:
        for index in range(topic.partition_count):
            pid = partition_id(topic.name, index)
            partitions[pid] = {
                "topic": topic.name,
                "partition": index,
                "next_offset": 0,
                "clock_watermark": 0,
                "inflight": None,
                "lock_mode": "partition",
                "mode_decision": None,
                "subscribers": {
                    sub.subscriber_id: {
                        "topic": sub.topic,
                        "deliveries": {},
                    }
                    for sub in subs_by_topic.get(topic.name, [])
                },
                "log": {},
            }
    return {
        "schema_version": 1,
        "state_version": 0,
        "global": {
            "inflight_count": 0,
            "rounds_committed": 0,
            "last_round_id": None,
        },
        "partitions": partitions,
    }


def idempotency_key(subscriber_id: str, event_id: str) -> str:
    return hashlib.sha256(
        f"{subscriber_id}\x1f{event_id}".encode("utf-8")
    ).hexdigest()


def delivery_key(
    subscriber_id: str,
    event_id: str,
    generation: int,
) -> str:
    return hashlib.sha256(
        f"{subscriber_id}\x1f{event_id}\x1f{generation}".encode("utf-8")
    ).hexdigest()


def delivery_id(
    subscriber_id: str,
    partition: str,
    offset: int,
    generation: int,
) -> str:
    return f"{subscriber_id}:{partition}:{offset}:g{generation}"


def append_event(
    state: dict[str, Any],
    topic: TopicSpec,
    event: EventSpec,
    pid: str,
    subscriptions: Mapping[str, SubscriptionSpec],
) -> int:
    partition = state["partitions"][pid]
    offset = int(partition["next_offset"])
    partition["log"][str(offset)] = {
        "event_seq": event.event_seq,
        "event_id": event.event_id,
        "partition_key": event.partition_key,
        "payload_digest": event.payload_digest,
        "logical_clock_ms": event.logical_clock_ms,
        "expected_delivery_generation": event.expected_delivery_generation,
    }
    partition["next_offset"] = offset + 1
    partition["clock_watermark"] = max(
        int(partition["clock_watermark"]),
        event.logical_clock_ms,
    )
    for subscriber_id, sub_state in partition["subscribers"].items():
        subscription = subscriptions[subscriber_id]
        if subscription.topic != topic.name:
            continue
        sub_state["deliveries"][str(offset)] = {
            "event_id": event.event_id,
            "generation": 1,
            "expected_generation": event.expected_delivery_generation,
            "status": "pending",
            "attempt_count": 0,
            "next_attempt_clock_ms": event.logical_clock_ms,
            "delivery_id": None,
            "idempotency_key": idempotency_key(subscriber_id, event.event_id),
            "attempts": [],
            "dead_letter_reason": None,
            "last_outcome": None,
            "acked_clock_ms": None,
            "dead_clock_ms": None,
        }
    return offset


def first_unresolved_offset(partition: Mapping[str, Any], subscriber_id: str) -> int:
    sub_state = partition["subscribers"][subscriber_id]
    deliveries = sub_state["deliveries"]
    next_offset = int(partition["next_offset"])
    for offset in range(next_offset):
        entry = deliveries.get(str(offset))
        if entry is None or entry["status"] not in TERMINAL_STATUSES:
            return offset
    return next_offset


def committed_prefix(partition: Mapping[str, Any], subscriber_id: str) -> int:
    return first_unresolved_offset(partition, subscriber_id) - 1


def acked_prefix(partition: Mapping[str, Any], subscriber_id: str) -> int:
    deliveries = partition["subscribers"][subscriber_id]["deliveries"]
    highest = -1
    for offset in range(int(partition["next_offset"])):
        entry = deliveries.get(str(offset))
        if entry is None or entry["status"] != "acked":
            break
        highest = offset
    return highest


def backlog_depth(partition: Mapping[str, Any]) -> int:
    next_offset = int(partition["next_offset"])
    if not partition["subscribers"]:
        return next_offset
    slowest_committed = min(
        committed_prefix(partition, subscriber_id)
        for subscriber_id in partition["subscribers"]
    )
    return max(0, next_offset - slowest_committed - 1)


def summarize_delivery(
    partition: Mapping[str, Any],
    subscriber_id: str,
) -> dict[str, Any]:
    sub_state = partition["subscribers"][subscriber_id]
    deliveries = sub_state["deliveries"]
    next_offset = int(partition["next_offset"])
    committed = committed_prefix(partition, subscriber_id)
    deliverable = first_unresolved_offset(partition, subscriber_id)
    acked = acked_prefix(partition, subscriber_id)
    dead: list[int] = []
    unresolved: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []

    for offset in range(next_offset):
        entry = deliveries.get(str(offset))
        if entry is None:
            unresolved.append({"offset": offset, "reason": "missing_delivery_state"})
            if offset < deliverable:
                gaps.append({"offset": offset, "reason": "missing_delivery_state"})
            continue
        status = entry["status"]
        if status == "dead":
            dead.append(offset)
        if status in UNRESOLVED_STATUSES:
            if status == "in_flight":
                reason = "in_flight"
            elif int(entry.get("next_attempt_clock_ms") or 0) > int(
                partition.get("clock_watermark") or 0
            ):
                reason = "retry_backoff"
            else:
                reason = "pending"
            unresolved.append(
                {
                    "offset": offset,
                    "reason": reason,
                    "generation": entry.get("generation"),
                    "next_attempt_clock_ms": entry.get("next_attempt_clock_ms"),
                }
            )
        if acked < offset < deliverable:
            if status == "dead":
                reason = "dead_letter"
            elif entry is None:
                reason = "missing_delivery_state"
            else:
                reason = f"unresolved_{status}"
            gaps.append(
                {
                    "offset": offset,
                    "reason": reason,
                    "generation": entry.get("generation") if entry else None,
                }
            )

    return {
        "subscriber_id": subscriber_id,
        "offset_watermark": next_offset,
        "deliverable_offset": deliverable,
        "committed_offset": committed,
        "acked_offset": acked,
        "dead_letter_offsets": dead,
        "unresolved_offsets": unresolved,
        "gaps": gaps,
    }


def partition_summary(partition: Mapping[str, Any]) -> dict[str, Any]:
    subscriber_ids = sorted(partition["subscribers"])
    per_subscriber = {
        subscriber_id: summarize_delivery(partition, subscriber_id)
        for subscriber_id in subscriber_ids
    }
    dead_offsets = sorted(
        {
            offset
            for summary in per_subscriber.values()
            for offset in summary["dead_letter_offsets"]
        }
    )
    if per_subscriber:
        deliverable = min(
            summary["deliverable_offset"] for summary in per_subscriber.values()
        )
        committed = min(
            summary["committed_offset"] for summary in per_subscriber.values()
        )
        acked = min(summary["acked_offset"] for summary in per_subscriber.values())
        gaps: list[dict[str, Any]] = []
        seen_gaps: set[tuple[int, str]] = set()
        for subscriber_id, summary in per_subscriber.items():
            for gap in summary["gaps"]:
                key = (int(gap["offset"]), str(gap["reason"]))
                if key in seen_gaps:
                    continue
                seen_gaps.add(key)
                gaps.append({**gap, "subscriber_id": subscriber_id})
    else:
        deliverable = int(partition["next_offset"])
        committed = int(partition["next_offset"]) - 1
        acked = committed
        gaps = []

    inflight = []
    if partition.get("inflight"):
        inflight = [dict(partition["inflight"])]

    return {
        "partition_id": f"{partition['topic']}/{partition['partition']}",
        "topic": partition["topic"],
        "partition": int(partition["partition"]),
        "offset_watermark": int(partition["next_offset"]),
        "offset_high": int(partition["next_offset"]) - 1,
        "committed_offset": committed,
        "acked_offset": acked,
        "deliverable_offset": deliverable,
        "dead_letter_offsets": dead_offsets,
        "dead_letter_count": len(dead_offsets),
        "clock_watermark": int(partition["clock_watermark"]),
        "inflight": inflight,
        "inflight_count": len(inflight),
        "backlog_depth": backlog_depth(partition),
        "lock_mode": partition.get("lock_mode", "partition"),
        "mode_decision": partition.get("mode_decision"),
        "subscribers": per_subscriber,
        "gaps": sorted(gaps, key=lambda item: (item["offset"], item["subscriber_id"])),
    }


def delivery_cost_ms(config: GatewayConfig, subscription: SubscriptionSpec) -> int:
    per_second = max(0.000001, float(subscription.consume_rate_per_sec))
    return max(1, int(math.ceil(1000.0 / per_second)))


def deterministic_jitter_ms(
    subscriber_id: str,
    event_id: str,
    generation: int,
    config: GatewayConfig,
) -> int:
    span = config.retry_backoff_base_ms * config.fixed_scale
    bucket = stable_hash_int(
        f"backoff\x1f{subscriber_id}\x1f{event_id}\x1f{generation}",
        config.deterministic_seed,
    ) % (span + 1)
    return int(math.ceil(bucket / config.fixed_scale))


def backoff_ms(
    subscriber_id: str,
    event_id: str,
    generation: int,
    config: GatewayConfig,
) -> int:
    exponent = min(max(0, generation - 1), 30)
    raw = config.retry_backoff_base_ms * (1 << exponent)
    jitter = deterministic_jitter_ms(
        subscriber_id,
        event_id,
        generation,
        config,
    )
    return min(config.retry_backoff_max_ms, max(config.retry_backoff_base_ms, raw + jitter))


def simulate_delivery_outcome(
    subscription: SubscriptionSpec,
    entry: Mapping[str, Any],
    generation: int,
    config: GatewayConfig,
) -> tuple[bool, str | None]:
    expected = int(entry.get("expected_generation") or 1)
    if generation < expected:
        return False, "expected_retry_until_generation"
    bucket = stable_hash_int(
        (
            f"failure\x1f{subscription.subscriber_id}\x1f"
            f"{entry['event_id']}\x1f{generation}"
        ),
        config.deterministic_seed,
    ) % 1_000_000
    threshold = int(round(subscription.failure_rate * 1_000_000))
    if bucket < threshold:
        return False, "declared_failure_rate"
    return True, None


def max_generation(subscription: SubscriptionSpec) -> int:
    return int(subscription.retry_budget) + 1


def logical_state_fingerprint(state: Mapping[str, Any]) -> str:
    """Stable fingerprint of business state, excluding pointer bookkeeping."""

    partitions: dict[str, Any] = {}
    for pid, partition in state["partitions"].items():
        normalized = {
            key: value
            for key, value in partition.items()
            if key not in {"lock_mode", "mode_decision", "inflight"}
        }
        partitions[pid] = normalized
    payload = {
        "global": {
            "rounds_committed": state["global"]["rounds_committed"],
            "last_round_id": state["global"]["last_round_id"],
        },
        "partitions": partitions,
    }
    from .storage import canonical_json

    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
