"""Strict parsers and validators for all versioned input formats."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import (
    EVENT_DUPLICATE_SEQUENCE,
    EVENT_OUT_OF_ORDER,
    EVENT_SCHEMA_INVALID,
    LOGICAL_CLOCK_INVALID,
    PARTITION_KEY_INVALID,
    PARTITION_MISSING,
    PATCH_GENERATION_REGRESSION,
    PATCH_SCHEMA_INVALID,
    PATCH_WATERMARK_INVALID,
    SUBSCRIBER_ENDPOINT_INVALID,
    SUBSCRIPTION_DUPLICATE_ID,
    SUBSCRIPTION_SCHEMA_INVALID,
    TOPOLOGY_DUPLICATE_TOPIC,
    TOPOLOGY_INVALID_PARTITION_COUNT,
    TOPOLOGY_INVALID_RULE,
    TOPOLOGY_RULE_UNCOVERED,
    TOPOLOGY_RULE_UNPARSABLE,
    V1_DUPLICATE_MESSAGE_KEY,
    V1_RELATIVE_SECONDS_INVALID,
    V1_SCHEMA_INVALID,
    V2_GENERATION_REGRESSION,
    V2_META_INCONSISTENT,
    V2_SCHEMA_INVALID,
    V2_SHARD_HASH_MISMATCH,
    V2_TOMBSTONE_OPEN,
    InputError,
)
from .util import (
    is_int,
    is_number,
    read_json,
    require_list,
    require_nonempty_string,
    require_nonnegative_int,
    require_object,
    require_positive_int,
    sha256_bytes,
)

TOPOLOGY_SCHEMA = "seqledger-topology/v1"
EVENT_SCHEMA = "seqledger-event/v1"
SUBSCRIPTION_SCHEMA = "seqledger-subscriptions/v1"
V1_SCHEMA = "seqledger-semantics-v1/v1"
V2_MANIFEST_SCHEMA = "seqledger-semantics-v2-manifest/v1"
V2_SHARD_SCHEMA = "seqledger-semantics-v2-shard/v1"
V2_META_SCHEMA = "seqledger-semantics-v2-meta/v1"
PATCH_SCHEMA = "seqledger-semantic-patch/v1"

_PAYLOAD_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


def validate_logical_clock(value: Any, *, path: str = "logical_clock_ms") -> int:
    return require_nonnegative_int(value, path=path, code=LOGICAL_CLOCK_INVALID)


def validate_topology(raw: Any, *, source: str = "<topology>") -> dict[str, Any]:
    document = require_object(raw, path=source, code=TOPOLOGY_INVALID_RULE)
    if document.get("schema") != TOPOLOGY_SCHEMA:
        raise InputError(
            TOPOLOGY_INVALID_RULE,
            "Topology schema is invalid",
            diagnostics={
                "path": source,
                "expected_schema": TOPOLOGY_SCHEMA,
                "actual_schema": document.get("schema"),
            },
        )

    raw_topics = require_list(document.get("topics"), path=f"{source}.topics", code=TOPOLOGY_INVALID_RULE)
    if not raw_topics:
        raise InputError(
            TOPOLOGY_INVALID_RULE,
            "Topology must declare at least one topic",
            diagnostics={"path": f"{source}.topics"},
        )

    names: set[str] = set()
    topics: list[dict[str, Any]] = []
    for index, raw_topic in enumerate(raw_topics):
        path = f"{source}.topics[{index}]"
        topic = require_object(raw_topic, path=path, code=TOPOLOGY_INVALID_RULE)
        name = require_nonempty_string(
            topic.get("name"),
            path=f"{path}.name",
            code=TOPOLOGY_INVALID_RULE,
        )
        if name in names:
            raise InputError(
                TOPOLOGY_DUPLICATE_TOPIC,
                f"Duplicate topic name: {name}",
                diagnostics={"path": f"{path}.name", "topic": name},
            )
        names.add(name)

        partition_count = require_positive_int(
            topic.get("partition_count"),
            path=f"{path}.partition_count",
            code=TOPOLOGY_INVALID_PARTITION_COUNT,
        )
        rule = require_object(
            topic.get("partition_key_rule"),
            path=f"{path}.partition_key_rule",
            code=TOPOLOGY_INVALID_RULE,
        )
        if rule.get("type") != "regex":
            raise InputError(
                TOPOLOGY_INVALID_RULE,
                f"Unsupported partition key rule type for {name}",
                diagnostics={"path": f"{path}.partition_key_rule.type", "value": rule.get("type")},
            )
        pattern = require_nonempty_string(
            rule.get("pattern"),
            path=f"{path}.partition_key_rule.pattern",
            code=TOPOLOGY_INVALID_RULE,
        )
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise InputError(
                TOPOLOGY_RULE_UNPARSABLE,
                f"Partition key rule for {name} cannot be parsed",
                diagnostics={
                    "path": f"{path}.partition_key_rule.pattern",
                    "pattern": pattern,
                    "reason": str(exc),
                },
            ) from exc
        routing = rule.get("routing", "sha256-mod")
        if routing != "sha256-mod":
            raise InputError(
                TOPOLOGY_INVALID_RULE,
                f"Unsupported routing rule for {name}: {routing}",
                diagnostics={"path": f"{path}.partition_key_rule.routing", "value": routing},
            )
        canonical = rule.get("canonical_form", pattern)
        if canonical != pattern:
            raise InputError(
                TOPOLOGY_INVALID_RULE,
                f"Canonical partition-key form for {name} must equal the validated pattern",
                diagnostics={
                    "path": f"{path}.partition_key_rule.canonical_form",
                    "pattern": pattern,
                    "canonical_form": canonical,
                },
            )
        topics.append(
            {
                "name": name,
                "partition_count": partition_count,
                "partition_key_rule": {
                    "type": "regex",
                    "pattern": pattern,
                    "canonical_form": canonical,
                    "routing": routing,
                },
                "_compiled": compiled,
            }
        )

    topics.sort(key=lambda item: item["name"])
    for topic in topics:
        topic.pop("_compiled", None)
    return {"schema": TOPOLOGY_SCHEMA, "topics": topics}


def topology_index(topology: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {topic["name"]: topic for topic in topology["topics"]}


def validate_event_against_topology(
    event: dict[str, Any],
    topics: dict[str, dict[str, Any]],
    *,
    sequence: int,
    path: str,
) -> None:
    topic_name = event.get("topic")
    if not isinstance(topic_name, str) or topic_name not in topics:
        raise InputError(
            PARTITION_MISSING,
            f"Event {sequence} references a topic/partition set that is not declared",
            diagnostics={"path": f"{path}.topic", "event_seq": sequence, "topic": topic_name},
        )
    partition_key = event.get("partition_key")
    if not isinstance(partition_key, str) or not partition_key:
        raise InputError(
            PARTITION_KEY_INVALID,
            f"Event {sequence} has an invalid partition key",
            diagnostics={"path": f"{path}.partition_key", "event_seq": sequence},
        )
    pattern = topics[topic_name]["partition_key_rule"]["pattern"]
    if re.fullmatch(pattern, partition_key) is None:
        raise InputError(
            TOPOLOGY_RULE_UNCOVERED,
            f"Partition key rule for {topic_name} does not cover event {sequence}",
            diagnostics={
                "path": f"{path}.partition_key",
                "event_seq": sequence,
                "topic": topic_name,
                "partition_key": partition_key,
                "pattern": pattern,
            },
        )


def validate_events(
    raw_events: list[Any],
    topology: dict[str, Any],
    *,
    source: str = "<events>",
) -> list[dict[str, Any]]:
    topics = topology_index(topology)
    events: list[dict[str, Any]] = []
    sequence_index: dict[int, int] = {}
    previous_sequence: int | None = None
    for index, raw_event in enumerate(raw_events):
        path = f"{source}[{index}]"
        event = require_object(raw_event, path=path, code=EVENT_SCHEMA_INVALID)
        sequence = require_positive_int(
            event.get("event_seq"),
            path=f"{path}.event_seq",
            code=EVENT_SCHEMA_INVALID,
        )
        if sequence in sequence_index:
            raise InputError(
                EVENT_DUPLICATE_SEQUENCE,
                f"Duplicate event sequence: {sequence}",
                diagnostics={
                    "event_seq": sequence,
                    "first_record": sequence_index[sequence],
                    "duplicate_record": index,
                    "path": f"{path}.event_seq",
                },
            )
        if previous_sequence is not None and sequence <= previous_sequence:
            raise InputError(
                EVENT_OUT_OF_ORDER,
                f"Event sequence {sequence} arrived after {previous_sequence}",
                diagnostics={
                    "event_seq": sequence,
                    "previous_event_seq": previous_sequence,
                    "record": index,
                    "path": f"{path}.event_seq",
                },
            )
        sequence_index[sequence] = index
        previous_sequence = sequence

        validate_event_against_topology(event, topics, sequence=sequence, path=path)
        topic = require_nonempty_string(
            event.get("topic"),
            path=f"{path}.topic",
            code=EVENT_SCHEMA_INVALID,
        )
        partition_key = require_nonempty_string(
            event.get("partition_key"),
            path=f"{path}.partition_key",
            code=PARTITION_KEY_INVALID,
        )
        payload_digest = event.get("payload_digest")
        if not isinstance(payload_digest, str) or _PAYLOAD_DIGEST_RE.fullmatch(payload_digest) is None:
            raise InputError(
                EVENT_SCHEMA_INVALID,
                f"Event {sequence} has an invalid payload digest",
                diagnostics={"path": f"{path}.payload_digest", "event_seq": sequence},
            )
        logical_ts_ms = require_nonnegative_int(
            event.get("logical_ts_ms"),
            path=f"{path}.logical_ts_ms",
            code=LOGICAL_CLOCK_INVALID,
        )
        expected_generation = require_positive_int(
            event.get("expected_delivery_generation"),
            path=f"{path}.expected_delivery_generation",
            code=EVENT_SCHEMA_INVALID,
        )
        events.append(
            {
                "schema": EVENT_SCHEMA,
                "event_seq": sequence,
                "topic": topic,
                "partition_key": partition_key,
                "payload_digest": payload_digest.lower(),
                "logical_ts_ms": logical_ts_ms,
                "expected_delivery_generation": expected_generation,
                "input_position": index,
            }
        )
    if not events:
        raise InputError(
            EVENT_SCHEMA_INVALID,
            "Producer event stream is empty",
            diagnostics={"path": source},
        )
    return events


def validate_subscriptions(
    raw: Any,
    topology: dict[str, Any],
    *,
    source: str = "<subscriptions>",
) -> list[dict[str, Any]]:
    document = require_object(raw, path=source, code=SUBSCRIPTION_SCHEMA_INVALID)
    if document.get("schema") != SUBSCRIPTION_SCHEMA:
        raise InputError(
            SUBSCRIPTION_SCHEMA_INVALID,
            "Subscription schema is invalid",
            diagnostics={
                "path": source,
                "expected_schema": SUBSCRIPTION_SCHEMA,
                "actual_schema": document.get("schema"),
            },
        )
    topics = topology_index(topology)
    raw_subscriptions = require_list(
        document.get("subscriptions"),
        path=f"{source}.subscriptions",
        code=SUBSCRIPTION_SCHEMA_INVALID,
    )
    if not raw_subscriptions:
        raise InputError(
            SUBSCRIPTION_SCHEMA_INVALID,
            "At least one subscriber declaration is required",
            diagnostics={"path": f"{source}.subscriptions"},
        )
    ids: set[str] = set()
    subscriptions: list[dict[str, Any]] = []
    for index, raw_subscription in enumerate(raw_subscriptions):
        path = f"{source}.subscriptions[{index}]"
        subscription = require_object(raw_subscription, path=path, code=SUBSCRIPTION_SCHEMA_INVALID)
        subscriber_id = require_nonempty_string(
            subscription.get("subscriber_id"),
            path=f"{path}.subscriber_id",
            code=SUBSCRIPTION_SCHEMA_INVALID,
        )
        if subscriber_id in ids:
            raise InputError(
                SUBSCRIPTION_DUPLICATE_ID,
                f"Duplicate subscriber ID: {subscriber_id}",
                diagnostics={"path": f"{path}.subscriber_id", "subscriber_id": subscriber_id},
            )
        ids.add(subscriber_id)
        topic = require_nonempty_string(
            subscription.get("topic"),
            path=f"{path}.topic",
            code=SUBSCRIPTION_SCHEMA_INVALID,
        )
        if topic not in topics:
            raise InputError(
                PARTITION_MISSING,
                f"Subscription references unknown topic: {topic}",
                diagnostics={"path": f"{path}.topic", "topic": topic},
            )
        batch_size = require_positive_int(
            subscription.get("batch_size"),
            path=f"{path}.batch_size",
            code=SUBSCRIPTION_SCHEMA_INVALID,
        )
        endpoint = require_nonempty_string(
            subscription.get("callback_endpoint"),
            path=f"{path}.callback_endpoint",
            code=SUBSCRIBER_ENDPOINT_INVALID,
        )
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise InputError(
                SUBSCRIBER_ENDPOINT_INVALID,
                f"Subscriber {subscriber_id} must use an unauthenticated loopback endpoint",
                diagnostics={
                    "path": f"{path}.callback_endpoint",
                    "subscriber_id": subscriber_id,
                    "callback_endpoint": endpoint,
                },
            )
        max_rate = subscription.get("max_rate_per_sec")
        if not is_number(max_rate) or float(max_rate) <= 0:
            raise InputError(
                SUBSCRIPTION_SCHEMA_INVALID,
                f"Subscriber {subscriber_id} has an invalid consumption rate",
                diagnostics={"path": f"{path}.max_rate_per_sec", "value": max_rate},
            )
        subscriptions.append(
            {
                "schema": SUBSCRIPTION_SCHEMA,
                "subscriber_id": subscriber_id,
                "topic": topic,
                "batch_size": batch_size,
                "callback_endpoint": endpoint,
                "max_rate_per_sec": max_rate,
            }
        )
    subscriptions.sort(key=lambda item: (item["topic"], item["subscriber_id"]))
    return subscriptions


def validate_v1(raw: Any, *, source: str = "<semantics-v1>") -> dict[str, Any]:
    document = require_object(raw, path=source, code=V1_SCHEMA_INVALID)
    if document.get("schema") != V1_SCHEMA:
        raise InputError(
            V1_SCHEMA_INVALID,
            "v1 semantics snapshot schema is invalid",
            diagnostics={
                "path": source,
                "expected_schema": V1_SCHEMA,
                "actual_schema": document.get("schema"),
            },
        )
    logical_clock_ms = require_nonnegative_int(
        document.get("logical_clock_ms"),
        path=f"{source}.logical_clock_ms",
        code=LOGICAL_CLOCK_INVALID,
    )
    scale = document.get("ms_per_second", 1000)
    if not is_int(scale) or scale <= 0:
        raise InputError(
            V1_SCHEMA_INVALID,
            "v1 conversion scale must be a positive integer",
            diagnostics={"path": f"{source}.ms_per_second", "value": scale},
        )
    rounding = document.get("rounding", "floor")
    if rounding != "floor":
        raise InputError(
            V1_SCHEMA_INVALID,
            "Only deterministic floor rounding is supported for v1 conversion",
            diagnostics={"path": f"{source}.rounding", "value": rounding},
        )
    raw_entries = require_list(document.get("entries"), path=f"{source}.entries", code=V1_SCHEMA_INVALID)
    if not raw_entries:
        raise InputError(
            V1_SCHEMA_INVALID,
            "v1 semantics snapshot must contain at least one entry",
            diagnostics={"path": f"{source}.entries"},
        )
    keys: set[str] = set()
    entries: list[dict[str, Any]] = []
    for index, raw_entry in enumerate(raw_entries):
        path = f"{source}.entries[{index}]"
        entry = require_object(raw_entry, path=path, code=V1_SCHEMA_INVALID)
        message_key = require_nonempty_string(
            entry.get("message_key"),
            path=f"{path}.message_key",
            code=V1_SCHEMA_INVALID,
        )
        if message_key in keys:
            raise InputError(
                V1_DUPLICATE_MESSAGE_KEY,
                f"Duplicate v1 message key: {message_key}",
                diagnostics={"path": f"{path}.message_key", "message_key": message_key},
            )
        keys.add(message_key)
        relative_seconds = entry.get("relative_effective_seconds")
        if not is_int(relative_seconds) or relative_seconds < 0:
            raise InputError(
                V1_RELATIVE_SECONDS_INVALID,
                f"v1 entry {message_key} has invalid relative effective seconds",
                diagnostics={
                    "path": f"{path}.relative_effective_seconds",
                    "message_key": message_key,
                    "value": relative_seconds,
                },
            )
        alias = require_nonempty_string(
            entry.get("delivery_alias"),
            path=f"{path}.delivery_alias",
            code=V1_SCHEMA_INVALID,
        )
        generation = entry.get("semantic_generation", 1)
        if not is_int(generation) or generation <= 0:
            raise InputError(
                V1_SCHEMA_INVALID,
                f"v1 entry {message_key} has an invalid semantic generation",
                diagnostics={"path": f"{path}.semantic_generation", "value": generation},
            )
        topic = entry.get("topic")
        if topic is not None and not isinstance(topic, str):
            raise InputError(
                V1_SCHEMA_INVALID,
                f"v1 entry {message_key} has an invalid topic",
                diagnostics={"path": f"{path}.topic"},
            )
        digest = entry.get("payload_digest")
        if digest is not None and (
            not isinstance(digest, str) or _PAYLOAD_DIGEST_RE.fullmatch(digest) is None
        ):
            raise InputError(
                V1_SCHEMA_INVALID,
                f"v1 entry {message_key} has an invalid payload digest",
                diagnostics={"path": f"{path}.payload_digest"},
            )
        entries.append(
            {
                "message_key": message_key,
                "topic": topic,
                "delivery_alias": alias,
                "relative_effective_seconds": relative_seconds,
                "semantic_generation": generation,
                "payload_digest": digest.lower() if isinstance(digest, str) else None,
                "logical_clock_ms": logical_clock_ms,
            }
        )
    entries.sort(key=lambda item: item["message_key"])
    return {
        "schema": V1_SCHEMA,
        "logical_clock_ms": logical_clock_ms,
        "ms_per_second": scale,
        "rounding": rounding,
        "entries": entries,
    }


def _read_meta_database(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        raise InputError(
            "INPUT_FILE_MISSING",
            f"v2 metadata database does not exist: {source}",
            diagnostics={"path": str(source)},
        )
    try:
        connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        metadata = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM meta").fetchall()
        }
        semantics = [
            dict(row)
            for row in connection.execute(
                "SELECT message_key, topic, delivery_alias, absolute_effective_ms, "
                "semantic_generation, shard_id, payload_digest FROM semantics"
            ).fetchall()
        ]
        watermarks = [
            dict(row)
            for row in connection.execute(
                "SELECT message_key, topic, partition, upstream_offset FROM watermarks"
            ).fetchall()
        ]
        tombstones = [
            dict(row)
            for row in connection.execute(
                "SELECT tombstone_id, message_key, reason, semantic_generation FROM tombstones"
            ).fetchall()
        ]
    except sqlite3.Error as exc:
        raise InputError(
            V2_META_INCONSISTENT,
            "v2 metadata database is unreadable or has an invalid schema",
            diagnostics={"path": str(source), "reason": str(exc)},
        ) from exc
    finally:
        if "connection" in locals():
            connection.close()
    return {
        "meta": metadata,
        "semantics": semantics,
        "watermarks": watermarks,
        "tombstones": tombstones,
    }


def validate_v2(
    manifest_raw: Any,
    manifest_source: str,
    *,
    meta_path: str | Path,
) -> dict[str, Any]:
    manifest = require_object(manifest_raw, path=manifest_source, code=V2_SCHEMA_INVALID)
    if manifest.get("schema") != V2_MANIFEST_SCHEMA:
        raise InputError(
            V2_SCHEMA_INVALID,
            "v2 shard manifest schema is invalid",
            diagnostics={
                "path": manifest_source,
                "expected_schema": V2_MANIFEST_SCHEMA,
                "actual_schema": manifest.get("schema"),
            },
        )
    manifest_dir = Path(manifest_source).resolve().parent
    raw_shards = require_list(
        manifest.get("shards"),
        path=f"{manifest_source}.shards",
        code=V2_SCHEMA_INVALID,
    )
    if not raw_shards:
        raise InputError(
            V2_SCHEMA_INVALID,
            "v2 shard manifest must contain at least one shard",
            diagnostics={"path": f"{manifest_source}.shards"},
        )

    shard_ids: set[str] = set()
    message_keys: set[str] = set()
    previous_generation: int | None = None
    shards: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for index, raw_shard in enumerate(raw_shards):
        path = f"{manifest_source}.shards[{index}]"
        shard_ref = require_object(raw_shard, path=path, code=V2_SCHEMA_INVALID)
        shard_id = require_nonempty_string(
            shard_ref.get("shard_id"),
            path=f"{path}.shard_id",
            code=V2_SCHEMA_INVALID,
        )
        if shard_id in shard_ids:
            raise InputError(
                V2_SCHEMA_INVALID,
                f"Duplicate v2 shard ID: {shard_id}",
                diagnostics={"path": f"{path}.shard_id", "shard_id": shard_id},
            )
        shard_ids.add(shard_id)
        generation = require_positive_int(
            shard_ref.get("semantic_generation"),
            path=f"{path}.semantic_generation",
            code=V2_SCHEMA_INVALID,
        )
        if previous_generation is not None and generation < previous_generation:
            raise InputError(
                V2_GENERATION_REGRESSION,
                f"v2 shard generation regressed at {shard_id}",
                diagnostics={
                    "shard_id": shard_id,
                    "event": "v2 shard manifest",
                    "previous_generation": previous_generation,
                    "semantic_generation": generation,
                    "path": f"{path}.semantic_generation",
                },
            )
        previous_generation = generation
        logical_clock_ms = require_nonnegative_int(
            shard_ref.get("logical_clock_ms"),
            path=f"{path}.logical_clock_ms",
            code=LOGICAL_CLOCK_INVALID,
        )
        relative_path = require_nonempty_string(
            shard_ref.get("path"),
            path=f"{path}.path",
            code=V2_SCHEMA_INVALID,
        )
        shard_path = (manifest_dir / relative_path).resolve()
        try:
            shard_bytes = shard_path.read_bytes()
        except OSError as exc:
            raise InputError(
                "INPUT_FILE_UNREADABLE",
                f"Cannot read v2 shard: {shard_path}",
                diagnostics={"path": str(shard_path), "reason": str(exc)},
            ) from exc
        actual_hash = sha256_bytes(shard_bytes)
        expected_hash = require_nonempty_string(
            shard_ref.get("sha256"),
            path=f"{path}.sha256",
            code=V2_SCHEMA_INVALID,
        ).lower()
        if _SHA256_RE.fullmatch(expected_hash) is None:
            raise InputError(
                V2_SCHEMA_INVALID,
                f"v2 shard hash is not a SHA-256 hex digest: {shard_id}",
                diagnostics={"path": f"{path}.sha256", "shard_id": shard_id},
            )
        if actual_hash != expected_hash:
            raise InputError(
                V2_SHARD_HASH_MISMATCH,
                f"v2 shard hash mismatch: {shard_id}",
                diagnostics={
                    "shard_id": shard_id,
                    "path": str(shard_path),
                    "expected_sha256": expected_hash,
                    "actual_sha256": actual_hash,
                },
            )
        try:
            shard_document = read_json(shard_path)
        except InputError:
            raise
        shard_document = require_object(
            shard_document,
            path=str(shard_path),
            code=V2_SCHEMA_INVALID,
        )
        if shard_document.get("schema") != V2_SHARD_SCHEMA:
            raise InputError(
                V2_SCHEMA_INVALID,
                f"v2 shard {shard_id} has an invalid schema",
                diagnostics={
                    "path": str(shard_path),
                    "expected_schema": V2_SHARD_SCHEMA,
                    "actual_schema": shard_document.get("schema"),
                    "shard_id": shard_id,
                },
            )
        if shard_document.get("shard_id") != shard_id:
            raise InputError(
                V2_META_INCONSISTENT,
                f"v2 shard identity mismatch for {shard_id}",
                diagnostics={
                    "manifest_shard_id": shard_id,
                    "shard_document_id": shard_document.get("shard_id"),
                    "path": str(shard_path),
                },
            )
        if shard_document.get("semantic_generation") != generation:
            raise InputError(
                V2_META_INCONSISTENT,
                f"v2 shard generation mismatch for {shard_id}",
                diagnostics={
                    "shard_id": shard_id,
                    "manifest_generation": generation,
                    "shard_generation": shard_document.get("semantic_generation"),
                },
            )
        if shard_document.get("logical_clock_ms") != logical_clock_ms:
            raise InputError(
                V2_META_INCONSISTENT,
                f"v2 shard logical clock mismatch for {shard_id}",
                diagnostics={
                    "shard_id": shard_id,
                    "manifest_logical_clock_ms": logical_clock_ms,
                    "shard_logical_clock_ms": shard_document.get("logical_clock_ms"),
                },
            )
        raw_entries = require_list(
            shard_document.get("entries"),
            path=f"{shard_path}.entries",
            code=V2_SCHEMA_INVALID,
        )
        for entry_index, raw_entry in enumerate(raw_entries):
            entry_path = f"{shard_path}.entries[{entry_index}]"
            entry = require_object(raw_entry, path=entry_path, code=V2_SCHEMA_INVALID)
            message_key = require_nonempty_string(
                entry.get("message_key"),
                path=f"{entry_path}.message_key",
                code=V2_SCHEMA_INVALID,
            )
            if message_key in message_keys:
                raise InputError(
                    V2_SCHEMA_INVALID,
                    f"Duplicate v2 message key across shards: {message_key}",
                    diagnostics={
                        "message_key": message_key,
                        "shard_id": shard_id,
                        "path": f"{entry_path}.message_key",
                    },
                )
            message_keys.add(message_key)
            absolute_effective_ms = require_nonnegative_int(
                entry.get("absolute_effective_ms"),
                path=f"{entry_path}.absolute_effective_ms",
                code=LOGICAL_CLOCK_INVALID,
            )
            entry_generation = require_positive_int(
                entry.get("semantic_generation"),
                path=f"{entry_path}.semantic_generation",
                code=V2_SCHEMA_INVALID,
            )
            if entry_generation != generation:
                raise InputError(
                    V2_META_INCONSISTENT,
                    f"v2 entry generation mismatch for {message_key}",
                    diagnostics={
                        "message_key": message_key,
                        "shard_generation": generation,
                        "entry_generation": entry_generation,
                    },
                )
            alias = require_nonempty_string(
                entry.get("delivery_alias"),
                path=f"{entry_path}.delivery_alias",
                code=V2_SCHEMA_INVALID,
            )
            topic = entry.get("topic")
            if topic is not None and not isinstance(topic, str):
                raise InputError(
                    V2_SCHEMA_INVALID,
                    f"v2 entry {message_key} has an invalid topic",
                    diagnostics={"path": f"{entry_path}.topic"},
                )
            digest = entry.get("payload_digest")
            if digest is not None and (
                not isinstance(digest, str)
                or _PAYLOAD_DIGEST_RE.fullmatch(digest) is None
            ):
                raise InputError(
                    V2_SCHEMA_INVALID,
                    f"v2 entry {message_key} has an invalid payload digest",
                    diagnostics={"path": f"{entry_path}.payload_digest"},
                )
            tombstone_ref = entry.get("tombstone_ref")
            if tombstone_ref is not None and not isinstance(tombstone_ref, str):
                raise InputError(
                    V2_SCHEMA_INVALID,
                    f"v2 entry {message_key} has an invalid tombstone reference",
                    diagnostics={"path": f"{entry_path}.tombstone_ref"},
                )
            entries.append(
                {
                    "message_key": message_key,
                    "topic": topic,
                    "delivery_alias": alias,
                    "absolute_effective_ms": absolute_effective_ms,
                    "semantic_generation": entry_generation,
                    "payload_digest": digest.lower() if isinstance(digest, str) else None,
                    "tombstone_ref": tombstone_ref,
                    "source_version": "v2",
                    "source_shard_id": shard_id,
                    "logical_clock_ms": logical_clock_ms,
                }
            )
        shards.append(
            {
                "shard_id": shard_id,
                "path": str(shard_path),
                "sha256": actual_hash,
                "semantic_generation": generation,
                "logical_clock_ms": logical_clock_ms,
                "entry_count": len(raw_entries),
            }
        )

    meta = _read_meta_database(meta_path)
    if meta["meta"].get("schema") != V2_META_SCHEMA:
        raise InputError(
            V2_META_INCONSISTENT,
            "v2 metadata schema marker is invalid",
            diagnostics={"path": str(meta_path), "meta": meta["meta"]},
        )
    current_generation_text = meta["meta"].get("current_generation")
    try:
        current_generation = int(current_generation_text or "")
    except ValueError as exc:
        raise InputError(
            V2_META_INCONSISTENT,
            "v2 metadata current_generation is invalid",
            diagnostics={"path": str(meta_path), "value": current_generation_text},
        ) from exc
    maximum_shard_generation = max(shard["semantic_generation"] for shard in shards)
    if current_generation != maximum_shard_generation:
        raise InputError(
            V2_META_INCONSISTENT,
            "v2 metadata current generation does not match shard generations",
            diagnostics={
                "path": str(meta_path),
                "current_generation": current_generation,
                "maximum_shard_generation": maximum_shard_generation,
            },
        )

    meta_semantics = {row["message_key"]: row for row in meta["semantics"]}
    shard_semantics = {entry["message_key"]: entry for entry in entries}
    if set(meta_semantics) != set(shard_semantics):
        raise InputError(
            V2_META_INCONSISTENT,
            "v2 metadata semantic rows do not close against shard entries",
            diagnostics={
                "meta_only": sorted(set(meta_semantics) - set(shard_semantics)),
                "shard_only": sorted(set(shard_semantics) - set(meta_semantics)),
                "path": str(meta_path),
            },
        )
    for message_key, entry in shard_semantics.items():
        row = meta_semantics[message_key]
        expected_values = {
            "absolute_effective_ms": entry["absolute_effective_ms"],
            "semantic_generation": entry["semantic_generation"],
            "shard_id": entry["source_shard_id"],
            "delivery_alias": entry["delivery_alias"],
            "topic": entry["topic"],
            "payload_digest": entry["payload_digest"],
        }
        mismatches = {
            field: {"meta": row.get(field), "shard": expected}
            for field, expected in expected_values.items()
            if row.get(field) != expected
        }
        if mismatches:
            raise InputError(
                V2_META_INCONSISTENT,
                f"v2 metadata row differs from shard entry for {message_key}",
                diagnostics={"message_key": message_key, "mismatches": mismatches},
            )

    watermarks = {row["message_key"]: row for row in meta["watermarks"]}
    if not set(watermarks).issubset(shard_semantics):
        raise InputError(
            V2_META_INCONSISTENT,
            "v2 upstream watermark references an unknown message key",
            diagnostics={
                "unknown_message_keys": sorted(set(watermarks) - set(shard_semantics)),
                "path": str(meta_path),
            },
        )
    for message_key, row in watermarks.items():
        partition = row.get("partition")
        upstream_offset = row.get("upstream_offset")
        if (
            not isinstance(row.get("topic"), str)
            or not row["topic"]
            or not is_int(partition)
            or partition < 0
            or not is_int(upstream_offset)
            or upstream_offset < 0
        ):
            raise InputError(
                V2_META_INCONSISTENT,
                f"v2 upstream watermark is malformed for {message_key}",
                diagnostics={"message_key": message_key, "row": row},
            )

    tombstones = {row["tombstone_id"]: row for row in meta["tombstones"]}
    if len(tombstones) != len(meta["tombstones"]):
        raise InputError(
            V2_TOMBSTONE_OPEN,
            "v2 tombstones contain duplicate IDs",
            diagnostics={"path": str(meta_path)},
        )
    referenced: set[str] = set()
    for entry in entries:
        tombstone_ref = entry["tombstone_ref"]
        if tombstone_ref is None:
            continue
        if tombstone_ref not in tombstones:
            raise InputError(
                V2_TOMBSTONE_OPEN,
                f"v2 tombstone reference is open: {tombstone_ref}",
                diagnostics={
                    "message_key": entry["message_key"],
                    "tombstone_ref": tombstone_ref,
                    "path": str(meta_path),
                },
            )
        referenced.add(tombstone_ref)
    for tombstone_id, row in tombstones.items():
        if tombstone_id not in referenced:
            raise InputError(
                V2_TOMBSTONE_OPEN,
                f"v2 tombstone is not referenced by a semantic entry: {tombstone_id}",
                diagnostics={"tombstone_id": tombstone_id, "path": str(meta_path)},
            )
        if row.get("message_key") not in shard_semantics:
            raise InputError(
                V2_TOMBSTONE_OPEN,
                f"v2 tombstone targets an unknown message key: {tombstone_id}",
                diagnostics={
                    "tombstone_id": tombstone_id,
                    "message_key": row.get("message_key"),
                    "path": str(meta_path),
                },
            )
        if (
            not isinstance(row.get("reason"), str)
            or not row["reason"]
            or not is_int(row.get("semantic_generation"))
            or row["semantic_generation"] < 1
            or row["semantic_generation"] > current_generation
        ):
            raise InputError(
                V2_TOMBSTONE_OPEN,
                f"v2 tombstone payload is malformed: {tombstone_id}",
                diagnostics={"tombstone_id": tombstone_id, "row": row},
            )

    for entry in entries:
        entry["dependency"] = None
        row = watermarks.get(entry["message_key"])
        if row is not None:
            entry["dependency"] = {
                "topic": row["topic"],
                "partition": row["partition"],
                "upstream_offset": row["upstream_offset"],
            }
        tombstone_ref = entry["tombstone_ref"]
        entry["tombstone"] = (
            dict(tombstones[tombstone_ref]) if tombstone_ref is not None else None
        )
    entries.sort(key=lambda item: item["message_key"])
    tombstones_list = sorted(
        (dict(row) for row in meta["tombstones"]),
        key=lambda row: row["tombstone_id"],
    )
    return {
        "schema": V2_MANIFEST_SCHEMA,
        "meta_schema": V2_META_SCHEMA,
        "manifest_source": str(Path(manifest_source).resolve()),
        "meta_path": str(Path(meta_path).resolve()),
        "current_generation": current_generation,
        "shards": sorted(shards, key=lambda item: item["shard_id"]),
        "entries": entries,
        "watermarks": sorted(
            (dict(row) for row in meta["watermarks"]),
            key=lambda row: row["message_key"],
        ),
        "tombstones": tombstones_list,
    }


def validate_patch(
    raw: Any,
    *,
    current_v2_generation: int,
) -> dict[str, Any]:
    document = require_object(raw, path="<patch>", code=PATCH_SCHEMA_INVALID)
    if document.get("schema") != PATCH_SCHEMA:
        raise InputError(
            PATCH_SCHEMA_INVALID,
            "Semantic patch schema is invalid",
            diagnostics={
                "expected_schema": PATCH_SCHEMA,
                "actual_schema": document.get("schema"),
            },
        )
    patch_id = require_nonempty_string(
        document.get("patch_id"),
        path="patch.patch_id",
        code=PATCH_SCHEMA_INVALID,
    )
    generation = require_positive_int(
        document.get("semantic_generation"),
        path="patch.semantic_generation",
        code=PATCH_SCHEMA_INVALID,
    )
    if generation < current_v2_generation:
        raise InputError(
            PATCH_GENERATION_REGRESSION,
            "Semantic patch generation would regress the v2 pointer",
            diagnostics={
                "patch_id": patch_id,
                "current_v2_generation": current_v2_generation,
                "patch_generation": generation,
            },
        )
    logical_clock_ms = require_nonnegative_int(
        document.get("logical_clock_ms"),
        path="patch.logical_clock_ms",
        code=LOGICAL_CLOCK_INVALID,
    )
    updates = require_list(document.get("updates", []), path="patch.updates", code=PATCH_SCHEMA_INVALID)
    watermark_updates = require_list(
        document.get("upstream_watermarks", []),
        path="patch.upstream_watermarks",
        code=PATCH_SCHEMA_INVALID,
    )
    tombstones = require_list(
        document.get("tombstones", []),
        path="patch.tombstones",
        code=PATCH_SCHEMA_INVALID,
    )
    if not updates and not watermark_updates and not tombstones:
        raise InputError(
            PATCH_SCHEMA_INVALID,
            "Semantic patch is empty",
            diagnostics={"patch_id": patch_id},
        )

    normalized_updates: list[dict[str, Any]] = []
    update_keys: set[str] = set()
    for index, raw_update in enumerate(updates):
        path = f"patch.updates[{index}]"
        update = require_object(raw_update, path=path, code=PATCH_SCHEMA_INVALID)
        key = require_nonempty_string(
            update.get("message_key"),
            path=f"{path}.message_key",
            code=PATCH_SCHEMA_INVALID,
        )
        if key in update_keys:
            raise InputError(
                PATCH_SCHEMA_INVALID,
                f"Duplicate patch update for message key: {key}",
                diagnostics={"message_key": key},
            )
        update_keys.add(key)
        absolute_ms = require_nonnegative_int(
            update.get("absolute_effective_ms"),
            path=f"{path}.absolute_effective_ms",
            code=LOGICAL_CLOCK_INVALID,
        )
        alias = require_nonempty_string(
            update.get("delivery_alias"),
            path=f"{path}.delivery_alias",
            code=PATCH_SCHEMA_INVALID,
        )
        update_generation = require_positive_int(
            update.get("semantic_generation", generation),
            path=f"{path}.semantic_generation",
            code=PATCH_SCHEMA_INVALID,
        )
        if update_generation < current_v2_generation:
            raise InputError(
                PATCH_GENERATION_REGRESSION,
                f"Patch update for {key} regresses the semantic generation",
                diagnostics={
                    "message_key": key,
                    "current_v2_generation": current_v2_generation,
                    "semantic_generation": update_generation,
                },
            )
        normalized_updates.append(
            {
                "message_key": key,
                "absolute_effective_ms": absolute_ms,
                "delivery_alias": alias,
                "semantic_generation": update_generation,
            }
        )

    normalized_watermarks: list[dict[str, Any]] = []
    for index, raw_watermark in enumerate(watermark_updates):
        path = f"patch.upstream_watermarks[{index}]"
        watermark = require_object(raw_watermark, path=path, code=PATCH_SCHEMA_INVALID)
        key = require_nonempty_string(
            watermark.get("message_key"),
            path=f"{path}.message_key",
            code=PATCH_SCHEMA_INVALID,
        )
        topic = require_nonempty_string(
            watermark.get("topic"),
            path=f"{path}.topic",
            code=PATCH_WATERMARK_INVALID,
        )
        partition = require_nonnegative_int(
            watermark.get("partition"),
            path=f"{path}.partition",
            code=PATCH_WATERMARK_INVALID,
        )
        upstream_offset = require_nonnegative_int(
            watermark.get("upstream_offset"),
            path=f"{path}.upstream_offset",
            code=PATCH_WATERMARK_INVALID,
        )
        normalized_watermarks.append(
            {
                "message_key": key,
                "topic": topic,
                "partition": partition,
                "upstream_offset": upstream_offset,
            }
        )

    normalized_tombstones: list[dict[str, Any]] = []
    tombstone_ids: set[str] = set()
    for index, raw_tombstone in enumerate(tombstones):
        path = f"patch.tombstones[{index}]"
        tombstone = require_object(raw_tombstone, path=path, code=PATCH_SCHEMA_INVALID)
        tombstone_id = require_nonempty_string(
            tombstone.get("tombstone_id"),
            path=f"{path}.tombstone_id",
            code=PATCH_SCHEMA_INVALID,
        )
        if tombstone_id in tombstone_ids:
            raise InputError(
                PATCH_SCHEMA_INVALID,
                f"Duplicate patch tombstone ID: {tombstone_id}",
                diagnostics={"tombstone_id": tombstone_id},
            )
        tombstone_ids.add(tombstone_id)
        key = require_nonempty_string(
            tombstone.get("message_key"),
            path=f"{path}.message_key",
            code=PATCH_SCHEMA_INVALID,
        )
        reason = require_nonempty_string(
            tombstone.get("reason"),
            path=f"{path}.reason",
            code=PATCH_SCHEMA_INVALID,
        )
        tombstone_generation = require_positive_int(
            tombstone.get("semantic_generation", generation),
            path=f"{path}.semantic_generation",
            code=PATCH_SCHEMA_INVALID,
        )
        if tombstone_generation < current_v2_generation:
            raise InputError(
                PATCH_GENERATION_REGRESSION,
                f"Patch tombstone for {key} regresses the semantic generation",
                diagnostics={
                    "tombstone_id": tombstone_id,
                    "current_v2_generation": current_v2_generation,
                    "semantic_generation": tombstone_generation,
                },
            )
        if tombstone_generation > generation:
            raise InputError(
                PATCH_SCHEMA_INVALID,
                f"Patch tombstone generation for {key} exceeds the patch generation",
                diagnostics={
                    "tombstone_id": tombstone_id,
                    "patch_generation": generation,
                    "semantic_generation": tombstone_generation,
                },
            )
        normalized_tombstones.append(
            {
                "tombstone_id": tombstone_id,
                "message_key": key,
                "reason": reason,
                "semantic_generation": tombstone_generation,
            }
        )

    return {
        "schema": PATCH_SCHEMA,
        "patch_id": patch_id,
        "semantic_generation": generation,
        "logical_clock_ms": logical_clock_ms,
        "updates": normalized_updates,
        "upstream_watermarks": normalized_watermarks,
        "tombstones": normalized_tombstones,
    }


# Avoid importing a non-existent generic code by defining a local fallback in
# one place; callers use the specific constants above for diagnostics.
