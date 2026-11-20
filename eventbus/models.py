from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import fail
from .expr import compile_expression
from .util import (
    canonical_json,
    digest_json,
    load_json,
    normalize_partition_key,
    require_nonempty_str,
    require_plain_int,
    sha256_text,
)


@dataclass(frozen=True)
class Channel:
    name: str
    topic: str
    partitions: int
    retention_bytes: int
    max_batch_records: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "topic": self.topic,
            "partitions": self.partitions,
            "retention_bytes": self.retention_bytes,
            "max_batch_records": self.max_batch_records,
        }

    @property
    def digest(self) -> str:
        return digest_json(self.to_dict())


@dataclass(frozen=True)
class BridgeRule:
    id: str
    upstream: str
    downstream: str
    filter_expr: str
    transform_expr: str
    depends_upstream_offset: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "upstream": self.upstream,
            "downstream": self.downstream,
            "filter": self.filter_expr,
            "transform": self.transform_expr,
            "depends_upstream_offset": self.depends_upstream_offset,
        }

    @property
    def digest(self) -> str:
        return digest_json(self.to_dict())


@dataclass(frozen=True)
class Event:
    seq: int
    topic: str
    partition_key: Any
    payload: dict[str, Any]
    clock_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "topic": self.topic,
            "partition_key": self.partition_key,
            "payload": self.payload,
            "clock_ms": self.clock_ms,
        }

    @property
    def source_digest(self) -> str:
        return digest_json(self.to_dict())

    @property
    def normalized_key(self) -> str:
        return normalize_partition_key(self.partition_key)


@dataclass(frozen=True)
class Subscription:
    consumer_id: str
    channel: str
    start_offset: int
    budget: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "consumer_id": self.consumer_id,
            "channel": self.channel,
            "start_offset": self.start_offset,
            "budget": self.budget,
        }


@dataclass(frozen=True)
class SampleBundle:
    channels: tuple[Channel, ...]
    rules: tuple[BridgeRule, ...]
    events: tuple[Event, ...]
    subscriptions: tuple[Subscription, ...]
    projected_ends: dict[str, int]
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "channels": [channel.to_dict() for channel in self.channels],
            "rules": [rule.to_dict() for rule in self.rules],
            "events": [event.to_dict() for event in self.events],
            "subscriptions": [
                subscription.to_dict() for subscription in self.subscriptions
            ],
            "projected_ends": dict(sorted(self.projected_ends.items())),
            "digest": self.digest,
        }


def _extract_list(root: Any, key: str, code: str) -> list[Any]:
    if isinstance(root, list):
        return root
    if isinstance(root, dict) and key in root and isinstance(root[key], list):
        return root[key]
    fail(code, f"expected a JSON array or an object containing '{key}'")


def _parse_channels(catalog: Any) -> tuple[list[Channel], dict[str, str]]:
    if not isinstance(catalog, dict):
        fail("E_BAD_CHANNEL", "catalog must be a JSON object")
    topics_raw = catalog.get("topics")
    if not isinstance(topics_raw, list):
        fail("E_BAD_CHANNEL", "catalog.topics must be an array")

    topic_names: set[str] = set()
    topic_to_channel: dict[str, str] = {}
    inline_channels: dict[str, dict[str, Any]] = {}
    channels_raw = catalog.get("channels")
    if channels_raw is not None and not isinstance(channels_raw, list):
        fail("E_BAD_CHANNEL", "catalog.channels must be an array")

    for raw_topic in topics_raw:
        if not isinstance(raw_topic, dict):
            fail("E_BAD_TOPIC", "each topic declaration must be an object")
        topic_name = require_nonempty_str(raw_topic.get("name"), "E_BAD_TOPIC", "topic")
        channel_name = require_nonempty_str(
            raw_topic.get("channel"), "E_BAD_CHANNEL", "channel"
        )
        if topic_name in topic_names:
            fail("E_DUPLICATE_TOPIC", "topic name is not unique", topic=topic_name)
        topic_names.add(topic_name)
        topic_to_channel[topic_name] = channel_name
        if channels_raw is None:
            inline_channels[channel_name] = {
                "name": channel_name,
                "partitions": raw_topic.get("partitions"),
                "retention_bytes": raw_topic.get("retention_bytes"),
                "max_batch_records": raw_topic.get("max_batch_records"),
            }

    if channels_raw is None:
        channels_raw = [
            inline_channels[name] for name in sorted(inline_channels)
        ]
    elif not channels_raw:
        fail("E_BAD_CHANNEL", "catalog.channels must not be empty")

    channel_names: set[str] = set()
    channels: list[Channel] = []
    for raw_channel in channels_raw:
        if not isinstance(raw_channel, dict):
            fail("E_BAD_CHANNEL", "each channel declaration must be an object")
        name = require_nonempty_str(
            raw_channel.get("name"), "E_BAD_CHANNEL", "channel.name"
        )
        if name in channel_names:
            fail("E_DUPLICATE_CHANNEL", "channel name is not unique", channel=name)
        channel_names.add(name)
        partitions = require_plain_int(
            raw_channel.get("partitions"), "E_BAD_PARTITIONS", "partitions"
        )
        if partitions <= 0:
            fail(
                "E_BAD_PARTITIONS",
                "partitions must be a positive integer",
                channel=name,
            )
        retention = require_plain_int(
            raw_channel.get("retention_bytes"),
            "E_BAD_RETENTION",
            "retention_bytes",
        )
        if retention <= 0:
            fail(
                "E_BAD_RETENTION",
                "retention_bytes must be a positive integer",
                channel=name,
            )
        batch_size = require_plain_int(
            raw_channel.get("max_batch_records"),
            "E_BAD_BATCH_SIZE",
            "max_batch_records",
        )
        if batch_size <= 0:
            fail(
                "E_BAD_BATCH_SIZE",
                "max_batch_records must be a positive integer",
                channel=name,
            )
        topics = [topic for topic, channel in topic_to_channel.items() if channel == name]
        if len(topics) != 1:
            fail(
                "E_BAD_CHANNEL",
                "each channel must be mapped from exactly one topic",
                channel=name,
                topics=sorted(topics),
            )
        channels.append(
            Channel(
                name=name,
                topic=topics[0],
                partitions=partitions,
                retention_bytes=retention,
                max_batch_records=batch_size,
            )
        )

    unknown = sorted(set(topic_to_channel.values()) - channel_names)
    if unknown:
        fail(
            "E_BAD_CHANNEL",
            "topic refers to an undeclared channel",
            channels=unknown,
        )
    channels.sort(key=lambda channel: channel.name)
    return channels, topic_to_channel


def _parse_rules(root: Any, channel_names: set[str]) -> list[BridgeRule]:
    rules_raw = _extract_list(root, "rules", "E_BAD_RULE")
    rules: list[BridgeRule] = []
    ids: set[str] = set()
    for raw in rules_raw:
        if not isinstance(raw, dict):
            fail("E_BAD_RULE", "each bridge rule must be an object")
        rule_id = require_nonempty_str(raw.get("id"), "E_BAD_RULE", "rule.id")
        if rule_id in ids:
            fail("E_DUPLICATE_RULE", "bridge rule id is not unique", rule=rule_id)
        ids.add(rule_id)
        upstream = require_nonempty_str(
            raw.get("upstream"), "E_BAD_RULE", "rule.upstream"
        )
        downstream = require_nonempty_str(
            raw.get("downstream"), "E_BAD_RULE", "rule.downstream"
        )
        if upstream not in channel_names or downstream not in channel_names:
            fail(
                "E_BAD_CHANNEL",
                "bridge rule refers to an unknown channel",
                rule=rule_id,
                upstream=upstream,
                downstream=downstream,
            )
        depends = require_plain_int(
            raw.get("depends_upstream_offset"),
            "E_BAD_RULE",
            "depends_upstream_offset",
        )
        if depends < 0:
            fail(
                "E_BAD_RULE",
                "depends_upstream_offset must be non-negative",
                rule=rule_id,
            )
        filter_expr = require_nonempty_str(raw.get("filter"), "E_BAD_EXPR", "filter")
        transform_expr = require_nonempty_str(
            raw.get("transform"), "E_BAD_EXPR", "transform"
        )
        compile_expression(filter_expr, f"rules.{rule_id}.filter")
        compile_expression(transform_expr, f"rules.{rule_id}.transform")
        rules.append(
            BridgeRule(
                id=rule_id,
                upstream=upstream,
                downstream=downstream,
                filter_expr=filter_expr,
                transform_expr=transform_expr,
                depends_upstream_offset=depends,
            )
        )
    rules.sort(key=lambda rule: rule.id)
    check_bridge_cycles(rules)
    return rules


def check_bridge_cycles(rules: list[BridgeRule]) -> None:
    graph: dict[str, list[str]] = {}
    for rule in sorted(rules, key=lambda item: item.id):
        graph.setdefault(rule.upstream, []).append(rule.downstream)
    for targets in graph.values():
        targets.sort()

    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for target in graph.get(node, []):
            if state.get(target, 0) == 0:
                visit(target)
            elif state.get(target) == 1:
                index = stack.index(target)
                cycle = stack[index:] + [target]
                fail(
                    "E_BRIDGE_CYCLE",
                    "bridge graph contains a cycle",
                    cycle=cycle,
                )
        stack.pop()
        state[node] = 2

    for node in sorted(graph):
        if state.get(node, 0) == 0:
            visit(node)


def bridge_topological_order(rules: list[BridgeRule]) -> list[BridgeRule]:
    by_upstream: dict[str, list[BridgeRule]] = {}
    indegree: dict[str, int] = {}
    channels: set[str] = set()
    for rule in rules:
        channels.add(rule.upstream)
        channels.add(rule.downstream)
        indegree.setdefault(rule.upstream, 0)
        indegree.setdefault(rule.downstream, 0)
    for rule in rules:
        by_upstream.setdefault(rule.upstream, []).append(rule)
        indegree[rule.downstream] += 1
    for bucket in by_upstream.values():
        bucket.sort(key=lambda item: item.id)
    ready = sorted(channel for channel, degree in indegree.items() if degree == 0)
    result: list[BridgeRule] = []
    while ready:
        channel = ready.pop(0)
        for rule in by_upstream.get(channel, []):
            result.append(rule)
            indegree[rule.downstream] -= 1
            if indegree[rule.downstream] == 0:
                ready.append(rule.downstream)
                ready.sort()
    if len(result) != len(rules):
        fail("E_BRIDGE_CYCLE", "bridge graph contains a cycle")
    return result


def _parse_events(root: Any, topic_to_channel: dict[str, str]) -> list[Event]:
    events_raw = _extract_list(root, "events", "E_BAD_EVENT")
    events: list[Event] = []
    previous_seq = 0
    for raw in events_raw:
        if not isinstance(raw, dict):
            fail("E_BAD_EVENT", "each producer event must be an object")
        seq = require_plain_int(raw.get("seq"), "E_EVENT_SEQ", "event.seq")
        if seq <= previous_seq:
            fail(
                "E_EVENT_SEQ",
                "event seq must be strictly increasing",
                previous=previous_seq,
                actual=seq,
            )
        previous_seq = seq
        topic = require_nonempty_str(raw.get("topic"), "E_BAD_EVENT", "event.topic")
        if topic not in topic_to_channel:
            fail("E_BAD_TOPIC", "event refers to an unknown topic", topic=topic)
        partition_key = raw.get("partition_key")
        normalize_partition_key(partition_key)
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            fail("E_BAD_EVENT", "event.payload must be a JSON object", seq=seq)
        clock_ms = require_plain_int(
            raw.get("clock_ms"), "E_BAD_CLOCK", "event.clock_ms"
        )
        if clock_ms < 0:
            fail(
                "E_BAD_CLOCK",
                "event.clock_ms must be non-negative",
                seq=seq,
            )
        events.append(
            Event(
                seq=seq,
                topic=topic,
                partition_key=partition_key,
                payload=payload,
                clock_ms=clock_ms,
            )
        )
    return events


def _parse_subscriptions(
    root: Any,
    channel_names: set[str],
    projected_ends: dict[str, int],
) -> list[Subscription]:
    subscriptions_raw = _extract_list(root, "subscriptions", "E_BAD_SUBSCRIPTION")
    subscriptions: list[Subscription] = []
    consumer_ids: set[str] = set()
    for raw in subscriptions_raw:
        if not isinstance(raw, dict):
            fail("E_BAD_SUBSCRIPTION", "each subscription must be an object")
        consumer_id = require_nonempty_str(
            raw.get("consumer_id"), "E_BAD_SUBSCRIPTION", "consumer_id"
        )
        if consumer_id in consumer_ids:
            fail(
                "E_DUPLICATE_SUBSCRIPTION",
                "consumer_id is not unique",
                consumer_id=consumer_id,
            )
        consumer_ids.add(consumer_id)
        channel = require_nonempty_str(
            raw.get("channel"), "E_BAD_SUBSCRIPTION", "channel"
        )
        if channel not in channel_names:
            fail(
                "E_BAD_CHANNEL",
                "subscription refers to an unknown channel",
                channel=channel,
            )
        start_offset = require_plain_int(
            raw.get("start_offset"), "E_BAD_SUBSCRIPTION", "start_offset"
        )
        budget = require_plain_int(
            raw.get("budget"), "E_BAD_SUBSCRIPTION", "budget"
        )
        if start_offset < 0 or budget <= 0:
            fail(
                "E_BAD_SUBSCRIPTION",
                "start_offset must be non-negative and budget positive",
                consumer_id=consumer_id,
            )
        end = projected_ends.get(channel, 0)
        if start_offset > end:
            fail(
                "E_OFFSET_AHEAD",
                "subscription start_offset is past the channel end",
                consumer_id=consumer_id,
                channel=channel,
                start_offset=start_offset,
                channel_end=end,
            )
        subscriptions.append(
            Subscription(
                consumer_id=consumer_id,
                channel=channel,
                start_offset=start_offset,
                budget=budget,
            )
        )
    subscriptions.sort(key=lambda item: item.consumer_id)
    return subscriptions


def validate_bundle(
    catalog: Any,
    bridges: Any,
    events_root: Any,
    subscriptions_root: Any,
) -> SampleBundle:
    channels, topic_to_channel = _parse_channels(catalog)
    channel_names = {channel.name for channel in channels}
    rules = _parse_rules(bridges, channel_names)
    events = _parse_events(events_root, topic_to_channel)
    projected_ends = {name: 0 for name in channel_names}
    topic_to_channel_for_channel = {
        channel.name: channel.topic for channel in channels
    }
    for event in events:
        channel = topic_to_channel[event.topic]
        projected_ends[channel] += 1
    subscriptions = _parse_subscriptions(
        subscriptions_root, channel_names, projected_ends
    )
    semantic = {
        "channels": [channel.to_dict() for channel in channels],
        "rules": [rule.to_dict() for rule in rules],
        "events": [event.to_dict() for event in events],
        "subscriptions": [
            subscription.to_dict() for subscription in subscriptions
        ],
        "projected_ends": dict(sorted(projected_ends.items())),
    }
    digest = digest_json(semantic)
    return SampleBundle(
        channels=tuple(channels),
        rules=tuple(rules),
        events=tuple(events),
        subscriptions=tuple(subscriptions),
        projected_ends=projected_ends,
        digest=digest,
    )


def load_sample_bundle(sample_dir: str | Path) -> SampleBundle:
    root = Path(sample_dir)
    files = {
        "catalog": "catalog.json",
        "bridges": "bridges.json",
        "events": "events.json",
        "subscriptions": "subscriptions.json",
    }
    raw: dict[str, Any] = {}
    for key, filename in files.items():
        path = root / filename
        if not path.exists():
            fail(
                "E_INPUT_JSON",
                "required sample file is missing",
                file=filename,
            )
        raw[key] = load_json(path)
    return validate_bundle(
        raw["catalog"], raw["bridges"], raw["events"], raw["subscriptions"]
    )


def parse_event_input(raw: Any, topic_to_channel: dict[str, str]) -> Event:
    if not isinstance(raw, dict):
        fail("E_BAD_EVENT", "event edit data must be an object")
    seq = require_plain_int(raw.get("seq"), "E_EVENT_SEQ", "event.seq")
    if seq <= 0:
        fail("E_EVENT_SEQ", "event seq must be positive", seq=seq)
    topic = require_nonempty_str(raw.get("topic"), "E_BAD_EVENT", "event.topic")
    if topic not in topic_to_channel:
        fail("E_BAD_TOPIC", "event refers to an unknown topic", topic=topic)
    partition_key = raw.get("partition_key")
    normalize_partition_key(partition_key)
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        fail("E_BAD_EVENT", "event.payload must be a JSON object", seq=seq)
    clock_ms = require_plain_int(raw.get("clock_ms"), "E_BAD_CLOCK", "event.clock_ms")
    if clock_ms < 0:
        fail("E_BAD_CLOCK", "event.clock_ms must be non-negative", seq=seq)
    return Event(
        seq=seq,
        topic=topic,
        partition_key=partition_key,
        payload=payload,
        clock_ms=clock_ms,
    )


def parse_rule_input(raw: Any, channel_names: set[str]) -> BridgeRule:
    rules = _parse_rules({"rules": [raw]}, channel_names)
    return rules[0]


def validate_rule_set(rules: list[BridgeRule]) -> None:
    check_bridge_cycles(rules)


def config_digest(
    channels: list[Channel],
    rules: list[BridgeRule],
    subscriptions: list[Subscription],
) -> str:
    semantic = {
        "channels": [channel.to_dict() for channel in sorted(channels, key=lambda c: c.name)],
        "rules": [rule.to_dict() for rule in sorted(rules, key=lambda r: r.id)],
        "subscriptions": [
            subscription.to_dict()
            for subscription in sorted(
                subscriptions, key=lambda s: s.consumer_id
            )
        ],
    }
    return "sha256:" + sha256_text(canonical_json(semantic))
