from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from .canon import canonical_hash, sha256_file
from .errors import Code, WorkbenchError
from .rules import parse_key_rule, partition_for_key, require_nonnegative_int, require_positive_int


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise WorkbenchError(Code.INPUT_MISSING, "required JSON input is missing", {"path": str(path)})
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise WorkbenchError(
            Code.JSON_INVALID,
            "JSON input cannot be parsed",
            {"path": str(path), "line": exc.lineno, "column": exc.colno, "message": exc.msg},
        ) from exc


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        raise WorkbenchError(Code.INPUT_MISSING, "required JSONL input is missing", {"path": str(path)})
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise WorkbenchError(
                        Code.JSON_INVALID,
                        "JSONL line cannot be parsed",
                        {"path": str(path), "line": line_number, "column": exc.colno},
                    ) from exc
                if not isinstance(row, dict):
                    raise WorkbenchError(Code.EVENT_FIELD, "JSONL row must be an object", {"path": str(path), "line": line_number})
                row["_line"] = line_number
                rows.append(row)
    except OSError as exc:
        raise WorkbenchError(Code.INPUT_MISSING, "cannot read JSONL input", {"path": str(path), "error": str(exc)}) from exc
    return rows


def _as_list(value: Any, field: str, code: str) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get(field), list):
        return value[field]
    raise WorkbenchError(code, f"expected a list under {field}", {"value_type": type(value).__name__})


def _validate_topics(raw: Any) -> List[Dict[str, Any]]:
    rows = _as_list(raw, "topics", Code.TOPIC_FIELD)
    topics: List[Dict[str, Any]] = []
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise WorkbenchError(Code.TOPIC_FIELD, "topic must be an object", {"index": index})
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise WorkbenchError(Code.TOPIC_FIELD, "topic.name must be a non-empty string", {"index": index})
        if name in seen:
            raise WorkbenchError(Code.TOPIC_DUPLICATE, "topic names must be unique", {"name": name})
        seen.add(name)
        partitions = require_positive_int(row.get("partitions"), Code.TOPIC_FIELD, "partitions", topic=name)
        backlog_limit = require_nonnegative_int(row.get("backlog_limit"), Code.TOPIC_FIELD, "backlog_limit", topic=name)
        rule_text = row.get("key_rule")
        parsed_rule = parse_key_rule(rule_text, partitions)
        topics.append(
            {
                "name": name,
                "partitions": partitions,
                "key_rule": rule_text,
                "parsed_key_rule": parsed_rule,
                "backlog_limit": backlog_limit,
            }
        )
    if not topics:
        raise WorkbenchError(Code.TOPIC_FIELD, "at least one topic is required")
    return topics


def _valid_loopback_url(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1", "[::1]"}:
        return False
    return parsed.port is not None and parsed.port > 0


def _validate_subscriptions(raw: Any, topic_names: set[str]) -> List[Dict[str, Any]]:
    rows = _as_list(raw, "subscriptions", Code.SUBSCRIPTION_FIELD)
    subscriptions: List[Dict[str, Any]] = []
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise WorkbenchError(Code.SUBSCRIPTION_FIELD, "subscription must be an object", {"index": index})
        subscriber_id = row.get("subscriber_id")
        if not isinstance(subscriber_id, str) or not subscriber_id:
            raise WorkbenchError(Code.SUBSCRIPTION_FIELD, "subscriber_id must be a non-empty string", {"index": index})
        if subscriber_id in seen:
            raise WorkbenchError(Code.SUBSCRIBER_DUPLICATE, "subscriber IDs must be unique", {"subscriber_id": subscriber_id})
        seen.add(subscriber_id)
        topic = row.get("topic")
        if topic not in topic_names:
            raise WorkbenchError(Code.SUBSCRIPTION_FIELD, "subscription references unknown topic", {"subscriber_id": subscriber_id, "topic": topic})
        if not _valid_loopback_url(row.get("callback_url")):
            raise WorkbenchError(
                Code.CALLBACK_ENDPOINT,
                "callback_url must be an explicit local loopback http(s) endpoint with port",
                {"subscriber_id": subscriber_id, "callback_url": row.get("callback_url")},
            )
        pull_batch_size = require_positive_int(row.get("pull_batch_size"), Code.SUBSCRIPTION_FIELD, "pull_batch_size", subscriber_id=subscriber_id)
        rate_limit = require_positive_int(row.get("rate_limit_per_sec"), Code.SUBSCRIPTION_FIELD, "rate_limit_per_sec", subscriber_id=subscriber_id)
        priority = row.get("priority", 100)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise WorkbenchError(Code.SUBSCRIPTION_FIELD, "priority must be an integer", {"subscriber_id": subscriber_id, "priority": priority})
        subscriptions.append(
            {
                "subscriber_id": subscriber_id,
                "topic": topic,
                "callback_url": row["callback_url"],
                "pull_batch_size": pull_batch_size,
                "rate_limit_per_sec": rate_limit,
                "priority": priority,
            }
        )
    if not subscriptions:
        raise WorkbenchError(Code.SUBSCRIPTION_FIELD, "at least one subscription is required")
    return subscriptions


def _normalize_dependency_ref(raw: Any, context: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(raw, str):
        parts = raw.split("/")
        if len(parts) != 3:
            raise WorkbenchError(Code.DEPENDENCY_INVALID, "string dependency must be topic/partition/offset", {"dependency": raw, **context})
        topic, partition_text, offset_text = parts
        try:
            partition = int(partition_text)
            offset = int(offset_text)
        except ValueError as exc:
            raise WorkbenchError(Code.DEPENDENCY_INVALID, "dependency partition and offset must be integers", {"dependency": raw, **context}) from exc
        ref = {"topic": topic, "partition": partition, "offset": offset}
    elif isinstance(raw, dict):
        topic = raw.get("upstream_topic", raw.get("topic"))
        partition = raw.get("upstream_partition", raw.get("partition"))
        offset = raw.get("upstream_offset", raw.get("offset"))
        if not isinstance(topic, str) or not isinstance(partition, int) or isinstance(partition, bool) or not isinstance(offset, int) or isinstance(offset, bool):
            raise WorkbenchError(Code.DEPENDENCY_INVALID, "dependency object needs topic, partition and offset", {"dependency": raw, **context})
        ref = {"topic": topic, "partition": partition, "offset": offset}
        if "required_watermark" in raw:
            required = raw["required_watermark"]
            if not isinstance(required, int) or isinstance(required, bool) or required < 0:
                raise WorkbenchError(Code.DEPENDENCY_INVALID, "required_watermark must be a non-negative integer", {"dependency": raw, **context})
            ref["required_watermark"] = required
    else:
        raise WorkbenchError(Code.DEPENDENCY_INVALID, "dependency must be a string or object", {"dependency": raw, **context})
    if ref["offset"] < 0 or ref["partition"] < 0:
        raise WorkbenchError(Code.DEPENDENCY_INVALID, "dependency offset and partition must be non-negative", {"dependency": raw, **context})
    ref.setdefault("required_watermark", ref["offset"] + 1)
    return ref


def _validate_events(rows: List[Dict[str, Any]], topics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    topic_by_name = {topic["name"]: topic for topic in topics}
    events: List[Dict[str, Any]] = []
    offsets: Dict[Tuple[str, int], int] = {}
    previous_seq: Optional[int] = None
    for row in rows:
        line = row.pop("_line", None)
        context = {"line": line} if line is not None else {}
        seq = row.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
            raise WorkbenchError(Code.EVENT_FIELD, "event.seq must be a positive integer", {"seq": seq, **context})
        if previous_seq is not None and seq <= previous_seq:
            raise WorkbenchError(
                Code.EVENT_SEQ,
                "event seq values must be strictly increasing",
                {"previous_seq": previous_seq, "seq": seq, **context},
            )
        previous_seq = seq
        topic_name = row.get("topic")
        topic = topic_by_name.get(topic_name)
        if topic is None:
            raise WorkbenchError(Code.EVENT_FIELD, "event references unknown topic", {"topic": topic_name, "seq": seq, **context})
        key = row.get("key")
        if not isinstance(key, str) or not key:
            raise WorkbenchError(Code.EVENT_FIELD, "event.key must be a non-empty string", {"seq": seq, **context})
        partition = partition_for_key(topic["parsed_key_rule"], key, topic["partitions"])
        logical_clock = require_nonnegative_int(row.get("logical_clock_ms"), Code.EVENT_FIELD, "logical_clock_ms", seq=seq)
        expected_generation = require_nonnegative_int(row.get("expected_generation"), Code.EVENT_FIELD, "expected_generation", seq=seq)
        revision = require_nonnegative_int(row.get("revision"), Code.EVENT_FIELD, "revision", seq=seq)
        payload_summary = row.get("payload_summary")
        payload_digest = row.get("payload_digest")
        if payload_summary is not None and not isinstance(payload_summary, str):
            raise WorkbenchError(Code.EVENT_FIELD, "payload_summary must be a string", {"seq": seq})
        if payload_digest is not None and not isinstance(payload_digest, str):
            raise WorkbenchError(Code.EVENT_FIELD, "payload_digest must be a string", {"seq": seq})
        offset = offsets.get((topic_name, partition), 0)
        offsets[(topic_name, partition)] = offset + 1
        event = {
            "seq": seq,
            "topic": topic_name,
            "key": key,
            "partition": partition,
            "offset": offset,
            "message_key": f"{topic_name}/{partition}/{offset}",
            "payload_summary": payload_summary or "",
            "payload_digest": payload_digest or "",
            "logical_clock_ms": logical_clock,
            "expected_generation": expected_generation,
            "revision": revision,
        }
        events.append(event)
    if not events:
        raise WorkbenchError(Code.EVENT_FIELD, "at least one producer event is required")
    return events


def _normalize_v1(snapshot: Any) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise WorkbenchError(Code.SNAPSHOT_FIELD, "v1 snapshot must be an object")
    if snapshot.get("format") != "v1":
        raise WorkbenchError(Code.SNAPSHOT_FIELD, "v1 snapshot format must equal 'v1'", {"format": snapshot.get("format")})
    declared_clock = require_nonnegative_int(snapshot.get("declared_clock_ms"), Code.SNAPSHOT_FIELD, "v1.declared_clock_ms")
    entries_raw = snapshot.get("entries")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise WorkbenchError(Code.SNAPSHOT_FIELD, "v1 entries must be a non-empty list")
    dependency_map = snapshot.get("delivery_dependencies", {})
    if not isinstance(dependency_map, dict):
        raise WorkbenchError(Code.DEPENDENCY_INVALID, "v1 delivery_dependencies must be an object")
    entries: List[Dict[str, Any]] = []
    seen = set()
    for index, row in enumerate(entries_raw):
        if not isinstance(row, dict):
            raise WorkbenchError(Code.SNAPSHOT_FIELD, "v1 entry must be an object", {"index": index})
        message_key = row.get("message_key")
        if not isinstance(message_key, str) or not message_key:
            raise WorkbenchError(Code.SNAPSHOT_FIELD, "v1 message_key invalid", {"index": index})
        if message_key in seen:
            raise WorkbenchError(Code.SNAPSHOT_FIELD, "duplicate v1 message_key", {"message_key": message_key})
        seen.add(message_key)
        rel_sec = row.get("effective_rel_sec")
        require_nonnegative_int(rel_sec, Code.V1_TIME, "effective_rel_sec", message_key=message_key)
        clock = require_nonnegative_int(row.get("logical_clock_ms"), Code.SNAPSHOT_FIELD, "v1 entry logical_clock_ms", message_key=message_key)
        generation = require_nonnegative_int(row.get("semantic_generation"), Code.SNAPSHOT_FIELD, "semantic_generation", message_key=message_key)
        revision = require_nonnegative_int(row.get("revision", 0), Code.SNAPSHOT_FIELD, "revision", message_key=message_key)
        raw_deps = dependency_map.get(message_key, row.get("delivery_dependencies", []))
        if not isinstance(raw_deps, list):
            raise WorkbenchError(Code.DEPENDENCY_INVALID, "v1 dependencies must be a list", {"message_key": message_key})
        deps = [_normalize_dependency_ref(ref, {"source": "v1", "message_key": message_key}) for ref in raw_deps]
        entries.append(
            {
                "message_key": message_key,
                "effective_rel_sec": rel_sec,
                "declared_clock_ms": clock,
                "semantic_generation": generation,
                "revision": revision,
                "tombstone": bool(row.get("tombstone", False)),
                "tombstone_reason": row.get("tombstone_reason"),
                "dependencies": deps,
                "source": "v1",
            }
        )
    extra_dep_keys = sorted(set(dependency_map) - seen)
    if extra_dep_keys:
        raise WorkbenchError(Code.DEPENDENCY_INVALID, "v1 dependency map references unknown entry", {"message_keys": extra_dep_keys})
    return {
        "format": "v1",
        "snapshot_id": snapshot.get("snapshot_id", "v1"),
        "snapshot_version": require_positive_int(snapshot.get("snapshot_version", 1), Code.SNAPSHOT_FIELD, "v1.snapshot_version"),
        "declared_clock_ms": declared_clock,
        "entries": entries,
    }


def _normalize_v2_entry(row: Dict[str, Any], source: str) -> Dict[str, Any]:
    message_key = row.get("message_key")
    if not isinstance(message_key, str) or not message_key:
        raise WorkbenchError(Code.SNAPSHOT_FIELD, "v2 message_key invalid", {"source": source, "row": row})
    effective_at = require_nonnegative_int(row.get("effective_at_ms"), Code.SNAPSHOT_FIELD, "effective_at_ms", message_key=message_key)
    clock = require_nonnegative_int(row.get("logical_clock_ms"), Code.SNAPSHOT_FIELD, "logical_clock_ms", message_key=message_key)
    generation = require_nonnegative_int(row.get("semantic_generation"), Code.SNAPSHOT_FIELD, "semantic_generation", message_key=message_key)
    revision = require_nonnegative_int(row.get("revision", 0), Code.SNAPSHOT_FIELD, "revision", message_key=message_key)
    deps_raw = row.get("upstream_watermark_dependencies", [])
    if not isinstance(deps_raw, list):
        raise WorkbenchError(Code.DEPENDENCY_INVALID, "v2 dependencies must be a list", {"message_key": message_key})
    deps = [_normalize_dependency_ref(ref, {"source": "v2", "message_key": message_key}) for ref in deps_raw]
    tombstone = row.get("tombstone", False)
    if not isinstance(tombstone, bool):
        raise WorkbenchError(Code.SNAPSHOT_FIELD, "tombstone must be boolean", {"message_key": message_key})
    reason = row.get("tombstone_reason")
    if tombstone and (not isinstance(reason, str) or not reason):
        raise WorkbenchError(Code.SNAPSHOT_FIELD, "tombstone_reason is required for tombstones", {"message_key": message_key})
    return {
        "message_key": message_key,
        "effective_at_ms": effective_at,
        "declared_clock_ms": clock,
        "semantic_generation": generation,
        "revision": revision,
        "tombstone": tombstone,
        "tombstone_reason": reason,
        "dependencies": deps,
        "source": "v2",
    }


def _validate_v2_metadata(meta_path: Path, manifest: Dict[str, Any], shards: List[Dict[str, Any]]) -> None:
    if not meta_path.is_file():
        raise WorkbenchError(Code.INPUT_MISSING, "v2 metadata SQLite database is missing", {"path": str(meta_path)})
    try:
        conn = sqlite3.connect(f"file:{meta_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise WorkbenchError(Code.V2_METADATA, "cannot open v2 metadata database", {"error": str(exc)}) from exc
    try:
        row = conn.execute("SELECT * FROM snapshot_meta LIMIT 1").fetchone()
        if row is None:
            raise WorkbenchError(Code.V2_METADATA, "snapshot_meta has no row")
        if int(row["snapshot_version"]) != int(manifest["snapshot_version"]):
            raise WorkbenchError(Code.V2_METADATA, "metadata snapshot_version does not match manifest")
        if int(row["generation_min"]) != int(manifest["generation_min"]):
            raise WorkbenchError(Code.V2_METADATA, "metadata generation_min does not match manifest")
        db_shards = [dict(item) for item in conn.execute("SELECT * FROM shards ORDER BY ordinal").fetchall()]
        if len(db_shards) != len(shards):
            raise WorkbenchError(Code.V2_METADATA, "shard count differs between manifest and metadata")
        for db_row, manifest_shard in zip(db_shards, manifest["shards"]):
            if db_row["sha256"] != manifest_shard["sha256"] or int(db_row["semantic_generation"]) != int(manifest_shard["semantic_generation"]):
                raise WorkbenchError(Code.V2_METADATA, "shard metadata does not match manifest", {"ordinal": manifest_shard["ordinal"]})
        db_entries = conn.execute(
            "SELECT message_key, shard_ordinal, effective_at_ms, logical_clock_ms, semantic_generation, revision, tombstone FROM entry_metadata ORDER BY message_key"
        ).fetchall()
        expected_entries = sorted(
            (
                entry["message_key"],
                shard["ordinal"],
                entry["effective_at_ms"],
                entry["declared_clock_ms"],
                entry["semantic_generation"],
                entry["revision"],
                1 if entry["tombstone"] else 0,
            )
            for shard in shards
            for entry in shard["entries"]
        )
        actual_entries = [tuple(row) for row in db_entries]
        if actual_entries != expected_entries:
            raise WorkbenchError(Code.V2_METADATA, "entry metadata differs from shard JSON")
        db_deps = conn.execute("SELECT message_key, dep_ord, upstream_topic, upstream_partition, upstream_offset, required_watermark FROM upstream_dependencies ORDER BY message_key, dep_ord").fetchall()
        expected_deps: List[Tuple[Any, ...]] = []
        for shard in shards:
            for entry in shard["entries"]:
                for index, dep in enumerate(entry["dependencies"]):
                    expected_deps.append((entry["message_key"], index, dep["topic"], dep["partition"], dep["offset"], dep["required_watermark"]))
        actual_deps = [tuple(row) for row in db_deps]
        if actual_deps != sorted(expected_deps, key=lambda item: (item[0], item[1])):
            raise WorkbenchError(Code.V2_METADATA, "upstream dependencies differ from shard JSON")
        db_tombs = {row["message_key"]: (int(row["semantic_generation"]), row["reason"]) for row in conn.execute("SELECT message_key, semantic_generation, reason FROM tombstones")}
        expected_tombs = {entry["message_key"]: (entry["semantic_generation"], entry["tombstone_reason"]) for shard in shards for entry in shard["entries"] if entry["tombstone"]}
        if db_tombs != expected_tombs:
            raise WorkbenchError(Code.V2_METADATA, "tombstones differ from shard JSON")
    except sqlite3.Error as exc:
        raise WorkbenchError(Code.V2_METADATA, "cannot query v2 metadata database", {"error": str(exc)}) from exc
    finally:
        conn.close()


def _load_v2(semantics_dir: Path) -> Dict[str, Any]:
    manifest_path = semantics_dir / "v2_manifest.json"
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("format") != "v2":
        raise WorkbenchError(Code.SNAPSHOT_FIELD, "v2 manifest format must equal v2")
    snapshot_version = require_positive_int(manifest.get("snapshot_version"), Code.SNAPSHOT_FIELD, "v2.snapshot_version")
    generation_min = require_nonnegative_int(manifest.get("generation_min"), Code.V2_MONOTONIC, "v2.generation_min")
    declared_clock = require_nonnegative_int(manifest.get("declared_clock_ms"), Code.SNAPSHOT_FIELD, "v2.declared_clock_ms")
    shard_refs = manifest.get("shards")
    if not isinstance(shard_refs, list) or not shard_refs:
        raise WorkbenchError(Code.SNAPSHOT_FIELD, "v2 manifest shards must be a non-empty list")
    loaded_shards: List[Dict[str, Any]] = []
    seen_keys = set()
    previous_generation: Optional[int] = None
    previous_hash = "GENESIS"
    expected_ordinal = 0
    for ref in shard_refs:
        if not isinstance(ref, dict):
            raise WorkbenchError(Code.SNAPSHOT_FIELD, "v2 shard reference must be an object")
        ordinal = ref.get("ordinal")
        if ordinal != expected_ordinal:
            raise WorkbenchError(Code.V2_MONOTONIC, "v2 shard ordinals must be contiguous and ordered", {"expected": expected_ordinal, "actual": ordinal})
        expected_ordinal += 1
        shard_generation = require_nonnegative_int(ref.get("semantic_generation"), Code.V2_MONOTONIC, "shard.semantic_generation", ordinal=ordinal)
        if previous_generation is not None and shard_generation < previous_generation:
            raise WorkbenchError(Code.V2_MONOTONIC, "v2 shard semantic generations must be non-decreasing", {"ordinal": ordinal, "previous": previous_generation, "actual": shard_generation})
        if ref.get("prev_sha256") != previous_hash:
            raise WorkbenchError(Code.V2_HASH, "v2 shard prev_sha256 chain is broken", {"ordinal": ordinal, "expected_prev": previous_hash, "actual_prev": ref.get("prev_sha256")})
        rel_path = ref.get("path")
        if not isinstance(rel_path, str):
            raise WorkbenchError(Code.SNAPSHOT_FIELD, "shard path must be a string", {"ordinal": ordinal})
        shard_path = semantics_dir / rel_path
        actual_hash = sha256_file(str(shard_path)) if shard_path.is_file() else None
        if actual_hash is None or actual_hash != ref.get("sha256"):
            raise WorkbenchError(Code.V2_HASH, "v2 shard hash does not match manifest", {"path": str(shard_path), "expected": ref.get("sha256"), "actual": actual_hash})
        shard = _read_json(shard_path)
        if not isinstance(shard, dict) or shard.get("shard_id") != ref.get("shard_id"):
            raise WorkbenchError(Code.SNAPSHOT_FIELD, "shard id mismatch", {"path": str(shard_path)})
        entries_raw = shard.get("entries")
        if not isinstance(entries_raw, list):
            raise WorkbenchError(Code.SNAPSHOT_FIELD, "shard entries must be a list", {"shard_id": ref.get("shard_id")})
        entries = []
        for entry_raw in entries_raw:
            if not isinstance(entry_raw, dict):
                raise WorkbenchError(Code.SNAPSHOT_FIELD, "v2 entry must be an object")
            entry = _normalize_v2_entry(entry_raw, str(shard_path))
            if entry["message_key"] in seen_keys:
                raise WorkbenchError(Code.SNAPSHOT_FIELD, "duplicate v2 message_key", {"message_key": entry["message_key"]})
            seen_keys.add(entry["message_key"])
            entries.append(entry)
        loaded_shards.append(
            {
                "shard_id": ref["shard_id"],
                "ordinal": ordinal,
                "path": rel_path,
                "sha256": actual_hash,
                "prev_sha256": ref["prev_sha256"],
                "semantic_generation": shard_generation,
                "entries": entries,
            }
        )
        previous_generation = shard_generation
        previous_hash = actual_hash
    metadata_rel = manifest.get("metadata_db", "v2_metadata.sqlite")
    _validate_v2_metadata(semantics_dir / metadata_rel, manifest, loaded_shards)
    return {
        "format": "v2",
        "snapshot_id": manifest.get("snapshot_id", "v2"),
        "snapshot_version": snapshot_version,
        "generation_min": generation_min,
        "declared_clock_ms": declared_clock,
        "manifest_hash": canonical_hash({key: value for key, value in manifest.items() if key != "manifest_sha256"}),
        "shards": loaded_shards,
        "entries": [entry for shard in loaded_shards for entry in shard["entries"]],
    }


def _validate_coordinates(bundle: Dict[str, Any]) -> None:
    topics = {topic["name"]: topic for topic in bundle["topics"]}
    coordinates = {(event["topic"], event["partition"], event["offset"]): event["message_key"] for event in bundle["events"]}
    keys = set(coordinates.values())
    all_entries = list(bundle["v1"]["entries"]) + list(bundle["v2"]["entries"])
    for entry in all_entries:
        if entry["message_key"] not in keys:
            raise WorkbenchError(Code.SEMANTIC_KEY_UNKNOWN, "semantic entry does not match a producer event offset", {"message_key": entry["message_key"]})
        for dep in entry["dependencies"]:
            topic = topics.get(dep["topic"])
            if topic is None:
                raise WorkbenchError(Code.DEPENDENCY_INVALID, "dependency references unknown topic", {"dependency": dep, "message_key": entry["message_key"]})
            if dep["partition"] >= topic["partitions"]:
                raise WorkbenchError(Code.DEPENDENCY_INVALID, "dependency partition is outside topic range", {"dependency": dep, "message_key": entry["message_key"]})
            coordinate = (dep["topic"], dep["partition"], dep["offset"])
            if coordinate not in coordinates:
                raise WorkbenchError(Code.DEPENDENCY_INVALID, "dependency references unknown upstream offset", {"dependency": dep, "message_key": entry["message_key"]})
            if dep["required_watermark"] > dep["offset"] + 1:
                raise WorkbenchError(Code.DEPENDENCY_INVALID, "required_watermark cannot exceed inclusive offset plus one", {"dependency": dep, "message_key": entry["message_key"]})
    graph: Dict[str, List[str]] = {entry["message_key"]: [] for entry in all_entries}
    key_to_coord = {event["message_key"]: (event["topic"], event["partition"], event["offset"]) for event in bundle["events"]}
    coord_to_key = coordinates
    for entry in all_entries:
        for dep in entry["dependencies"]:
            upstream_key = coord_to_key[(dep["topic"], dep["partition"], dep["offset"])]
            graph.setdefault(entry["message_key"], []).append(upstream_key)
    state: Dict[str, int] = {}

    def visit(node: str, stack: List[str]) -> None:
        if state.get(node) == 1:
            cycle = stack[stack.index(node):] + [node]
            raise WorkbenchError(Code.DEPENDENCY_CYCLE, "delivery dependency graph contains a cycle", {"cycle": cycle})
        if state.get(node) == 2:
            return
        state[node] = 1
        for nxt in sorted(graph.get(node, [])):
            visit(nxt, stack + [node])
        state[node] = 2

    for node in sorted(graph):
        visit(node, [])


def load_bundle(sample_dir: str | Path) -> Dict[str, Any]:
    root = Path(sample_dir)
    topics = _validate_topics(_read_json(root / "topics.json"))
    topic_names = {topic["name"] for topic in topics}
    subscriptions = _validate_subscriptions(_read_json(root / "subscriptions.json"), topic_names)
    events = _validate_events(_read_jsonl(root / "events.jsonl"), topics)
    semantics_dir = root / "semantics"
    v1 = _normalize_v1(_read_json(semantics_dir / "v1_snapshot.json"))
    v2 = _load_v2(semantics_dir)
    bundle = {
        "sample_dir": str(root),
        "topics": topics,
        "events": events,
        "subscriptions": subscriptions,
        "v1": v1,
        "v2": v2,
    }
    _validate_coordinates(bundle)
    bundle["input_hash"] = canonical_hash(
        {
            "topics": topics,
            "events": events,
            "subscriptions": subscriptions,
            "v1": v1,
            "v2": {key: value for key, value in v2.items() if key != "entries"},
        }
    )
    return bundle
