"""Parsing, validation, and two-generation merge."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import BusinessError, err
from .expressions import validate_filter, validate_transforms
from .models import (
    BridgeDef,
    ChannelDef,
    Config,
    FieldDef,
    LoadedDataset,
    SubscriptionDef,
    UnifiedEvent,
)
from .util import (
    canonical_json,
    digest_value,
    ensure_json_value,
    is_int,
    payload_digest,
    read_json,
    typed_value,
)

_FIELD_TYPES = {"string", "number", "integer", "boolean", "object", "any"}
_SOURCE_PRIORITY = {"v1": 1, "v2": 2}


@dataclass
class _EventSource:
    sequence: int
    logical_clock: int
    produced_at_ms: int
    fields: dict[str, Any]
    tombstone: bool
    source_version: str
    generation: int
    raw: dict[str, Any]
    time_conversion: dict[str, Any] | None = None
    field_checksum: str = ""


@dataclass
class _ChannelSource:
    canonical_name: str
    generation: int
    partitions: int
    field_schema: list[FieldDef]
    aliases: list[str]
    source_version: str
    events: list[_EventSource] = field(default_factory=list)


@dataclass
class _BridgeSource:
    bridge_id: str
    source: str
    target: str
    source_generation: int | None
    target_generation: int | None
    min_upstream_watermark: int
    filter: dict[str, Any]
    transforms: list[dict[str, Any]]
    max_batch: int
    source_version: str


@dataclass
class _SubscriptionSource:
    name: str
    channel: str
    start_offset: int
    source_version: str


def load_dataset(
    v1_path: str | Path,
    v2_shards_dir: str | Path,
    v2_metadata_path: str | Path,
    *,
    retention_bytes: int | None = None,
    clock_tolerance_ms: int | None = None,
) -> LoadedDataset:
    v1_path = Path(v1_path)
    shards_dir = Path(v2_shards_dir)
    metadata_path = Path(v2_metadata_path)

    v1_document = read_json(v1_path)
    v1_channels, v1_bridges, v1_subscriptions, epoch_ms, v1_retention, v1_tolerance = (
        _parse_v1(v1_document)
    )
    metadata = _read_v2_metadata(metadata_path)
    shard_paths = sorted(shards_dir.glob("*.json"), key=lambda item: item.name)
    if not shard_paths:
        raise err("MALFORMED_EVENT", "v2 分片目录为空", path=str(shards_dir))
    shard_documents = [(path, read_json(path)) for path in shard_paths]

    fingerprint = _source_fingerprint(v1_path, metadata_path, shard_paths)
    (
        v2_channels,
        v2_bridges,
        v2_subscriptions,
        v2_retention,
        v2_tolerance,
    ) = _parse_v2(metadata, shard_documents)

    channels, v2_field_names, alias_map = _merge_channel_definitions(
        v1_channels, v2_channels
    )
    events, merge_decisions, degradations = _merge_events(
        channels, v2_field_names, v1_channels, v2_channels
    )
    bridges = _merge_bridges(
        v1_bridges, v2_bridges, channels, alias_map
    )
    subscriptions = _merge_subscriptions(
        v1_subscriptions, v2_subscriptions, channels, alias_map, events
    )

    retention = (
        retention_bytes
        if retention_bytes is not None
        else v2_retention
        if v2_retention is not None
        else v1_retention
        if v1_retention is not None
        else 4096
    )
    tolerance = (
        clock_tolerance_ms
        if clock_tolerance_ms is not None
        else v2_tolerance
        if v2_tolerance is not None
        else v1_tolerance
        if v1_tolerance is not None
        else 1000
    )
    if not is_int(retention) or retention <= 0:
        raise err("PATCH_INVALID", "retention_bytes 必须为正整数", value=retention)
    if not is_int(tolerance) or tolerance < 0:
        raise err("PATCH_INVALID", "clock_tolerance_ms 必须为非负整数", value=tolerance)

    config = Config(
        channels=channels,
        bridges=bridges,
        subscriptions=subscriptions,
        retention_bytes=retention,
        clock_tolerance_ms=tolerance,
        source_fingerprint=fingerprint,
        base_epoch_ms=epoch_ms,
    )
    return LoadedDataset(
        config=config,
        events=events,
        merge_decisions=merge_decisions,
        degradations=degradations,
        source_fingerprint=fingerprint,
    )


def _parse_v1(
    document: Any,
) -> tuple[
    list[_ChannelSource],
    list[_BridgeSource],
    list[_SubscriptionSource],
    int,
    int | None,
    int | None,
]:
    if not isinstance(document, dict) or document.get("format") != "eventbus-v1":
        raise err("INVALID_FORMAT", "v1 文档 format 必须为 eventbus-v1")
    epoch_ms = document.get("epoch_ms")
    if not is_int(epoch_ms) or epoch_ms < 0:
        raise err("INVALID_FORMAT", "v1 epoch_ms 必须为非负整数")
    raw_channels = document.get("channels")
    if not isinstance(raw_channels, list):
        raise err("INVALID_FORMAT", "v1 channels 必须为数组")

    channels: list[_ChannelSource] = []
    subscriptions: list[_SubscriptionSource] = []
    seen_names: set[str] = set()
    seen_canonical: set[str] = set()
    for channel_index, raw in enumerate(raw_channels):
        path = f"channels[{channel_index}]"
        if not isinstance(raw, dict):
            raise err("INVALID_FORMAT", "v1 通道必须为对象", path=path)
        name = _nonempty_string(raw.get("name"), f"{path}.name", "CHANNEL_DUPLICATE")
        canonical = raw.get("canonical_name", name)
        canonical = _nonempty_string(
            canonical, f"{path}.canonical_name", "CHANNEL_DUPLICATE"
        )
        if name in seen_names or canonical in seen_canonical:
            raise err("CHANNEL_DUPLICATE", "v1 通道名或规范名重复", name=name)
        seen_names.add(name)
        seen_canonical.add(canonical)
        generation = _positive_int(raw.get("generation", 1), f"{path}.generation")
        partitions = _positive_int(raw.get("partitions"), f"{path}.partitions")

        event_fields = raw.get("event_fields")
        if not isinstance(event_fields, list) or not event_fields:
            raise err("INVALID_FORMAT", "v1 event_fields 必须为非空字符串数组", path=path)
        for index, field_name in enumerate(event_fields):
            _nonempty_string(field_name, f"{path}.event_fields[{index}]", "INVALID_FORMAT")
        if len(set(event_fields)) != len(event_fields):
            raise err("INVALID_FORMAT", "v1 event_fields 不能重复", path=path)

        aliases = [name]
        raw_aliases = raw.get("aliases", [])
        if not isinstance(raw_aliases, list):
            raise err("INVALID_FORMAT", "v1 aliases 必须为数组", path=path)
        for index, alias in enumerate(raw_aliases):
            aliases.append(
                _nonempty_string(alias, f"{path}.aliases[{index}]", "CHANNEL_DUPLICATE")
            )
        if canonical not in aliases:
            aliases.append(canonical)

        channel = _ChannelSource(
            canonical_name=canonical,
            generation=generation,
            partitions=partitions,
            field_schema=[FieldDef(str(item), "any", None) for item in event_fields],
            aliases=aliases,
            source_version="v1",
        )
        raw_events = raw.get("events")
        if not isinstance(raw_events, list):
            raise err("INVALID_FORMAT", "v1 events 必须为数组", path=path)
        seen_sequences: set[int] = set()
        for event_index, raw_event in enumerate(raw_events):
            event_path = f"{path}.events[{event_index}]"
            if not isinstance(raw_event, dict):
                raise err("MALFORMED_EVENT", "v1 事件必须为对象", path=event_path)
            sequence = _positive_int(raw_event.get("sequence"), f"{event_path}.sequence")
            if sequence in seen_sequences:
                raise err(
                    "SEQUENCE_DUPLICATE",
                    "v1 通道内事件序号重复",
                    channel=canonical,
                    sequence=sequence,
                )
            seen_sequences.add(sequence)
            relative_seconds = raw_event.get("relative_seconds")
            if not is_int(relative_seconds) or relative_seconds < 0:
                raise err(
                    "V1_TIME_INVALID",
                    "v1 relative_seconds 必须为非负整数",
                    channel=canonical,
                    sequence=sequence,
                )
            logical_clock = raw_event.get("logical_clock", 0)
            if not is_int(logical_clock) or logical_clock < 0:
                raise err(
                    "MALFORMED_EVENT",
                    "v1 logical_clock 必须为非负整数",
                    channel=canonical,
                    sequence=sequence,
                )
            flat_fields = raw_event.get("fields")
            if not isinstance(flat_fields, list):
                raise err(
                    "MALFORMED_EVENT",
                    "v1 fields 必须为扁平数组",
                    channel=canonical,
                    sequence=sequence,
                )
            if len(flat_fields) != len(event_fields):
                raise err(
                    "FIELD_COUNT_MISMATCH",
                    "v1 扁平字段数组长度与声明不一致",
                    channel=canonical,
                    sequence=sequence,
                    expected=len(event_fields),
                    actual=len(flat_fields),
                )
            for value_index, value in enumerate(flat_fields):
                ensure_json_value(
                    value, path=f"{event_path}.fields[{value_index}]"
                )
            fields = {
                field_name: value
                for field_name, value in zip(event_fields, flat_fields, strict=True)
            }
            absolute_ms = epoch_ms + relative_seconds * 1000
            time_conversion = {
                "source_relative_seconds": relative_seconds,
                "scale": "seconds*1000",
                "rounding_direction": "none",
                "rounding_remainder_ms": 0,
                "epoch_ms": epoch_ms,
                "absolute_ms": absolute_ms,
            }
            partition_key = raw_event.get("partition_key", str(sequence))
            if not isinstance(partition_key, str) or not partition_key:
                raise err(
                    "MALFORMED_EVENT",
                    "partition_key 必须为非空字符串",
                    channel=canonical,
                    sequence=sequence,
                )
            tombstone = raw_event.get("tombstone", False)
            if not isinstance(tombstone, bool):
                raise err("MALFORMED_EVENT", "tombstone 必须为布尔值", path=event_path)
            channel.events.append(
                _EventSource(
                    sequence=sequence,
                    logical_clock=logical_clock,
                    produced_at_ms=absolute_ms,
                    fields=fields,
                    tombstone=tombstone,
                    source_version="v1",
                    generation=generation,
                    raw=raw_event,
                    time_conversion=time_conversion,
                    field_checksum=payload_digest(fields),
                )
            )
        channels.append(channel)

        raw_subscriptions = raw.get("subscriptions", [])
        if not isinstance(raw_subscriptions, list):
            raise err("INVALID_FORMAT", "subscriptions 必须为数组", path=path)
        for index, item in enumerate(raw_subscriptions):
            subscriptions.append(
                _parse_subscription_source(
                    item,
                    "v1",
                    f"{path}.subscriptions[{index}]",
                    default_channel=name,
                )
            )

    for index, item in enumerate(document.get("subscriptions", [])):
        if not isinstance(item, dict):
            raise err("INVALID_FORMAT", "顶层订阅必须为对象", path=f"subscriptions[{index}]")
        subscriptions.append(
            _parse_subscription_source(item, "v1", f"subscriptions[{index}]")
        )
    bridges = _parse_bridges(document.get("bridges", []), "v1", "bridges")
    retention = _optional_positive_int(document.get("retention_bytes"), "retention_bytes")
    tolerance = _optional_nonnegative_int(
        document.get("clock_tolerance_ms"), "clock_tolerance_ms"
    )
    return channels, bridges, subscriptions, int(epoch_ms), retention, tolerance


def _parse_v2(
    metadata: dict[str, Any],
    shard_documents: list[tuple[Path, Any]],
) -> tuple[
    list[_ChannelSource],
    list[_BridgeSource],
    list[_SubscriptionSource],
    int | None,
    int | None,
]:
    if metadata["meta"].get("format") != "eventbus-v2-metadata":
        raise err("METADATA_INVALID", "v2 meta.format 必须为 eventbus-v2-metadata")

    channels: list[_ChannelSource] = []
    seen_canonical: set[str] = set()
    for row in metadata["channels"]:
        canonical = _nonempty_string(
            row["canonical_name"], "v2.channels.canonical_name", "CHANNEL_DUPLICATE"
        )
        if canonical in seen_canonical:
            raise err("CHANNEL_DUPLICATE", "v2 通道规范名重复", name=canonical)
        seen_canonical.add(canonical)
        generation = _positive_int(row["generation"], "v2.channels.generation")
        partitions = _positive_int(row["partitions"], "v2.channels.partitions")
        schema = _parse_field_schema(row["field_schema_json"], canonical)
        aliases = [canonical]
        v1_name = row.get("v1_name")
        if v1_name:
            aliases.append(
                _nonempty_string(v1_name, "v2.channels.v1_name", "CHANNEL_DUPLICATE")
            )
        channels.append(
            _ChannelSource(
                canonical_name=canonical,
                generation=generation,
                partitions=partitions,
                field_schema=schema,
                aliases=aliases,
                source_version="v2",
            )
        )
    channel_by_name = {item.canonical_name: item for item in channels}

    indexed = metadata["event_index"]
    seen_sequences: dict[str, set[int]] = {name: set() for name in channel_by_name}
    indexed_keys: set[tuple[str, int]] = set()
    shard_events_seen: set[tuple[str, int]] = set()
    for shard_path, shard in shard_documents:
        if not isinstance(shard, dict) or shard.get("format") != "eventbus-v2-shard":
            raise err("INVALID_FORMAT", "v2 分片 format 不合法", shard=shard_path.name)
        checksum = shard.get("checksum")
        shard_without_checksum = {
            key: value for key, value in shard.items() if key != "checksum"
        }
        actual_shard_hash = digest_value(shard_without_checksum)
        if checksum != actual_shard_hash:
            raise err(
                "SHARD_HASH_MISMATCH",
                "v2 分片哈希不匹配",
                shard=shard_path.name,
                expected=checksum,
                actual=actual_shard_hash,
            )
        canonical = shard.get("channel")
        if canonical not in channel_by_name:
            raise err(
                "BRIDGE_CHANNEL_MISSING",
                "v2 分片引用了元数据中不存在的通道",
                shard=shard_path.name,
                channel=canonical,
            )
        channel = channel_by_name[canonical]
        if shard.get("generation") != channel.generation:
            raise err(
                "GENERATION_MISMATCH",
                "v2 分片代数与元数据不一致",
                shard=shard_path.name,
                expected=channel.generation,
                actual=shard.get("generation"),
            )
        raw_events = shard.get("events")
        if not isinstance(raw_events, list):
            raise err("MALFORMED_EVENT", "v2 分片 events 必须为数组", shard=shard_path.name)
        for event_index, raw_event in enumerate(raw_events):
            path = f"{shard_path.name}.events[{event_index}]"
            if not isinstance(raw_event, dict):
                raise err("MALFORMED_EVENT", "v2 事件必须为对象", path=path)
            sequence = _positive_int(raw_event.get("sequence"), f"{path}.sequence")
            if sequence in seen_sequences[canonical]:
                raise err(
                    "SEQUENCE_DUPLICATE",
                    "v2 通道内事件序号重复",
                    channel=canonical,
                    sequence=sequence,
                )
            seen_sequences[canonical].add(sequence)
            shard_events_seen.add((canonical, sequence))
            logical_clock = raw_event.get("logical_clock")
            if not is_int(logical_clock) or logical_clock < 0:
                raise err(
                    "V2_CLOCK_INVALID",
                    "v2 logical_clock 必须为非负整数",
                    channel=canonical,
                    sequence=sequence,
                )
            produced_at_ms = raw_event.get("produced_at_ms")
            if not is_int(produced_at_ms) or produced_at_ms < 0:
                raise err(
                    "MALFORMED_EVENT",
                    "v2 produced_at_ms 必须为非负整数毫秒戳",
                    channel=canonical,
                    sequence=sequence,
                )
            fields = raw_event.get("fields")
            if not isinstance(fields, dict):
                raise err(
                    "MALFORMED_EVENT",
                    "v2 具名字段信封必须为对象",
                    channel=canonical,
                    sequence=sequence,
                )
            for key, value in fields.items():
                if not isinstance(key, str) or not key:
                    raise err("MALFORMED_EVENT", "v2 字段名必须为非空字符串", path=path)
                ensure_json_value(value, path=f"{path}.fields.{key}")
            actual_checksum = payload_digest(fields)
            declared_checksum = raw_event.get("field_checksum")
            if declared_checksum != actual_checksum:
                raise err(
                    "V2_CHECKSUM_MISMATCH",
                    "v2 具名字段信封校验和不匹配",
                    channel=canonical,
                    sequence=sequence,
                    expected=declared_checksum,
                    actual=actual_checksum,
                )
            tombstone = raw_event.get("tombstone", False)
            if not isinstance(tombstone, bool):
                raise err(
                    "MALFORMED_EVENT",
                    "v2 tombstone 必须为布尔值",
                    channel=canonical,
                    sequence=sequence,
                )
            partition_key = raw_event.get("partition_key", str(sequence))
            if not isinstance(partition_key, str) or not partition_key:
                raise err(
                    "MALFORMED_EVENT",
                    "partition_key 必须为非空字符串",
                    channel=canonical,
                    sequence=sequence,
                )
            index_row = indexed.get((canonical, sequence))
            if index_row is None:
                raise err(
                    "METADATA_INVALID",
                    "v2 事件缺少 SQLite 索引",
                    channel=canonical,
                    sequence=sequence,
                )
            if index_row["shard"] != shard_path.name:
                raise err(
                    "METADATA_INVALID",
                    "v2 事件索引的分片名不一致",
                    channel=canonical,
                    sequence=sequence,
                )
            if index_row["shard_sha256"] != checksum:
                raise err(
                    "SHARD_HASH_MISMATCH",
                    "v2 事件索引的分片哈希不一致",
                    channel=canonical,
                    sequence=sequence,
                )
            try:
                indexed_fields = json.loads(index_row["fields_json"])
            except (json.JSONDecodeError, TypeError) as exc:
                raise err(
                    "METADATA_INVALID",
                    "v2 事件索引 fields_json 不是合法 JSON",
                    channel=canonical,
                    sequence=sequence,
                ) from exc
            if (
                index_row["generation"] != channel.generation
                or canonical_json(indexed_fields) != canonical_json(fields)
                or index_row["field_checksum"] != declared_checksum
                or index_row["produced_at_ms"] != produced_at_ms
                or index_row["logical_clock"] != logical_clock
                or bool(index_row["tombstone"]) is not tombstone
            ):
                raise err(
                    "METADATA_INVALID",
                    "v2 事件与 SQLite 索引元数据不一致",
                    channel=canonical,
                    sequence=sequence,
                )
            indexed_keys.add((canonical, sequence))
            channel.events.append(
                _EventSource(
                    sequence=sequence,
                    logical_clock=logical_clock,
                    produced_at_ms=produced_at_ms,
                    fields=fields,
                    tombstone=tombstone,
                    source_version="v2",
                    generation=channel.generation,
                    raw=raw_event,
                    field_checksum=actual_checksum,
                )
            )
    if indexed_keys != set(indexed) or shard_events_seen != set(indexed):
        raise err(
            "METADATA_INVALID",
            "v2 SQLite 索引与分片事件集合不一致",
            indexed=len(indexed),
            shards=len(shard_events_seen),
        )

    bridges = _parse_bridges(metadata["bridges"], "v2", "v2.bridges")
    subscriptions = [
        _SubscriptionSource(
            name=_nonempty_string(row["name"], "v2.subscriptions.name", "INVALID_FORMAT"),
            channel=_nonempty_string(
                row["channel"], "v2.subscriptions.channel", "INVALID_FORMAT"
            ),
            start_offset=_nonnegative_int(
                row["start_offset"], "v2.subscriptions.start_offset"
            ),
            source_version="v2",
        )
        for row in metadata["subscriptions"]
    ]
    retention = _optional_positive_int(
        _meta_int(metadata["meta"].get("retention_bytes"), "v2.retention_bytes"),
        "v2.retention_bytes",
    )
    tolerance = _optional_nonnegative_int(
        _meta_int(
            metadata["meta"].get("clock_tolerance_ms"), "v2.clock_tolerance_ms"
        ),
        "v2.clock_tolerance_ms",
    )
    return channels, bridges, subscriptions, retention, tolerance


def _read_v2_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise err("NOT_FOUND", "v2 SQLite 元数据库不存在", path=str(path))
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {"meta", "channels", "event_index", "bridges", "subscriptions"}
        if not required.issubset(tables):
            raise err(
                "METADATA_INVALID",
                "v2 元数据库缺少必要表",
                missing=sorted(required - tables),
            )
        meta = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM meta")
        }
        channels = [
            dict(row) for row in connection.execute(
                "SELECT canonical_name, generation, partitions, field_schema_json, v1_name "
                "FROM channels ORDER BY canonical_name"
            )
        ]
        event_index = {
            (row["channel"], row["sequence"]): dict(row)
            for row in connection.execute(
                "SELECT channel, sequence, generation, shard, shard_sha256, "
                "field_checksum, fields_json, produced_at_ms, logical_clock, "
                "tombstone FROM event_index"
            )
        }
        bridges = [
            dict(row)
            for row in connection.execute(
                "SELECT id, source_channel, target_channel, source_generation, "
                "target_generation, min_upstream_watermark, filter_json, "
                "transforms_json, max_batch FROM bridges ORDER BY id"
            )
        ]
        subscriptions = [
            dict(row)
            for row in connection.execute(
                "SELECT name, channel, start_offset FROM subscriptions ORDER BY name"
            )
        ]
        return {
            "meta": meta,
            "channels": channels,
            "event_index": event_index,
            "bridges": bridges,
            "subscriptions": subscriptions,
        }
    except sqlite3.Error as exc:
        raise err("METADATA_INVALID", "无法读取 v2 元数据库", path=str(path), reason=str(exc)) from exc
    finally:
        try:
            connection.close()
        except (NameError, UnboundLocalError):
            pass


def _parse_field_schema(raw: Any, channel: str) -> list[FieldDef]:
    try:
        document = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise err("METADATA_INVALID", "field_schema_json 不是合法 JSON", channel=channel) from exc
    if not isinstance(document, list) or not document:
        raise err("METADATA_INVALID", "field_schema 必须为非空数组", channel=channel)
    result: list[FieldDef] = []
    seen: set[str] = set()
    for index, item in enumerate(document):
        if not isinstance(item, dict):
            raise err("METADATA_INVALID", "字段声明必须为对象", channel=channel, index=index)
        name = _nonempty_string(
            item.get("name"), f"{channel}.field_schema[{index}].name", "METADATA_INVALID"
        )
        if name in seen:
            raise err("METADATA_INVALID", "字段名重复", channel=channel, field=name)
        seen.add(name)
        type_name = item.get("type", "any")
        if type_name not in _FIELD_TYPES:
            raise err(
                "METADATA_INVALID",
                "字段类型不合法",
                channel=channel,
                field=name,
                type=type_name,
            )
        default = item.get("default")
        if default is not None:
            ensure_json_value(default, path=f"{channel}.field_schema.{name}.default")
            try:
                default = typed_value(default, type_name)
            except BusinessError as exc:
                raise err(
                    "METADATA_INVALID",
                    "字段默认值与声明类型不兼容",
                    channel=channel,
                    field=name,
                    type=type_name,
                    default=default,
                ) from exc
        result.append(FieldDef(name=name, type=type_name, default=default))
    return result


def _merge_channel_definitions(
    v1_channels: list[_ChannelSource],
    v2_channels: list[_ChannelSource],
) -> tuple[dict[str, ChannelDef], dict[str, set[str]], dict[str, str]]:
    grouped: dict[str, list[_ChannelSource]] = {}
    for channel in v1_channels + v2_channels:
        grouped.setdefault(channel.canonical_name, []).append(channel)

    merged: dict[str, ChannelDef] = {}
    v2_field_names: dict[str, set[str]] = {}
    alias_map: dict[str, str] = {}
    for canonical in sorted(grouped):
        sources = grouped[canonical]
        versions = {item.source_version for item in sources}
        winner = max(
            sources,
            key=lambda item: (
                item.generation,
                _SOURCE_PRIORITY[item.source_version],
            ),
        )
        schema: list[FieldDef] = []
        by_name: dict[str, FieldDef] = {}
        ordered_sources = sorted(
            sources,
            key=lambda item: (
                -item.generation,
                -_SOURCE_PRIORITY[item.source_version],
            ),
        )
        for source in ordered_sources:
            for item in source.field_schema:
                if item.name not in by_name:
                    by_name[item.name] = item
                    schema.append(item)
        v2_schema = next(
            (item for item in sources if item.source_version == "v2"), None
        )
        v2_fields = (
            {item.name for item in v2_schema.field_schema}
            if v2_schema is not None
            else set()
        )
        for source in ordered_sources:
            if source.source_version == "v2":
                continue
            for item in source.field_schema:
                if item.name not in by_name:
                    by_name[item.name] = item
                    schema.append(item)
        aliases = sorted({alias for source in sources for alias in source.aliases})
        merged[canonical] = ChannelDef(
            name=canonical,
            generation=max(item.generation for item in sources),
            partitions=winner.partitions,
            field_schema=schema,
            aliases=aliases,
            sources=sorted(versions),
        )
        v2_field_names[canonical] = v2_fields
        for alias in aliases:
            previous = alias_map.get(alias)
            if previous is not None and previous != canonical:
                raise err(
                    "CHANNEL_DUPLICATE",
                    "通道别名冲突",
                    alias=alias,
                    first=previous,
                    second=canonical,
                )
            alias_map[alias] = canonical
    return merged, v2_field_names, alias_map


def _merge_events(
    channels: dict[str, ChannelDef],
    v2_field_names: dict[str, set[str]],
    v1_channels: list[_ChannelSource],
    v2_channels: list[_ChannelSource],
) -> tuple[list[UnifiedEvent], list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: dict[tuple[str, int], list[_EventSource]] = {}
    for channel in v1_channels + v2_channels:
        for event in channel.events:
            candidates.setdefault((channel.canonical_name, event.sequence), []).append(event)

    events: list[UnifiedEvent] = []
    decisions: list[dict[str, Any]] = []
    degradations: list[dict[str, Any]] = []
    for (channel_name, sequence) in sorted(candidates):
        options = candidates[(channel_name, sequence)]
        winner = max(
            options,
            key=lambda item: (
                item.logical_clock,
                item.generation,
                _SOURCE_PRIORITY[item.source_version],
            ),
        )
        fields = dict(winner.fields)
        channel = channels[channel_name]
        for schema_field in channel.field_schema:
            if schema_field.name in fields:
                continue
            fields[schema_field.name] = schema_field.default
            if (
                winner.source_version == "v1"
                and schema_field.name in v2_field_names[channel_name]
            ):
                degradation = {
                    "channel": channel_name,
                    "sequence": sequence,
                    "field": schema_field.name,
                    "source_version": "v1",
                    "action": "filled_v2_default",
                    "default": schema_field.default,
                    "reason": "v2 field absent from v1 flat envelope",
                }
                degradations.append(degradation)
        ranking = []
        for item in sorted(
            options,
            key=lambda candidate: (
                candidate.source_version,
                candidate.generation,
                candidate.logical_clock,
            ),
        ):
            ranking.append(
                {
                    "source_version": item.source_version,
                    "generation": item.generation,
                    "logical_clock": item.logical_clock,
                    "field_checksum": item.field_checksum,
                    "selected": item is winner,
                }
            )
        if len(options) == 1:
            basis = f"only source {winner.source_version} at generation {winner.generation}"
        else:
            others = [item for item in options if item is not winner]
            best_other = max(
                others,
                key=lambda item: (
                    item.logical_clock,
                    item.generation,
                    _SOURCE_PRIORITY[item.source_version],
                ),
            )
            if winner.logical_clock != best_other.logical_clock:
                basis = (
                    f"logical_clock {winner.logical_clock} > "
                    f"{best_other.logical_clock}"
                )
            elif winner.generation != best_other.generation:
                basis = (
                    f"logical_clock tie; generation {winner.generation} > "
                    f"{best_other.generation}"
                )
            else:
                basis = (
                    "logical_clock and generation tie; format priority "
                    f"{winner.source_version} > {best_other.source_version}"
                )
        merged_event = UnifiedEvent(
            channel=channel_name,
            sequence=sequence,
            partition_key=winner.raw.get("partition_key", str(sequence)),
            logical_clock=winner.logical_clock,
            produced_at_ms=winner.produced_at_ms,
            fields=fields,
            tombstone=winner.tombstone,
            source_version=winner.source_version,
            source_generation=winner.generation,
            raw_repr=winner.raw,
            time_conversion=winner.time_conversion,
            decision_basis=basis,
            decision_options=ranking,
            degradations=[
                item
                for item in degradations
                if item["channel"] == channel_name and item["sequence"] == sequence
            ],
            field_checksum=payload_digest(fields),
        )
        events.append(merged_event)
        raw_v1 = next(
            (item.raw for item in options if item.source_version == "v1"), None
        )
        raw_v2 = next(
            (item.raw for item in options if item.source_version == "v2"), None
        )
        decisions.append(
            {
                "channel": channel_name,
                "sequence": sequence,
                "selected_source": winner.source_version,
                "selected_generation": winner.generation,
                "logical_clock": winner.logical_clock,
                "decision_basis": basis,
                "options": ranking,
                "raw_v1": raw_v1,
                "raw_v2": raw_v2,
            }
        )

    events.sort(key=lambda item: (item.channel, item.sequence))
    decisions.sort(key=lambda item: (item["channel"], item["sequence"]))
    degradations.sort(key=lambda item: (item["channel"], item["sequence"], item["field"]))
    return events, decisions, degradations


def _merge_bridges(
    v1_bridges: list[_BridgeSource],
    v2_bridges: list[_BridgeSource],
    channels: dict[str, ChannelDef],
    alias_map: dict[str, str],
) -> dict[str, BridgeDef]:
    candidates: dict[str, list[_BridgeSource]] = {}
    for bridge in v1_bridges + v2_bridges:
        candidates.setdefault(bridge.bridge_id, []).append(bridge)
    result: dict[str, BridgeDef] = {}
    for bridge_id in sorted(candidates):
        winner = max(
            candidates[bridge_id],
            key=lambda item: _SOURCE_PRIORITY[item.source_version],
        )
        source = alias_map.get(winner.source)
        target = alias_map.get(winner.target)
        if source is None or target is None:
            raise err(
                "BRIDGE_CHANNEL_MISSING",
                "桥接规则引用了不存在的通道",
                bridge=bridge_id,
                source=winner.source,
                target=winner.target,
            )
        result[bridge_id] = BridgeDef(
            id=bridge_id,
            source=source,
            target=target,
            source_generation=(
                winner.source_generation
                if winner.source_generation is not None
                else channels[source].generation
            ),
            target_generation=(
                winner.target_generation
                if winner.target_generation is not None
                else channels[target].generation
            ),
            min_upstream_watermark=winner.min_upstream_watermark,
            filter=winner.filter,
            transforms=winner.transforms,
            max_batch=winner.max_batch,
        )
    _validate_bridge_graph(result)
    return result


def _validate_bridge_graph(bridges: dict[str, BridgeDef]) -> None:
    adjacency: dict[str, set[str]] = {}
    indegree: dict[str, int] = {}
    for bridge in bridges.values():
        adjacency.setdefault(bridge.source, set()).add(bridge.target)
        adjacency.setdefault(bridge.target, set())
        indegree.setdefault(bridge.source, 0)
        indegree.setdefault(bridge.target, 0)
    for source, targets in adjacency.items():
        for target in targets:
            indegree[target] += 1
    queue = sorted(node for node, degree in indegree.items() if degree == 0)
    visited = 0
    while queue:
        node = queue.pop(0)
        visited += 1
        for target in sorted(adjacency[node]):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
                queue.sort()
    if visited != len(indegree):
        raise err("BRIDGE_CYCLE", "桥接图存在环", nodes=sorted(indegree))


def _merge_subscriptions(
    v1_subscriptions: list[_SubscriptionSource],
    v2_subscriptions: list[_SubscriptionSource],
    channels: dict[str, ChannelDef],
    alias_map: dict[str, str],
    events: list[UnifiedEvent],
) -> dict[str, SubscriptionDef]:
    counts: dict[str, int] = {name: 0 for name in channels}
    for event in events:
        counts[event.channel] += 1
    grouped: dict[str, list[_SubscriptionSource]] = {}
    for subscription in v1_subscriptions + v2_subscriptions:
        grouped.setdefault(subscription.name, []).append(subscription)
    result: dict[str, SubscriptionDef] = {}
    for name in sorted(grouped):
        winner = max(
            grouped[name],
            key=lambda item: _SOURCE_PRIORITY[item.source_version],
        )
        channel = alias_map.get(winner.channel)
        if channel is None:
            raise err(
                "SUBSCRIPTION_RANGE",
                "订阅引用了不存在的通道",
                subscription=name,
                channel=winner.channel,
            )
        if winner.start_offset < 0 or winner.start_offset > counts[channel]:
            raise err(
                "SUBSCRIPTION_RANGE",
                "订阅起始偏移超出合法区间",
                subscription=name,
                channel=channel,
                start_offset=winner.start_offset,
                max_offset=counts[channel],
            )
        result[name] = SubscriptionDef(
            name=name, channel=channel, start_offset=winner.start_offset
        )
    return result


def _parse_bridges(raw: Any, source_version: str, path: str) -> list[_BridgeSource]:
    if not isinstance(raw, list):
        raise err("INVALID_FORMAT", "桥接规则必须为数组", path=path)
    result: list[_BridgeSource] = []
    for index, item in enumerate(raw):
        if isinstance(item, sqlite3.Row):
            item = dict(item)
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            raise err("INVALID_FORMAT", "桥接规则必须为对象", path=item_path)
        if source_version == "v1":
            bridge_id = item.get("id")
            source = item.get("source")
            target = item.get("target")
            filter_document = item.get("filter", {})
            transforms = item.get("transforms", item.get("transform", []))
            source_generation = item.get("source_generation")
            target_generation = item.get("target_generation")
            minimum = item.get("min_upstream_watermark", 0)
            max_batch = item.get("max_batch", 100)
        else:
            bridge_id = item.get("id")
            source = item.get("source_channel")
            target = item.get("target_channel")
            filter_document = _json_column(
                item.get("filter_json"), f"{item_path}.filter_json"
            )
            transforms = _json_column(
                item.get("transforms_json"), f"{item_path}.transforms_json"
            )
            source_generation = item.get("source_generation")
            target_generation = item.get("target_generation")
            minimum = item.get("min_upstream_watermark", 0)
            max_batch = item.get("max_batch", 100)
        bridge_id = _nonempty_string(bridge_id, f"{item_path}.id", "INVALID_FORMAT")
        source = _nonempty_string(source, f"{item_path}.source", "INVALID_FORMAT")
        target = _nonempty_string(target, f"{item_path}.target", "INVALID_FORMAT")
        source_generation = (
            _positive_int(source_generation, f"{item_path}.source_generation")
            if source_generation is not None
            else None
        )
        target_generation = (
            _positive_int(target_generation, f"{item_path}.target_generation")
            if target_generation is not None
            else None
        )
        minimum = _nonnegative_int(
            minimum, f"{item_path}.min_upstream_watermark"
        )
        max_batch = _positive_int(max_batch, f"{item_path}.max_batch")
        if not isinstance(filter_document, dict):
            raise err("PATCH_INVALID", "filter 必须为对象", path=item_path)
        if not isinstance(transforms, list):
            raise err("PATCH_INVALID", "transforms 必须为数组", path=item_path)
        validate_filter(filter_document, f"{item_path}.filter")
        validate_transforms(transforms, f"{item_path}.transforms")
        result.append(
            _BridgeSource(
                bridge_id=bridge_id,
                source=source,
                target=target,
                source_generation=source_generation,
                target_generation=target_generation,
                min_upstream_watermark=minimum,
                filter=filter_document,
                transforms=transforms,
                max_batch=max_batch,
                source_version=source_version,
            )
        )
    return result


def _parse_subscription_source(
    item: Any,
    source_version: str,
    path: str,
    default_channel: str | None = None,
) -> _SubscriptionSource:
    if not isinstance(item, dict):
        raise err("INVALID_FORMAT", "订阅声明必须为对象", path=path)
    name = _nonempty_string(item.get("name"), f"{path}.name", "INVALID_FORMAT")
    channel = item.get("channel", default_channel)
    channel = _nonempty_string(channel, f"{path}.channel", "INVALID_FORMAT")
    start_offset = _nonnegative_int(item.get("start_offset"), f"{path}.start_offset")
    return _SubscriptionSource(
        name=name,
        channel=channel,
        start_offset=start_offset,
        source_version=source_version,
    )


def _source_fingerprint(
    v1_path: Path, metadata_path: Path, shard_paths: list[Path]
) -> str:
    digest = hashlib.sha256()
    for path in [v1_path, metadata_path, *shard_paths]:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            raise err("NOT_FOUND", "读取样本文件失败", path=str(path), reason=str(exc)) from exc
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _json_column(value: Any, path: str) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        raise err("METADATA_INVALID", "JSON 列不是文本", path=path)
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise err("METADATA_INVALID", "JSON 列解析失败", path=path) from exc


def _nonempty_string(value: Any, path: str, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise err(code, "必须为非空字符串", path=path)
    return value


def _positive_int(value: Any, path: str) -> int:
    if not is_int(value) or value <= 0:
        if "partition" in path:
            raise err("PARTITIONS_INVALID", "分区数必须为正整数", path=path, value=value)
        raise err("INVALID_FORMAT", "必须为正整数", path=path, value=value)
    return value


def _nonnegative_int(value: Any, path: str) -> int:
    if not is_int(value) or value < 0:
        raise err("INVALID_FORMAT", "必须为非负整数", path=path, value=value)
    return value


def _optional_positive_int(value: Any, path: str) -> int | None:
    if value is None:
        return None
    if not is_int(value) or value <= 0:
        raise err("INVALID_FORMAT", "必须为正整数", path=path, value=value)
    return value


def _optional_nonnegative_int(value: Any, path: str) -> int | None:
    if value is None:
        return None
    if not is_int(value) or value < 0:
        raise err("INVALID_FORMAT", "必须为非负整数", path=path, value=value)
    return value


def _meta_int(value: Any, path: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = int(value, 10)
        except ValueError as exc:
            raise err("METADATA_INVALID", "元数据整数格式不合法", path=path, value=value) from exc
    if not is_int(value):
        raise err("METADATA_INVALID", "元数据必须为整数", path=path, value=value)
    return value
