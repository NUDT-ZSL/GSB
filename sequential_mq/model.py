"""Input validation and deterministic normalization.

The model layer never opens the database.  Consequently a rejected sample
cannot create or mutate persistence files.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    E_BATCH_FIELD,
    E_BATCH_SEQ,
    E_BATCH_TOO_LARGE,
    E_CLOCK_REGRESSION,
    E_INJECTION_CONFIG,
    E_INJECTION_FILE,
    E_INPUT_JSON,
    E_PARTITION_CONFIG,
    E_PARTITION_LIMIT,
    E_PAYLOAD_DECODE,
    E_ROUTING_RULE,
    E_SCHEMA,
    E_TOPIC_DUPLICATE,
    E_TX_DECLARATION,
    EngineError,
)

SUPPORTED_SCHEMA_VERSION = 1
SUPPORTED_ENCODINGS = {"utf8", "utf8-json", "base64"}
INJECTION_STAGES = {"append", "prepare", "commit", "ack"}
INJECTION_ERROR_CODES = {
    "partition_log_full": "E_APPEND_LOG_FULL",
    "E_APPEND_LOG_FULL": "E_APPEND_LOG_FULL",
    "transaction_conflict": "E_PREPARE_TX_CONFLICT",
    "E_PREPARE_TX_CONFLICT": "E_PREPARE_TX_CONFLICT",
    "digest_mismatch": "E_COMMIT_DIGEST_MISMATCH",
    "E_COMMIT_DIGEST_MISMATCH": "E_COMMIT_DIGEST_MISMATCH",
    "offset_regression": "E_ACK_OFFSET_REGRESSION",
    "E_ACK_OFFSET_REGRESSION": "E_ACK_OFFSET_REGRESSION",
}
STAGE_ERROR_CODE = {
    "append": "E_APPEND_LOG_FULL",
    "prepare": "E_PREPARE_TX_CONFLICT",
    "commit": "E_COMMIT_DIGEST_MISMATCH",
    "ack": "E_ACK_OFFSET_REGRESSION",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fnv1a32(data: bytes) -> int:
    value = 0x811C9DC5
    for byte in data:
        value ^= byte
        value = (value * 0x01000193) & 0xFFFFFFFF
    return value


def read_json(path: str | Path, what: str) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EngineError(
            E_INPUT_JSON,
            f"cannot read {what} JSON",
            details={"path": str(path), "reason": str(exc)},
        ) from exc


def _bad(code: str, message: str, **details: Any) -> EngineError:
    return EngineError(code, message, details=details)


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _bad(E_SCHEMA, f"{field} must be an object", field=field)
    return value


def _require_int(
    value: Any,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(E_SCHEMA, f"{field} must be an integer", field=field, value=value)
    if minimum is not None and value < minimum:
        raise _bad(
            E_SCHEMA,
            f"{field} must be >= {minimum}",
            field=field,
            value=value,
            minimum=minimum,
        )
    if maximum is not None and value > maximum:
        raise _bad(
            E_SCHEMA,
            f"{field} must be <= {maximum}",
            field=field,
            value=value,
            maximum=maximum,
        )
    return value


def _require_string(value: Any, field: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value:
        raise _bad(E_SCHEMA, f"{field} must be a non-empty string", field=field)
    if len(value) > max_length:
        raise _bad(
            E_SCHEMA,
            f"{field} is too long",
            field=field,
            max_length=max_length,
            length=len(value),
        )
    return value


@dataclass(frozen=True)
class EngineConfig:
    max_partitions_per_topic: int
    max_batch_messages: int
    transaction_timeout_ms: int
    retry_budget: int
    logical_clock_start_ms: int
    injection_manifest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "injection_manifest": self.injection_manifest,
            "logical_clock_start_ms": self.logical_clock_start_ms,
            "max_batch_messages": self.max_batch_messages,
            "max_partitions_per_topic": self.max_partitions_per_topic,
            "retry_budget": self.retry_budget,
            "transaction_timeout_ms": self.transaction_timeout_ms,
        }


@dataclass(frozen=True)
class KeyRule:
    partition: int
    algorithm: str
    modulus: int
    remainder: int
    salt: str
    rule_type: str = "hash_modulo"

    def matches(self, ordering_key: str) -> bool:
        payload = f"{self.salt}\x00{ordering_key}".encode("utf-8")
        return fnv1a32(payload) % self.modulus == self.remainder

    def as_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "modulus": self.modulus,
            "partition": self.partition,
            "remainder": self.remainder,
            "salt": self.salt,
            "type": self.rule_type,
        }


@dataclass(frozen=True)
class Topic:
    name: str
    partition_count: int
    partition_capacities: tuple[int, ...]
    ordering_key_rules: tuple[KeyRule, ...]
    digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ordering_key_rules": [rule.as_dict() for rule in self.ordering_key_rules],
            "partition_capacities": list(self.partition_capacities),
            "partition_count": self.partition_count,
        }

    def route(self, ordering_key: str) -> int:
        matches = [rule.partition for rule in self.ordering_key_rules if rule.matches(ordering_key)]
        if len(matches) != 1:
            raise _bad(
                E_ROUTING_RULE,
                "ordering key did not match exactly one partition rule",
                topic=self.name,
                ordering_key=ordering_key,
                matched_partitions=matches,
            )
        return matches[0]


@dataclass(frozen=True)
class Message:
    message_index: int
    message_id: str
    payload_encoding: str
    payload_data: Any
    payload_json: str
    payload_digest: str

    def canonical(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "payload": {
                "data": self.payload_data,
                "encoding": self.payload_encoding,
            },
            "payload_digest": self.payload_digest,
        }


@dataclass(frozen=True)
class Batch:
    batch_seq: int
    topic: str
    ordering_key: str
    tx_id: str | None
    logical_ts_ms: int
    messages: tuple[Message, ...]
    digest: str
    canonical: str

    @property
    def message_count(self) -> int:
        return len(self.messages)

    def transaction_declaration(self, declared: bool) -> dict[str, Any]:
        return {"declared": declared, "id": self.tx_id}


@dataclass(frozen=True)
class Injection:
    injection_id: str
    stage: str
    action: str
    timing: str
    error_code: str | None
    occurrence: int
    match: dict[str, Any]
    canonical: str

    def matches(self, context: dict[str, Any]) -> bool:
        for key, expected in self.match.items():
            if context.get(key) != expected:
                return False
        return True


@dataclass(frozen=True)
class ValidatedSample:
    sample_path: str
    topics: tuple[Topic, ...]
    batches: tuple[Batch, ...]
    config: EngineConfig
    injections: tuple[Injection, ...]
    sample_digest: str

    @property
    def topics_by_name(self) -> dict[str, Topic]:
        return {topic.name: topic for topic in self.topics}


def _normalize_payload(payload: Any, *, topic: str, batch_seq: int, index: int) -> tuple[str, Any, str, str]:
    context = {"topic": topic, "batch_seq": batch_seq, "message_index": index}
    if isinstance(payload, str):
        encoding = "utf8"
        data: Any = payload
        raw = payload.encode("utf-8")
    elif isinstance(payload, dict):
        if "encoding" not in payload or "data" not in payload:
            raise _bad(
                E_PAYLOAD_DECODE,
                "payload object requires encoding and data",
                **context,
            )
        encoding = payload["encoding"]
        data = payload["data"]
        if encoding not in SUPPORTED_ENCODINGS:
            raise _bad(
                E_PAYLOAD_DECODE,
                "unsupported payload encoding",
                encoding=encoding,
                supported=sorted(SUPPORTED_ENCODINGS),
                **context,
            )
        if encoding == "utf8":
            if not isinstance(data, str):
                raise _bad(E_PAYLOAD_DECODE, "utf8 payload data must be a string", **context)
            raw = data.encode("utf-8")
        elif encoding == "utf8-json":
            try:
                raw = canonical_json(data).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise _bad(
                    E_PAYLOAD_DECODE,
                    "utf8-json payload is not encodable",
                    reason=str(exc),
                    **context,
                ) from exc
            data = json.loads(raw.decode("utf-8"))
        else:
            if not isinstance(data, str):
                raise _bad(E_PAYLOAD_DECODE, "base64 payload data must be a string", **context)
            try:
                raw = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise _bad(
                    E_PAYLOAD_DECODE,
                    "payload is not valid base64",
                    reason=str(exc),
                    **context,
                ) from exc
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _bad(
                    E_PAYLOAD_DECODE,
                    "base64 payload does not decode as UTF-8",
                    reason=str(exc),
                    **context,
                ) from exc
    else:
        raise _bad(
            E_PAYLOAD_DECODE,
            "payload must be a string or an encoding/data object",
            **context,
        )
    payload_json = canonical_json({"data": data, "encoding": encoding})
    payload_digest = hashlib.sha256(encoding.encode("ascii") + b"\0" + raw).hexdigest()
    return encoding, data, payload_json, payload_digest


def payload_digest_for(encoding: str, data: Any) -> str:
    """Recompute a stored payload digest without consulting wall-clock state."""
    if encoding == "utf8":
        if not isinstance(data, str):
            raise _bad(E_PAYLOAD_DECODE, "utf8 payload data must be a string")
        raw = data.encode("utf-8")
    elif encoding == "utf8-json":
        try:
            raw = canonical_json(data).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise _bad(
                E_PAYLOAD_DECODE,
                "utf8-json payload is not encodable",
                reason=str(exc),
            ) from exc
    elif encoding == "base64":
        if not isinstance(data, str):
            raise _bad(E_PAYLOAD_DECODE, "base64 payload data must be a string")
        try:
            raw = base64.b64decode(data, validate=True)
            raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise _bad(
                E_PAYLOAD_DECODE,
                "base64 payload does not decode as UTF-8",
                reason=str(exc),
            ) from exc
    else:
        raise _bad(E_PAYLOAD_DECODE, "unsupported payload encoding", encoding=encoding)
    return hashlib.sha256(encoding.encode("ascii") + b"\0" + raw).hexdigest()


def normalize_batch(
    value: Any,
    *,
    topics_by_name: dict[str, Topic],
    config: EngineConfig,
) -> Batch:
    obj = _require_object(value, "batch")
    batch_seq = _require_int(obj.get("batch_seq"), "batch_seq", minimum=1)
    topic_name = _require_string(obj.get("topic"), "topic")
    topic = topics_by_name.get(topic_name)
    if topic is None:
        raise _bad(
            E_BATCH_FIELD,
            "batch references an unknown topic",
            batch_seq=batch_seq,
            topic=topic_name,
        )
    ordering_key = _require_string(obj.get("ordering_key"), "ordering_key")
    logical_ts_ms = _require_int(obj.get("logical_ts_ms"), "logical_ts_ms", minimum=0)
    if logical_ts_ms < config.logical_clock_start_ms:
        raise _bad(
            E_CLOCK_REGRESSION,
            "batch timestamp precedes the configured logical clock start",
            batch_seq=batch_seq,
            logical_ts_ms=logical_ts_ms,
            clock_start_ms=config.logical_clock_start_ms,
        )

    transaction = obj.get("transaction")
    tx_id: str | None
    if transaction is None:
        tx_id = None
    else:
        if not isinstance(transaction, dict):
            raise _bad(
                E_TX_DECLARATION,
                "transaction must be null or an object",
                batch_seq=batch_seq,
            )
        declared = transaction.get("declared")
        if not isinstance(declared, bool):
            raise _bad(
                E_TX_DECLARATION,
                "transaction.declared must be a boolean",
                batch_seq=batch_seq,
            )
        declared_id = transaction.get("id")
        if declared:
            tx_id = _require_string(declared_id, "transaction.id", max_length=128)
        else:
            if declared_id is not None:
                raise _bad(
                    E_TX_DECLARATION,
                    "a non-transactional batch must not carry a transaction id",
                    batch_seq=batch_seq,
                    transaction_id=declared_id,
                )
            tx_id = None

    raw_messages = obj.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise _bad(
            E_BATCH_FIELD,
            "messages must be a non-empty array",
            batch_seq=batch_seq,
        )
    if len(raw_messages) > config.max_batch_messages:
        raise _bad(
            E_BATCH_TOO_LARGE,
            "batch exceeds max_batch_messages",
            batch_seq=batch_seq,
            message_count=len(raw_messages),
            max_batch_messages=config.max_batch_messages,
        )

    messages: list[Message] = []
    seen_message_ids: set[str] = set()
    for index, raw_message in enumerate(raw_messages):
        if not isinstance(raw_message, dict):
            raise _bad(
                E_BATCH_FIELD,
                "each message must be an object",
                batch_seq=batch_seq,
                message_index=index,
            )
        default_id = f"b{batch_seq}-m{index}"
        message_id = raw_message.get("message_id", default_id)
        message_id = _require_string(message_id, "message_id", max_length=128)
        if message_id in seen_message_ids:
            raise _bad(
                E_BATCH_FIELD,
                "message_id must be unique within a batch",
                batch_seq=batch_seq,
                message_id=message_id,
            )
        seen_message_ids.add(message_id)
        encoding, data, payload_json, payload_digest = _normalize_payload(
            raw_message.get("payload"),
            topic=topic_name,
            batch_seq=batch_seq,
            index=index,
        )
        messages.append(
            Message(
                message_index=index,
                message_id=message_id,
                payload_encoding=encoding,
                payload_data=data,
                payload_json=payload_json,
                payload_digest=payload_digest,
            )
        )

    normalized = {
        "batch_seq": batch_seq,
        "logical_ts_ms": logical_ts_ms,
        "messages": [message.canonical() for message in messages],
        "ordering_key": ordering_key,
        "topic": topic_name,
        "transaction": None if tx_id is None else {"declared": True, "id": tx_id},
    }
    canonical = canonical_json(normalized)
    return Batch(
        batch_seq=batch_seq,
        topic=topic_name,
        ordering_key=ordering_key,
        tx_id=tx_id,
        logical_ts_ms=logical_ts_ms,
        messages=tuple(messages),
        digest=digest_text(canonical),
        canonical=canonical,
    )


def _validate_topic(value: Any, config: EngineConfig, seen: set[str]) -> Topic:
    obj = _require_object(value, "topic")
    name = _require_string(obj.get("name"), "topic.name", max_length=128)
    if name in seen:
        raise _bad(E_TOPIC_DUPLICATE, "topic name must be unique", topic=name)
    seen.add(name)
    partition_count = _require_int(obj.get("partition_count"), "partition_count", minimum=1)
    if partition_count > config.max_partitions_per_topic:
        raise _bad(
            E_PARTITION_LIMIT,
            "topic partition count exceeds configured limit",
            topic=name,
            partition_count=partition_count,
            max_partitions_per_topic=config.max_partitions_per_topic,
        )
    capacities_raw = obj.get("partition_capacities")
    if not isinstance(capacities_raw, list) or len(capacities_raw) != partition_count:
        raise _bad(
            E_PARTITION_CONFIG,
            "partition_capacities must contain one entry per partition",
            topic=name,
            partition_count=partition_count,
            capacity_entries=len(capacities_raw) if isinstance(capacities_raw, list) else None,
        )
    capacities = tuple(
        _require_int(capacity, "partition_capacities[]", minimum=1) for capacity in capacities_raw
    )

    raw_rules = obj.get("ordering_key_rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise _bad(
            E_ROUTING_RULE,
            "ordering_key_rules must be a non-empty array",
            topic=name,
        )
    rules: list[KeyRule] = []
    for raw_rule in raw_rules:
        rule_obj = _require_object(raw_rule, "ordering_key_rules[]")
        partition = _require_int(rule_obj.get("partition"), "rule.partition", minimum=0)
        if partition >= partition_count:
            raise _bad(
                E_ROUTING_RULE,
                "rule partition is outside the topic",
                topic=name,
                partition=partition,
                partition_count=partition_count,
            )
        rule_type = rule_obj.get("type")
        algorithm = rule_obj.get("algorithm")
        if rule_type != "hash_modulo" or algorithm != "fnv1a32":
            raise _bad(
                E_ROUTING_RULE,
                "only hash_modulo/fnv1a32 ordering key rules are supported",
                topic=name,
                partition=partition,
                type=rule_type,
                algorithm=algorithm,
            )
        modulus = _require_int(rule_obj.get("modulus"), "rule.modulus", minimum=1)
        remainder = _require_int(rule_obj.get("remainder"), "rule.remainder", minimum=0)
        salt = _require_string(rule_obj.get("salt"), "rule.salt", max_length=128)
        rules.append(
            KeyRule(
                partition=partition,
                algorithm=algorithm,
                modulus=modulus,
                remainder=remainder,
                salt=salt,
            )
        )
    rules.sort(key=lambda rule: rule.partition)
    partitions = [rule.partition for rule in rules]
    if partitions != list(range(partition_count)):
        raise _bad(
            E_ROUTING_RULE,
            "each partition must have exactly one ordering key rule",
            topic=name,
            rule_partitions=partitions,
            partition_count=partition_count,
        )
    signatures = {(rule.algorithm, rule.modulus, rule.salt) for rule in rules}
    if len(signatures) != 1:
        raise _bad(
            E_ROUTING_RULE,
            "all rules in a topic must use the same algorithm, modulus and salt",
            topic=name,
        )
    modulus = rules[0].modulus
    if modulus != partition_count or {rule.remainder for rule in rules} != set(range(partition_count)):
        raise _bad(
            E_ROUTING_RULE,
            "remainders must cover every partition exactly once",
            topic=name,
            modulus=modulus,
            partition_count=partition_count,
            remainders=sorted(rule.remainder for rule in rules),
        )
    digest_payload = {
        "name": name,
        "ordering_key_rules": [rule.as_dict() for rule in rules],
        "partition_capacities": list(capacities),
        "partition_count": partition_count,
    }
    return Topic(
        name=name,
        partition_count=partition_count,
        partition_capacities=capacities,
        ordering_key_rules=tuple(rules),
        digest=digest_text(canonical_json(digest_payload)),
    )


def validate_injections(value: Any) -> tuple[Injection, ...]:
    obj = _require_object(value, "injection manifest")
    version = obj.get("schema_version", SUPPORTED_SCHEMA_VERSION)
    if version != SUPPORTED_SCHEMA_VERSION:
        raise _bad(E_INJECTION_CONFIG, "unsupported injection schema", schema_version=version)
    entries = obj.get("injections")
    if not isinstance(entries, list):
        raise _bad(E_INJECTION_CONFIG, "injections must be an array")
    seen: set[str] = set()
    result: list[Injection] = []
    for raw_entry in entries:
        entry = _require_object(raw_entry, "injection")
        injection_id = _require_string(entry.get("id"), "injection.id", max_length=128)
        if injection_id in seen:
            raise _bad(
                E_INJECTION_CONFIG,
                "injection id must be unique",
                injection_id=injection_id,
            )
        seen.add(injection_id)
        stage = entry.get("stage")
        if stage not in INJECTION_STAGES:
            raise _bad(
                E_INJECTION_CONFIG,
                "unsupported injection stage",
                injection_id=injection_id,
                stage=stage,
                supported=sorted(INJECTION_STAGES),
            )
        action = entry.get("action", "error")
        if action not in {"error", "crash"}:
            raise _bad(
                E_INJECTION_CONFIG,
                "injection action must be error or crash",
                injection_id=injection_id,
                action=action,
            )
        timing = entry.get("timing", "before")
        if timing not in {"before", "after"}:
            raise _bad(
                E_INJECTION_CONFIG,
                "injection timing must be before or after",
                injection_id=injection_id,
                timing=timing,
            )
        if action == "error" and timing != "before":
            raise _bad(
                E_INJECTION_CONFIG,
                "controlled errors must use timing=before",
                injection_id=injection_id,
            )
        error_code: str | None = None
        if action == "error":
            error_name = entry.get("error")
            mapped = INJECTION_ERROR_CODES.get(error_name)
            if mapped is None or mapped != STAGE_ERROR_CODE[stage]:
                raise _bad(
                    E_INJECTION_CONFIG,
                    "injection error does not match its stage",
                    injection_id=injection_id,
                    stage=stage,
                    error=error_name,
                    expected=STAGE_ERROR_CODE[stage],
                )
            error_code = mapped
        occurrence = entry.get("occurrence", 1)
        if isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 1:
            raise _bad(
                E_INJECTION_CONFIG,
                "injection occurrence must be a positive integer",
                injection_id=injection_id,
                occurrence=occurrence,
            )
        match: dict[str, Any] = {}
        allowed = {"topic", "batch_seq", "tx_id", "partition", "consumer_id", "offset"}
        for key in allowed:
            if key in entry:
                match[key] = entry[key]
        canonical = canonical_json(
            {
                "action": action,
                "error": error_code,
                "id": injection_id,
                "match": match,
                "occurrence": occurrence,
                "stage": stage,
                "timing": timing,
            }
        )
        result.append(
            Injection(
                injection_id=injection_id,
                stage=stage,
                action=action,
                timing=timing,
                error_code=error_code,
                occurrence=occurrence,
                match=match,
                canonical=canonical,
            )
        )
    result.sort(key=lambda item: item.injection_id)
    return tuple(result)


def load_injection_file(path: str | Path) -> tuple[Injection, ...]:
    try:
        value = read_json(path, "injection manifest")
    except EngineError as exc:
        if exc.code == E_INPUT_JSON:
            raise EngineError(
                E_INJECTION_FILE,
                "cannot load injection manifest",
                details=exc.details,
            ) from exc
        raise
    return validate_injections(value)


def validate_sample(
    sample_path: str | Path,
    *,
    injection_override: str | Path | None = None,
) -> ValidatedSample:
    path = Path(sample_path)
    raw = read_json(path, "sample")
    obj = _require_object(raw, "sample")
    version = obj.get("schema_version", SUPPORTED_SCHEMA_VERSION)
    if version != SUPPORTED_SCHEMA_VERSION:
        raise _bad(E_SCHEMA, "unsupported sample schema", schema_version=version)
    raw_config = _require_object(obj.get("config"), "config")
    max_partitions = _require_int(
        raw_config.get("max_partitions_per_topic"),
        "config.max_partitions_per_topic",
        minimum=1,
        maximum=4096,
    )
    max_batch_messages = _require_int(
        raw_config.get("max_batch_messages"),
        "config.max_batch_messages",
        minimum=1,
        maximum=10000,
    )
    timeout_ms = _require_int(
        raw_config.get("transaction_timeout_ms"),
        "config.transaction_timeout_ms",
        minimum=1,
    )
    retry_budget = _require_int(
        raw_config.get("retry_budget"),
        "config.retry_budget",
        minimum=0,
        maximum=1000,
    )
    clock_start = _require_int(
        raw_config.get("logical_clock_start_ms"),
        "config.logical_clock_start_ms",
        minimum=0,
    )
    injection_manifest = _require_string(
        raw_config.get("injection_manifest"),
        "config.injection_manifest",
        max_length=4096,
    )
    config = EngineConfig(
        max_partitions_per_topic=max_partitions,
        max_batch_messages=max_batch_messages,
        transaction_timeout_ms=timeout_ms,
        retry_budget=retry_budget,
        logical_clock_start_ms=clock_start,
        injection_manifest=injection_manifest,
    )

    raw_topics = obj.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise _bad(E_SCHEMA, "topics must be a non-empty array")
    seen_topics: set[str] = set()
    topics = tuple(_validate_topic(topic, config, seen_topics) for topic in raw_topics)
    topics_by_name = {topic.name: topic for topic in topics}

    raw_batches = obj.get("batches")
    if not isinstance(raw_batches, list) or not raw_batches:
        raise _bad(E_BATCH_FIELD, "batches must be a non-empty array")
    batches: list[Batch] = []
    previous_seq: int | None = None
    previous_clock: int | None = None
    tx_topics: dict[str, str] = {}
    for raw_batch in raw_batches:
        batch = normalize_batch(raw_batch, topics_by_name=topics_by_name, config=config)
        if previous_seq is not None and batch.batch_seq <= previous_seq:
            raise _bad(
                E_BATCH_SEQ,
                "batch_seq must be strictly increasing",
                previous_batch_seq=previous_seq,
                batch_seq=batch.batch_seq,
            )
        if previous_clock is not None and batch.logical_ts_ms < previous_clock:
            raise _bad(
                E_CLOCK_REGRESSION,
                "batch logical timestamps must be nondecreasing",
                previous_logical_ts_ms=previous_clock,
                logical_ts_ms=batch.logical_ts_ms,
                batch_seq=batch.batch_seq,
            )
        if batch.tx_id is not None:
            prior_topic = tx_topics.setdefault(batch.tx_id, batch.topic)
            if prior_topic != batch.topic:
                raise _bad(
                    E_TX_DECLARATION,
                    "one transaction id cannot span multiple topics",
                    transaction_id=batch.tx_id,
                    first_topic=prior_topic,
                    topic=batch.topic,
                    batch_seq=batch.batch_seq,
                )
        previous_seq = batch.batch_seq
        previous_clock = batch.logical_ts_ms
        batches.append(batch)

    injection_path: Path
    if injection_override is not None:
        injection_path = Path(injection_override)
    else:
        candidate = Path(config.injection_manifest)
        injection_path = candidate if candidate.is_absolute() else path.parent / candidate
    injections = load_injection_file(injection_path)
    sample_digest = digest_text(
        canonical_json(
            {
                "batches": [batch.canonical for batch in batches],
                "config": config.as_dict(),
                "topics": [topic.as_dict() for topic in topics],
            }
        )
    )
    return ValidatedSample(
        sample_path=str(path),
        topics=topics,
        batches=tuple(batches),
        config=config,
        injections=injections,
        sample_digest=sample_digest,
    )


def transaction_digest(records: Iterable[dict[str, Any]]) -> str:
    material = [
        {
            "batch_seq": record["batch_seq"],
            "message_index": record["message_index"],
            "payload_digest": record["payload_digest"],
        }
        for record in records
    ]
    material.sort(key=lambda item: (item["batch_seq"], item["message_index"]))
    return digest_text(canonical_json(material))


def transaction_deadline(prepared_at_ms: int, timeout_ms: int) -> int:
    return prepared_at_ms + timeout_ms

