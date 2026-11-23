from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import E_BACKLOG_LIMIT, E_CLOCK_REGRESSION, GatewayError, bad_sample
from .routing import (
    canonical_key_rule,
    normalize_partition_key,
    partition_id,
    route_partition,
)


@dataclass(frozen=True)
class TopicSpec:
    name: str
    partition_count: int
    partition_key_rule: str
    backlog_limit: int
    out_of_order_tolerance_window: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EventSpec:
    event_seq: int
    event_id: str
    topic: str
    partition_key: str
    payload_digest: str
    logical_clock_ms: int
    expected_delivery_generation: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SubscriptionSpec:
    subscriber_id: str
    topic: str
    pull_batch_size: int
    consume_rate_per_sec: float
    failure_rate: float
    retry_budget: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GatewayConfig:
    partition_lock_granularity: str
    catchup_logical_time_budget_ms: int
    global_concurrency_limit: int
    retry_backoff_base_ms: int
    retry_backoff_max_ms: int
    fixed_scale: int
    clock_tolerance_ms: int
    deterministic_seed: int = 0
    adaptive_lock_enabled: bool = True
    lock_switch_backlog_threshold: int = 8
    lock_switch_inflight_threshold: int = 2
    segment_size: int = 4
    lock_cost_ms: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Sample:
    topics: tuple[TopicSpec, ...]
    events: tuple[EventSpec, ...]
    subscriptions: tuple[SubscriptionSpec, ...]
    config: GatewayConfig
    sample_hash: str
    metadata_hash: str

    @property
    def topic_map(self) -> dict[str, TopicSpec]:
        return {topic.name: topic for topic in self.topics}

    @property
    def subscription_map(self) -> dict[str, SubscriptionSpec]:
        return {sub.subscriber_id: sub for sub in self.subscriptions}

    def event_routes(self) -> dict[int, str]:
        topics = self.topic_map
        routes: dict[int, str] = {}
        for event in self.events:
            topic = topics[event.topic]
            routes[event.event_seq] = partition_id(
                topic.name,
                route_partition(
                    topic.name,
                    topic.partition_count,
                    event.partition_key,
                    topic.partition_key_rule,
                ),
            )
        return routes


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise bad_sample(f"{label} must be a JSON object", field=label)
    return value


def _list_root(document: Any, preferred_key: str, label: str) -> list[Any]:
    if isinstance(document, list):
        return document
    obj = _mapping(document, label)
    value = obj.get(preferred_key)
    if value is None:
        for alias in (preferred_key + "s", "items", "data"):
            if alias in obj:
                value = obj[alias]
                break
    if not isinstance(value, list):
        raise bad_sample(
            f"{label} document must contain a JSON array",
            field=preferred_key,
        )
    return value


def _first(mapping: Mapping[str, Any], aliases: Iterable[str]) -> Any:
    for alias in aliases:
        if alias in mapping:
            return mapping[alias]
    return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _positive_int(value: Any, field: str, **context: Any) -> int:
    if not _is_int(value) or value <= 0:
        raise bad_sample(
            f"{field} must be a positive integer",
            field=field,
            value=value,
            **context,
        )
    return value


def _nonnegative_int(value: Any, field: str, **context: Any) -> int:
    if not _is_int(value) or value < 0:
        raise bad_sample(
            f"{field} must be a non-negative integer",
            field=field,
            value=value,
            **context,
        )
    return value


def _nonempty_string(value: Any, field: str, **context: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise bad_sample(
            f"{field} must be a non-empty string",
            field=field,
            value=value,
            **context,
        )
    result = value.strip()
    if "\x00" in result:
        raise bad_sample(
            f"{field} contains an unsupported character",
            field=field,
            value=value,
            **context,
        )
    return result


def parse_topics(document: Any) -> tuple[TopicSpec, ...]:
    raw_topics = _list_root(document, "topic", "topics")
    if not raw_topics:
        raise bad_sample("topics sample must not be empty", field="topics")

    topics: list[TopicSpec] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_topics):
        obj = _mapping(raw, f"topics[{index}]")
        name = _nonempty_string(
            _first(obj, ("name", "topic")),
            "name",
            index=index,
        )
        if name in seen:
            raise bad_sample(
                "topic name must be unique",
                field="name",
                index=index,
                topic=name,
            )
        seen.add(name)

        raw_count = _first(obj, ("partition_count", "partitions", "num_partitions"))
        if isinstance(raw_count, list):
            raw_count = len(raw_count)
        partition_count = _positive_int(
            raw_count,
            "partition_count",
            index=index,
            topic=name,
        )
        raw_rule = _first(
            obj,
            ("partition_key_rule", "key_rule", "routing_rule"),
        )
        if raw_rule is None:
            raw_rule = "sha256_mod_v1"
        partition_key_rule = canonical_key_rule(raw_rule)
        backlog_limit = _positive_int(
            _first(
                obj,
                (
                    "backlog_limit",
                    "partition_backlog_limit",
                    "max_backlog",
                    "max_partition_backlog",
                ),
            ),
            "backlog_limit",
            index=index,
            topic=name,
        )
        window = _nonnegative_int(
            _first(
                obj,
                (
                    "out_of_order_tolerance_window",
                    "out_of_order_window",
                    "tolerance_window",
                    "out_of_order_tolerance_window_events",
                    "out_of_order_events",
                ),
            ),
            "out_of_order_tolerance_window",
            index=index,
            topic=name,
        )
        topics.append(
            TopicSpec(
                name=name,
                partition_count=partition_count,
                partition_key_rule=partition_key_rule,
                backlog_limit=backlog_limit,
                out_of_order_tolerance_window=window,
            )
        )
    return tuple(topics)


def parse_events(
    document: Any,
    topics: tuple[TopicSpec, ...] | list[TopicSpec],
) -> tuple[EventSpec, ...]:
    raw_events = _list_root(document, "event", "events")
    topic_names = {topic.name for topic in topics}
    only_topic = next(iter(topic_names)) if len(topic_names) == 1 else None
    default_topic = None
    if isinstance(document, Mapping):
        raw_default = _first(document, ("default_topic", "topic"))
        if raw_default is not None:
            default_topic = _nonempty_string(
                raw_default,
                "default_topic",
            )
            if default_topic not in topic_names:
                raise bad_sample(
                    "events document references an unknown default topic",
                    field="default_topic",
                    topic=default_topic,
                )
    events: list[EventSpec] = []
    seen_seq: set[int] = set()
    seen_ids: set[str] = set()

    for index, raw in enumerate(raw_events):
        obj = _mapping(raw, f"events[{index}]")
        event_seq = _nonnegative_int(
            _first(obj, ("event_seq", "sequence", "seq")),
            "event_seq",
            index=index,
        )
        if event_seq in seen_seq:
            raise bad_sample(
                "event sequence must be unique",
                field="event_seq",
                index=index,
                event_seq=event_seq,
            )
        seen_seq.add(event_seq)

        event_id_raw = _first(obj, ("event_id", "id"))
        if event_id_raw is None:
            event_id = f"evt-{event_seq:08d}"
        else:
            event_id = _nonempty_string(event_id_raw, "event_id", index=index)
        if event_id in seen_ids:
            raise bad_sample(
                "event id must be unique",
                field="event_id",
                index=index,
                event_id=event_id,
            )
        seen_ids.add(event_id)

        topic_raw = obj.get("topic")
        if topic_raw is None:
            if default_topic is not None:
                topic = default_topic
            elif only_topic is not None:
                topic = only_topic
            else:
                raise bad_sample(
                    "event topic is required when multiple topics exist",
                    field="topic",
                    index=index,
                )
        else:
            topic = _nonempty_string(topic_raw, "topic", index=index)
        if topic not in topic_names:
            raise bad_sample(
                "event references an unknown topic",
                field="topic",
                index=index,
                topic=topic,
            )

        partition_key = normalize_partition_key(
            _first(obj, ("partition_key", "key"))
        )
        payload_digest = _nonempty_string(
            _first(obj, ("payload_digest", "digest", "payload_hash")),
            "payload_digest",
            index=index,
        )
        clock_ms = _nonnegative_int(
            _first(
                obj,
                (
                    "injected_clock_ms",
                    "logical_clock_ms",
                    "clock_ms",
                    "timestamp_ms",
                    "injected_logical_clock_ms",
                ),
            ),
            "injected_clock_ms",
            index=index,
        )
        expected_generation = _positive_int(
            _first(
                obj,
                (
                    "expected_delivery_generation",
                    "expected_generation",
                    "expected_generation_no",
                    "expected_delivery_attempt",
                ),
            ),
            "expected_delivery_generation",
            index=index,
        )
        events.append(
            EventSpec(
                event_seq=event_seq,
                event_id=event_id,
                topic=topic,
                partition_key=partition_key,
                payload_digest=payload_digest,
                logical_clock_ms=clock_ms,
                expected_delivery_generation=expected_generation,
            )
        )
    events.sort(key=lambda event: event.event_seq)
    return tuple(events)


def parse_subscriptions(
    document: Any,
    topics: tuple[TopicSpec, ...] | list[TopicSpec],
) -> tuple[SubscriptionSpec, ...]:
    raw_subscriptions = _list_root(document, "subscription", "subscriptions")
    topic_names = {topic.name for topic in topics}
    seen: set[str] = set()
    subscriptions: list[SubscriptionSpec] = []

    for index, raw in enumerate(raw_subscriptions):
        obj = _mapping(raw, f"subscriptions[{index}]")
        subscriber_id = _nonempty_string(
            _first(obj, ("subscriber_id", "id", "subscriber")),
            "subscriber_id",
            index=index,
        )
        if subscriber_id in seen:
            raise bad_sample(
                "subscriber id must be unique",
                field="subscriber_id",
                index=index,
                subscriber_id=subscriber_id,
            )
        seen.add(subscriber_id)

        topic = _nonempty_string(obj.get("topic"), "topic", index=index)
        if topic not in topic_names:
            raise bad_sample(
                "subscription references an unknown topic",
                field="topic",
                index=index,
                subscriber_id=subscriber_id,
                topic=topic,
            )
        pull_batch_size = _positive_int(
            _first(
                obj,
                ("pull_batch_size", "batch_size", "max_pull", "pull_size"),
            ),
            "pull_batch_size",
            index=index,
            subscriber_id=subscriber_id,
        )
        consume_rate_raw = _first(
            obj,
            ("consume_rate_per_sec", "consume_rate", "rate_per_sec"),
        )
        if not _is_number(consume_rate_raw) or float(consume_rate_raw) <= 0:
            raise bad_sample(
                "consume_rate_per_sec must be a positive number",
                field="consume_rate_per_sec",
                index=index,
                subscriber_id=subscriber_id,
                value=consume_rate_raw,
            )
        failure_rate_raw = _first(
            obj,
            (
                "failure_rate",
                "processing_failure_rate",
                "failure_rate_declared",
            ),
        )
        if (
            not _is_number(failure_rate_raw)
            or float(failure_rate_raw) < 0
            or float(failure_rate_raw) > 1
        ):
            raise bad_sample(
                "failure_rate must be between 0 and 1",
                field="failure_rate",
                index=index,
                subscriber_id=subscriber_id,
                value=failure_rate_raw,
            )
        retry_budget = _nonnegative_int(
            _first(obj, ("retry_budget", "max_retries")),
            "retry_budget",
            index=index,
            subscriber_id=subscriber_id,
        )
        subscriptions.append(
            SubscriptionSpec(
                subscriber_id=subscriber_id,
                topic=topic,
                pull_batch_size=pull_batch_size,
                consume_rate_per_sec=float(consume_rate_raw),
                failure_rate=float(failure_rate_raw),
                retry_budget=retry_budget,
            )
        )
    subscriptions.sort(key=lambda sub: sub.subscriber_id)
    return tuple(subscriptions)


def parse_config(document: Any) -> GatewayConfig:
    root = _mapping(document, "config")
    if isinstance(root.get("gateway"), Mapping):
        obj = _mapping(root["gateway"], "config.gateway")
    elif isinstance(root.get("config"), Mapping):
        obj = _mapping(root["config"], "config.config")
    else:
        obj = root

    raw_mode = _first(
        obj,
        ("partition_lock_granularity", "lock_granularity"),
    )
    if not isinstance(raw_mode, str) or raw_mode not in {"partition", "segment"}:
        raise bad_sample(
            "partition_lock_granularity must be 'partition' or 'segment'",
            field="partition_lock_granularity",
            value=raw_mode,
        )
    budget = _positive_int(
        _first(
            obj,
            (
                "catchup_logical_time_budget_ms",
                "round_budget_ms",
                "logical_time_budget_ms",
                "round_logical_time_budget_ms",
            ),
        ),
        "catchup_logical_time_budget_ms",
    )
    global_limit = _positive_int(
        _first(
            obj,
            (
                "global_concurrency_limit",
                "global_inflight_limit",
                "max_concurrent_deliveries",
                "global_delivery_concurrency",
            ),
        ),
        "global_concurrency_limit",
    )
    base = _positive_int(
        _first(
            obj,
            ("retry_backoff_base_ms", "backoff_base_ms", "retry_base_ms"),
        ),
        "retry_backoff_base_ms",
    )
    cap = _positive_int(
        _first(
            obj,
            ("retry_backoff_max_ms", "backoff_max_ms", "retry_cap_ms"),
        ),
        "retry_backoff_max_ms",
    )
    if cap < base:
        raise bad_sample(
            "retry backoff maximum must not be smaller than its base",
            field="retry_backoff_max_ms",
            base=base,
            maximum=cap,
        )
    fixed_scale = _positive_int(
        _first(obj, ("fixed_scale", "fixed_point_scale", "scale")),
        "fixed_scale",
    )
    tolerance = _positive_int(
        _first(
            obj,
            (
                "clock_tolerance_ms",
                "clock_regression_tolerance_ms",
                "tolerance_ms",
            ),
        ),
        "clock_tolerance_ms",
    )

    seed = obj.get("deterministic_seed", 0)
    seed = _nonnegative_int(seed, "deterministic_seed")
    adaptive = obj.get("adaptive_lock_enabled", True)
    if not isinstance(adaptive, bool):
        raise bad_sample(
            "adaptive_lock_enabled must be a boolean",
            field="adaptive_lock_enabled",
            value=adaptive,
        )
    backlog_threshold = _positive_int(
        obj.get("lock_switch_backlog_threshold", 8),
        "lock_switch_backlog_threshold",
    )
    inflight_threshold = _positive_int(
        obj.get("lock_switch_inflight_threshold", 2),
        "lock_switch_inflight_threshold",
    )
    segment_size = _positive_int(
        obj.get("segment_size", 4),
        "segment_size",
    )
    lock_cost = _nonnegative_int(
        obj.get("lock_cost_ms", 1),
        "lock_cost_ms",
    )
    return GatewayConfig(
        partition_lock_granularity=raw_mode,
        catchup_logical_time_budget_ms=budget,
        global_concurrency_limit=global_limit,
        retry_backoff_base_ms=base,
        retry_backoff_max_ms=cap,
        fixed_scale=fixed_scale,
        clock_tolerance_ms=tolerance,
        deterministic_seed=seed,
        adaptive_lock_enabled=adaptive,
        lock_switch_backlog_threshold=backlog_threshold,
        lock_switch_inflight_threshold=inflight_threshold,
        segment_size=segment_size,
        lock_cost_ms=lock_cost,
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _preflight(
    topics: tuple[TopicSpec, ...],
    events: tuple[EventSpec, ...],
    config: GatewayConfig,
) -> None:
    topic_map = {topic.name: topic for topic in topics}
    clocks: dict[str, int] = {}
    backlog: dict[str, int] = {}

    for event in events:
        topic = topic_map[event.topic]
        pid = partition_id(
            topic.name,
            route_partition(
                topic.name,
                topic.partition_count,
                event.partition_key,
                topic.partition_key_rule,
            ),
        )
        previous_clock = clocks.get(pid, 0)
        if event.logical_clock_ms < previous_clock - config.clock_tolerance_ms:
            raise GatewayError(
                E_CLOCK_REGRESSION,
                "injected logical clock regressed beyond tolerance",
                {
                    "partition_id": pid,
                    "event_seq": event.event_seq,
                    "previous_watermark": previous_clock,
                    "event_clock_ms": event.logical_clock_ms,
                    "tolerance_ms": config.clock_tolerance_ms,
                },
            )
        clocks[pid] = max(previous_clock, event.logical_clock_ms)
        backlog[pid] = backlog.get(pid, 0) + 1
        if backlog[pid] > topic.backlog_limit:
            raise GatewayError(
                E_BACKLOG_LIMIT,
                "partition backlog limit would be exceeded",
                {
                    "partition_id": pid,
                    "backlog_limit": topic.backlog_limit,
                    "attempted_backlog": backlog[pid],
                    "event_seq": event.event_seq,
                },
            )


def validate_sample(
    topics_document: Any,
    events_document: Any,
    subscriptions_document: Any,
    config_document: Any,
) -> Sample:
    topics = parse_topics(topics_document)
    events = parse_events(events_document, topics)
    subscriptions = parse_subscriptions(subscriptions_document, topics)
    config = parse_config(config_document)
    _preflight(topics, events, config)

    topics_payload = [topic.to_dict() for topic in topics]
    subscriptions_payload = [sub.to_dict() for sub in subscriptions]
    config_payload = config.to_dict()
    metadata_hash = hashlib.sha256(
        _canonical_json(
            {
                "topics": topics_payload,
                "subscriptions": subscriptions_payload,
                "config": config_payload,
            }
        ).encode("utf-8")
    ).hexdigest()
    sample_hash = hashlib.sha256(
        _canonical_json(
            {
                "metadata_hash": metadata_hash,
                "events": [event.to_dict() for event in events],
            }
        ).encode("utf-8")
    ).hexdigest()
    return Sample(
        topics=topics,
        events=events,
        subscriptions=subscriptions,
        config=config,
        sample_hash=sample_hash,
        metadata_hash=metadata_hash,
    )


def load_json_file(path: str | Path, label: str) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise bad_sample(
            f"cannot parse {label} JSON",
            path=str(path),
            reason=str(exc),
        ) from exc
