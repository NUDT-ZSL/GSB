from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import ErrorCode, GatewayError

TOKEN_SCALE = 1_000_000


def _fail(code: str, message: str, **details: Any) -> None:
    raise GatewayError(code, message, details)


def load_json(path: str | Path, label: str) -> Any:
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(ErrorCode.CONFIG_FILE, f"cannot read {label} file", path=str(p), error=str(exc))
    try:
        return json.loads(raw, parse_float=Decimal)
    except (json.JSONDecodeError, InvalidOperation) as exc:
        _fail(ErrorCode.CONFIG_JSON, f"invalid JSON in {label} file", path=str(p), error=str(exc))


def require_object(value: Any, label: str, code: str = ErrorCode.CONFIG_ROOT) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(code, f"{label} must be a JSON object", field=label)
    return value


def require_int(
    value: Any,
    code: str,
    field: str,
    *,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(code, f"{field} must be an integer", field=field, value=repr(value))
    if minimum is not None and value < minimum:
        _fail(code, f"{field} must be >= {minimum}", field=field, value=value)
    return value


def require_nonempty_str(value: Any, code: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(code, f"{field} must be a non-empty string", field=field, value=repr(value))
    if value != value.strip():
        _fail(code, f"{field} must not have surrounding whitespace", field=field, value=value)
    return value


def require_positive_int(value: Any, code: str, field: str) -> int:
    return require_int(value, code, field, minimum=1)


def require_decimal(value: Any, code: str, field: str, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool):
        _fail(code, f"{field} must be a finite number", field=field, value=repr(value))
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        _fail(code, f"{field} must be a finite number", field=field, value=repr(value))
    if not number.is_finite():
        _fail(code, f"{field} must be finite", field=field, value=str(value))
    if positive and number <= 0:
        _fail(code, f"{field} must be > 0", field=field, value=str(value))
    return number


def scaled_rate(rate: Decimal, field: str) -> int:
    units = rate * TOKEN_SCALE
    if units != units.to_integral_value() or int(units) < 1:
        _fail(
            ErrorCode.TOKEN_PRECISION_INVALID,
            f"{field} cannot be represented at micro-token precision",
            field=field,
            value=str(rate),
            scale=TOKEN_SCALE,
        )
    return int(units)


def rate_text(rate_scaled: int) -> str:
    value = Decimal(rate_scaled) / Decimal(TOKEN_SCALE)
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class TopicConfig:
    name: str
    partitions: int
    partition_key_rule: str
    backlog_limit: int


@dataclass(frozen=True)
class ConsumerConfig:
    consumer_id: str
    topic: str
    request_size: int
    rate_limit_per_sec: Decimal
    rate_limit_scaled: int
    delivery_timeout_ms: int


@dataclass(frozen=True)
class QuotaConfig:
    config_version: int
    global_capacity: int
    global_refill_rate_per_sec: Decimal
    global_refill_scaled: int
    per_consumer_capacity: int
    round_budget_ms: int
    delivery_timeout_ms: int
    max_delivery_generation: int
    retry_backoff_base_ms: int
    retry_backoff_max_ms: int
    attempt_duration_ms: int


@dataclass(frozen=True)
class ClockConfig:
    initial_now_ms: int
    regression_tolerance_ms: int


@dataclass(frozen=True)
class FaultConfig:
    message_id: str
    consumer_id: str
    generation: int | None
    action: str
    duration_ms: int | None
    note: str


@dataclass(frozen=True)
class TopicBundle:
    topics: tuple[TopicConfig, ...]
    global_seed: int
    canonical: str


@dataclass(frozen=True)
class ConsumerBundle:
    consumers: tuple[ConsumerConfig, ...]
    canonical: str


@dataclass(frozen=True)
class FaultBundle:
    faults: tuple[FaultConfig, ...]
    canonical: str


@dataclass(frozen=True)
class GeneratedMessage:
    message_id: str
    topic: str
    partition: int
    partition_key: str
    seq_in_partition: int
    payload: str


PARTITION_RULES: dict[str, Callable[[str, int], int]] = {}


def _rule(fn: Callable[[str, int], int]) -> Callable[[str, int], int]:
    return fn


def _fnv1a32(key: str) -> int:
    value = 0x811C9DC5
    for byte in key.encode("utf-8"):
        value ^= byte
        value = (value * 0x01000193) & 0xFFFFFFFF
    return value


PARTITION_RULES["fnv1a32_mod"] = _rule(lambda key, n: _fnv1a32(key) % n)
PARTITION_RULES["sha256_mod"] = _rule(
    lambda key, n: int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big") % n
)
PARTITION_RULES["crc32_mod"] = _rule(
    lambda key, n: zlib.crc32(key.encode("utf-8")) % n
)


def parse_topics(data: Any) -> TopicBundle:
    root = require_object(data, "topics")
    raw_topics = root.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        _fail(ErrorCode.CONFIG_ROOT, "topics.topics must be a non-empty array", field="topics")
    seed = require_int(root.get("global_seed", 0), ErrorCode.CONFIG_ROOT, "global_seed", minimum=0)
    topics: list[TopicConfig] = []
    names: set[str] = set()
    for index, item in enumerate(raw_topics):
        obj = require_object(item, f"topics[{index}]")
        name = require_nonempty_str(obj.get("name"), ErrorCode.TOPIC_NAME_INVALID, f"topics[{index}].name")
        if name in names:
            _fail(ErrorCode.TOPIC_DUPLICATE, "topic name must be unique", field="name", value=name)
        names.add(name)
        partitions = require_positive_int(
            obj.get("partitions"), ErrorCode.TOPIC_PARTITIONS_INVALID, f"topics[{index}].partitions"
        )
        rule = require_nonempty_str(
            obj.get("partition_key_rule"),
            ErrorCode.TOPIC_PARTITION_RULE_INVALID,
            f"topics[{index}].partition_key_rule",
        )
        if rule not in PARTITION_RULES:
            _fail(
                ErrorCode.TOPIC_PARTITION_RULE_INVALID,
                "partition_key_rule is not recognized",
                field="partition_key_rule",
                value=rule,
                supported=sorted(PARTITION_RULES),
            )
        backlog_limit = require_positive_int(
            obj.get("backlog_limit"), ErrorCode.TOPIC_BACKLOG_LIMIT_INVALID, f"topics[{index}].backlog_limit"
        )
        topics.append(TopicConfig(name, partitions, rule, backlog_limit))
    canonical = canonical_json(
        {
            "global_seed": seed,
            "topics": [
                {
                    "name": t.name,
                    "partitions": t.partitions,
                    "partition_key_rule": t.partition_key_rule,
                    "backlog_limit": t.backlog_limit,
                }
                for t in topics
            ],
        }
    )
    return TopicBundle(tuple(topics), seed, canonical)


def parse_consumers(data: Any, topics: TopicBundle) -> ConsumerBundle:
    root = require_object(data, "consumers")
    raw_consumers = root.get("consumers")
    if not isinstance(raw_consumers, list) or not raw_consumers:
        _fail(ErrorCode.CONFIG_ROOT, "consumers.consumers must be a non-empty array", field="consumers")
    topic_names = {topic.name for topic in topics.topics}
    consumers: list[ConsumerConfig] = []
    ids: set[str] = set()
    for index, item in enumerate(raw_consumers):
        obj = require_object(item, f"consumers[{index}]")
        consumer_id = require_nonempty_str(
            obj.get("consumer_id"), ErrorCode.CONSUMER_ID_INVALID, f"consumers[{index}].consumer_id"
        )
        if consumer_id in ids:
            _fail(
                ErrorCode.CONSUMER_DUPLICATE,
                "consumer_id must be unique",
                field="consumer_id",
                value=consumer_id,
            )
        ids.add(consumer_id)
        topic = require_nonempty_str(obj.get("topic"), ErrorCode.CONSUMER_TOPIC_NOT_FOUND, f"consumers[{index}].topic")
        if topic not in topic_names:
            _fail(
                ErrorCode.CONSUMER_TOPIC_NOT_FOUND,
                "consumer references an unknown topic",
                field="topic",
                value=topic,
                consumer_id=consumer_id,
            )
        request_size = require_positive_int(
            obj.get("request_size"), ErrorCode.CONSUMER_RESULT_SIZE_INVALID, f"consumers[{index}].request_size"
        )
        rate = require_decimal(
            obj.get("rate_limit_per_sec"),
            ErrorCode.CONSUMER_RATE_INVALID,
            f"consumers[{index}].rate_limit_per_sec",
        )
        rate_scaled = scaled_rate(rate, f"consumers[{index}].rate_limit_per_sec")
        timeout = require_positive_int(
            obj.get("delivery_timeout_ms"),
            ErrorCode.CONSUMER_TIMEOUT_INVALID,
            f"consumers[{index}].delivery_timeout_ms",
        )
        consumers.append(
            ConsumerConfig(
                consumer_id=consumer_id,
                topic=topic,
                request_size=request_size,
                rate_limit_per_sec=rate,
                rate_limit_scaled=rate_scaled,
                delivery_timeout_ms=timeout,
            )
        )
    canonical = canonical_json(
        {
            "consumers": [
                {
                    "consumer_id": c.consumer_id,
                    "topic": c.topic,
                    "request_size": c.request_size,
                    "rate_limit_per_sec": rate_text(c.rate_limit_scaled),
                    "delivery_timeout_ms": c.delivery_timeout_ms,
                }
                for c in consumers
            ]
        }
    )
    return ConsumerBundle(tuple(consumers), canonical)


def parse_quota(data: Any) -> QuotaConfig:
    root = require_object(data, "quota")
    version = require_int(root.get("config_version"), ErrorCode.CONFIG_VERSION_INVALID, "config_version", minimum=1)
    global_obj = require_object(root.get("global"), "quota.global")
    global_capacity = require_positive_int(
        global_obj.get("capacity"), ErrorCode.GLOBAL_CAPACITY_INVALID, "global.capacity"
    )
    global_refill = require_decimal(
        global_obj.get("refill_rate_per_sec"),
        ErrorCode.GLOBAL_REFILL_INVALID,
        "global.refill_rate_per_sec",
    )
    global_refill_scaled = scaled_rate(global_refill, "global.refill_rate_per_sec")
    per_consumer = require_object(root.get("per_consumer"), "quota.per_consumer")
    consumer_capacity = require_positive_int(
        per_consumer.get("capacity"), ErrorCode.CONSUMER_CAPACITY_INVALID, "per_consumer.capacity"
    )
    round_budget = require_positive_int(
        root.get("round_budget_ms"), ErrorCode.ROUND_BUDGET_INVALID, "round_budget_ms"
    )
    delivery_timeout = require_positive_int(
        root.get("delivery_timeout_ms"), ErrorCode.DELIVERY_TIMEOUT_INVALID, "delivery_timeout_ms"
    )
    max_generation = require_int(
        root.get("max_delivery_generation"),
        ErrorCode.MAX_GENERATION_INVALID,
        "max_delivery_generation",
        minimum=0,
    )
    backoff_base = require_positive_int(
        root.get("retry_backoff_base_ms"),
        ErrorCode.BACKOFF_BASE_INVALID,
        "retry_backoff_base_ms",
    )
    backoff_max = require_positive_int(
        root.get("retry_backoff_max_ms"),
        ErrorCode.BACKOFF_MAX_INVALID,
        "retry_backoff_max_ms",
    )
    attempt_duration = require_positive_int(
        root.get("attempt_duration_ms"), ErrorCode.ATTEMPT_DURATION_INVALID, "attempt_duration_ms"
    )
    return QuotaConfig(
        config_version=version,
        global_capacity=global_capacity,
        global_refill_rate_per_sec=global_refill,
        global_refill_scaled=global_refill_scaled,
        per_consumer_capacity=consumer_capacity,
        round_budget_ms=round_budget,
        delivery_timeout_ms=delivery_timeout,
        max_delivery_generation=max_generation,
        retry_backoff_base_ms=backoff_base,
        retry_backoff_max_ms=backoff_max,
        attempt_duration_ms=attempt_duration,
    )


def parse_clock(data: Any) -> ClockConfig:
    root = require_object(data, "clock")
    initial = require_int(
        root.get("initial_now_ms", 0), ErrorCode.CLOCK_INITIAL_INVALID, "initial_now_ms", minimum=0
    )
    tolerance = require_int(
        root.get("regression_tolerance_ms", 0),
        ErrorCode.CLOCK_TOLERANCE_INVALID,
        "regression_tolerance_ms",
        minimum=0,
    )
    return ClockConfig(initial, tolerance)


def generate_messages(bundle: TopicBundle) -> tuple[GeneratedMessage, ...]:
    messages: list[GeneratedMessage] = []
    for topic in bundle.topics:
        rule = PARTITION_RULES[topic.partition_key_rule]
        per_partition: list[list[GeneratedMessage]] = [[] for _ in range(topic.partitions)]
        candidate = 0
        max_candidates = max(1000, topic.partitions * topic.backlog_limit * 50)
        while any(len(bucket) < topic.backlog_limit for bucket in per_partition):
            if candidate >= max_candidates:
                _fail(
                    ErrorCode.INTERNAL,
                    "deterministic message generator could not fill every partition",
                    topic=topic.name,
                )
            partition_key = hashlib.sha256(f"{bundle.global_seed}:{topic.name}:{candidate}".encode()).hexdigest()[:24]
            partition = rule(partition_key, topic.partitions)
            bucket = per_partition[partition]
            if len(bucket) < topic.backlog_limit:
                seq = len(bucket)
                message_id = "msg_" + hashlib.sha256(
                    f"{topic.name}:{partition}:{seq}:{partition_key}".encode()
                ).hexdigest()[:24]
                payload = canonical_json(
                    {
                        "generator": "local-deterministic-v1",
                        "global_seed": bundle.global_seed,
                        "topic": topic.name,
                        "partition": partition,
                        "seq": seq,
                        "partition_key": partition_key,
                        "amount": int(hashlib.sha256(f"{message_id}:amount".encode()).hexdigest()[:8], 16) % 10000,
                    }
                )
                bucket.append(
                    GeneratedMessage(
                        message_id=message_id,
                        topic=topic.name,
                        partition=partition,
                        partition_key=partition_key,
                        seq_in_partition=seq,
                        payload=payload,
                    )
                )
            candidate += 1
        for bucket in per_partition:
            messages.extend(bucket)
    messages.sort(key=lambda m: (m.topic, m.partition, m.seq_in_partition))
    return tuple(messages)


def parse_faults(
    data: Any, consumers: ConsumerBundle, messages: Iterable[GeneratedMessage]
) -> FaultBundle:
    root = require_object(data, "faults", ErrorCode.FAULT_ROOT)
    raw_events = root.get("events", [])
    if not isinstance(raw_events, list):
        _fail(ErrorCode.FAULT_ROOT, "faults.events must be an array", field="events")
    consumer_ids = {c.consumer_id for c in consumers.consumers}
    message_by_id = {m.message_id: m for m in messages}
    faults: list[FaultConfig] = []
    seen: set[tuple[str, str, int | None]] = set()
    allowed = {"success", "fail", "reject", "expire", "slow", "crash_after_claim"}
    for index, item in enumerate(raw_events):
        obj = require_object(item, f"faults.events[{index}]", ErrorCode.FAULT_ROOT)
        consumer_id = require_nonempty_str(
            obj.get("consumer_id"), ErrorCode.FAULT_TARGET_INVALID, f"events[{index}].consumer_id"
        )
        if consumer_id not in consumer_ids:
            _fail(
                ErrorCode.FAULT_TARGET_NOT_FOUND,
                "fault references an unknown consumer",
                field="consumer_id",
                value=consumer_id,
            )
        if "message_id" in obj:
            message_id = require_nonempty_str(
                obj.get("message_id"), ErrorCode.FAULT_TARGET_INVALID, f"events[{index}].message_id"
            )
            if message_id not in message_by_id:
                _fail(
                    ErrorCode.FAULT_TARGET_NOT_FOUND,
                    "fault references an unknown message",
                    field="message_id",
                    value=message_id,
                )
        else:
            topic = require_nonempty_str(
                obj.get("topic"), ErrorCode.FAULT_TARGET_INVALID, f"events[{index}].topic"
            )
            partition = require_int(
                obj.get("partition"),
                ErrorCode.FAULT_TARGET_INVALID,
                f"events[{index}].partition",
                minimum=0,
            )
            seq = require_int(
                obj.get("seq"),
                ErrorCode.FAULT_TARGET_INVALID,
                f"events[{index}].seq",
                minimum=0,
            )
            match = [
                m
                for m in message_by_id.values()
                if m.topic == topic and m.partition == partition and m.seq_in_partition == seq
            ]
            if not match:
                _fail(
                    ErrorCode.FAULT_TARGET_NOT_FOUND,
                    "fault selector does not match a generated message",
                    field="selector",
                    topic=topic,
                    partition=partition,
                    seq=seq,
                )
            message_id = match[0].message_id
        generation_raw = obj.get("generation")
        generation = None
        if generation_raw is not None:
            generation = require_int(
                generation_raw,
                ErrorCode.FAULT_GENERATION_INVALID,
                f"events[{index}].generation",
                minimum=1,
            )
        action = require_nonempty_str(
            obj.get("action"), ErrorCode.FAULT_ACTION_INVALID, f"events[{index}].action"
        )
        if action not in allowed:
            _fail(
                ErrorCode.FAULT_ACTION_INVALID,
                "unsupported fault action",
                field="action",
                value=action,
                supported=sorted(allowed),
            )
        duration = None
        if obj.get("duration_ms") is not None:
            duration = require_int(
                obj.get("duration_ms"),
                ErrorCode.FAULT_DURATION_INVALID,
                f"events[{index}].duration_ms",
                minimum=0,
            )
        elif action == "slow":
            _fail(
                ErrorCode.FAULT_DURATION_INVALID,
                "slow fault requires duration_ms",
                field=f"events[{index}].duration_ms",
            )
        note_raw = obj.get("note", "")
        if not isinstance(note_raw, str):
            _fail(ErrorCode.FAULT_ROOT, "fault note must be a string", field=f"events[{index}].note")
        key = (consumer_id, message_id, generation)
        if key in seen:
            _fail(
                ErrorCode.FAULT_DUPLICATE,
                "duplicate fault selector",
                consumer_id=consumer_id,
                message_id=message_id,
                generation=generation,
            )
        seen.add(key)
        faults.append(FaultConfig(message_id, consumer_id, generation, action, duration, note_raw))
    faults.sort(key=lambda f: (f.consumer_id, f.message_id, -1 if f.generation is None else f.generation, f.action))
    canonical = canonical_json(
        {
            "events": [
                {
                    "message_id": f.message_id,
                    "consumer_id": f.consumer_id,
                    "generation": f.generation,
                    "action": f.action,
                    "duration_ms": f.duration_ms,
                    "note": f.note,
                }
                for f in faults
            ]
        }
    )
    return FaultBundle(tuple(faults), canonical)


def canonical_json(value: Any) -> str:
    def default(obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return format(obj.normalize(), "f")
        raise TypeError(f"not JSON serializable: {type(obj)!r}")

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=default)


def combine_fingerprint(
    topics: TopicBundle,
    consumers: ConsumerBundle,
    quota: QuotaConfig,
    clock: ClockConfig,
    faults: FaultBundle,
) -> str:
    payload = {
        "topics": json.loads(topics.canonical),
        "consumers": json.loads(consumers.canonical),
        "quota": {
            "config_version": quota.config_version,
            "global_capacity": quota.global_capacity,
            "global_refill_rate_per_sec": rate_text(quota.global_refill_scaled),
            "per_consumer_capacity": quota.per_consumer_capacity,
            "round_budget_ms": quota.round_budget_ms,
            "delivery_timeout_ms": quota.delivery_timeout_ms,
            "max_delivery_generation": quota.max_delivery_generation,
            "retry_backoff_base_ms": quota.retry_backoff_base_ms,
            "retry_backoff_max_ms": quota.retry_backoff_max_ms,
            "attempt_duration_ms": quota.attempt_duration_ms,
        },
        "clock": {
            "initial_now_ms": clock.initial_now_ms,
            "regression_tolerance_ms": clock.regression_tolerance_ms,
        },
        "faults": json.loads(faults.canonical),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def backoff_delay_ms(
    message_id: str,
    consumer_id: str,
    generation: int,
    base_ms: int,
    max_ms: int,
) -> tuple[int, int, int]:
    exponent = max(0, generation - 1)
    if exponent >= 63:
        capped_base = max_ms
    else:
        capped_base = min(max_ms, base_ms * (1 << exponent))
    digest = hashlib.sha256(f"backoff:{message_id}:{consumer_id}:{generation}".encode()).digest()
    jitter = int.from_bytes(digest[:8], "big") % (base_ms + 1)
    delay = min(max_ms, capped_base + jitter)
    return capped_base, jitter, delay


def wait_ms_for_token(balance_scaled: int, rate_scaled_per_sec: int) -> int:
    if balance_scaled >= TOKEN_SCALE:
        return 0
    need = TOKEN_SCALE - balance_scaled
    return (need * 1000 + rate_scaled_per_sec - 1) // rate_scaled_per_sec



