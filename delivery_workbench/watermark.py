from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from .canon import canonical_hash


def partition_key(topic: str, partition: int) -> str:
    return f"{topic}/{partition}"


def coordinate_message_key(ref: Dict[str, Any]) -> str:
    return partition_key(ref["topic"], ref["partition"]) + f"/{ref['offset']}"


def index_bundle(bundle: Dict[str, Any], view: Dict[str, Any]) -> Dict[str, Any]:
    entry_by_key = {entry["message_key"]: entry for entry in view["entries"]}
    event_by_key = {event["message_key"]: event for event in bundle["events"]}
    events_by_partition: Dict[str, List[Dict[str, Any]]] = {}
    for event in bundle["events"]:
        pk = partition_key(event["topic"], event["partition"])
        events_by_partition.setdefault(pk, []).append(event)
    for rows in events_by_partition.values():
        rows.sort(key=lambda event: event["offset"])
    topic_by_name = {topic["name"]: topic for topic in bundle["topics"]}
    partition_keys = sorted(
        partition_key(topic["name"], partition_id)
        for topic in bundle["topics"]
        for partition_id in range(topic["partitions"])
    )
    return {
        "entry_by_key": entry_by_key,
        "event_by_key": event_by_key,
        "events_by_partition": events_by_partition,
        "topic_by_name": topic_by_name,
        "partition_keys": partition_keys,
    }


def build_reverse_graph(entry_by_key: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    reverse: Dict[str, List[str]] = {}
    for key, entry in entry_by_key.items():
        if not entry.get("present"):
            continue
        for dep in entry.get("dependencies", []):
            upstream_key = coordinate_message_key(dep)
            reverse.setdefault(upstream_key, []).append(key)
    for values in reverse.values():
        values.sort()
    return reverse


def closure_from_roots(root_keys: List[str], entry_by_key: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    reverse = build_reverse_graph(entry_by_key)
    roots = sorted(set(root_keys))
    affected: Set[str] = set(roots)
    edges: Set[Tuple[str, str]] = set()
    queue = list(roots)
    while queue:
        current = queue.pop(0)
        for nxt in reverse.get(current, []):
            edge = (current, nxt)
            edges.add(edge)
            if nxt not in affected:
                affected.add(nxt)
                queue.append(nxt)

    longest_memo: Dict[str, List[str]] = {}

    def longest_path(node: str, visiting: Optional[Set[str]] = None) -> List[str]:
        if node in longest_memo:
            return longest_memo[node]
        visiting = visiting or set()
        best = [node]
        for nxt in sorted(reverse.get(node, [])):
            if nxt in visiting:
                continue
            candidate = [node] + longest_path(nxt, visiting | {node})
            if len(candidate) > len(best) or (len(candidate) == len(best) and candidate < best):
                best = candidate
        longest_memo[node] = best
        return best

    paths: Dict[str, List[str]] = {}
    root_impacts: Dict[str, Dict[str, Any]] = {}
    affected_partitions: Set[str] = set()
    for key in sorted(affected):
        entry = entry_by_key.get(key, {})
        if entry.get("topic") is not None:
            affected_partitions.add(partition_key(entry["topic"], entry["partition"]))
    for root in roots:
        path = longest_path(root)
        root_impacts[root] = {
            "representative_path": path,
            "reaches_additional_nodes": len(path) > 1,
            "affected_partitions_on_path": sorted(
                {
                    partition_key(entry_by_key[node]["topic"], entry_by_key[node]["partition"])
                    for node in path
                    if node in entry_by_key and entry_by_key[node].get("topic") is not None
                }
            ),
        }
    for node in sorted(affected):
        best_path = [node]
        for root in roots:
            candidate = _path_between(root, node, reverse)
            if candidate and (len(candidate) > len(best_path) or (len(candidate) == len(best_path) and candidate < best_path)):
                best_path = candidate
        paths[node] = best_path
    return {
        "root_keys": roots,
        "affected_keys": sorted(affected),
        "affected_partitions": sorted(affected_partitions),
        "edges": [{"from": left, "to": right, "relation": "upstream_offset_watermark"} for left, right in sorted(edges)],
        "paths": paths,
        "root_impacts": root_impacts,
    }


def _path_between(root: str, target: str, reverse: Dict[str, List[str]]) -> Optional[List[str]]:
    if root == target:
        return [root]
    queue: List[Tuple[str, List[str]]] = [(root, [root])]
    seen = {root}
    while queue:
        current, path = queue.pop(0)
        for nxt in reverse.get(current, []):
            if nxt in seen:
                continue
            next_path = path + [nxt]
            if nxt == target:
                return next_path
            seen.add(nxt)
            queue.append((nxt, next_path))
    return None


def evaluate_partition(
    pk: str,
    events: List[Dict[str, Any]],
    entry_by_key: Dict[str, Dict[str, Any]],
    topic: Dict[str, Any],
    watermarks: Dict[str, int],
    as_of_clock_ms: int,
    semantic_view_hash: str,
) -> Dict[str, Any]:
    blocker: Optional[Dict[str, Any]] = None
    watermark = len(events)
    for position, event in enumerate(events):
        key = event["message_key"]
        entry = entry_by_key.get(key)
        if entry is None or not entry.get("present"):
            blocker = {"offset": event["offset"], "message_key": key, "code": "WB_WM_MISSING_SEMANTICS", "reason": "no merged semantic entry"}
            watermark = position
            break
        if entry.get("effective_at_ms", 0) > as_of_clock_ms:
            blocker = {
                "offset": event["offset"],
                "message_key": key,
                "code": "WB_WM_FUTURE_SEMANTICS",
                "reason": "semantic effective time is after logical as-of clock",
                "effective_at_ms": entry["effective_at_ms"],
                "as_of_clock_ms": as_of_clock_ms,
            }
            watermark = position
            break
        if entry.get("tombstone"):
            blocker = {
                "offset": event["offset"],
                "message_key": key,
                "code": "WB_WM_TOMBSTONE",
                "reason": entry.get("tombstone_reason") or "semantic tombstone",
                "semantic_generation": entry.get("semantic_generation"),
            }
            watermark = position
            break
        if entry.get("semantic_generation", 0) < event["expected_generation"] or entry.get("revision", 0) < event["revision"]:
            blocker = {
                "offset": event["offset"],
                "message_key": key,
                "code": "WB_WM_STALE_GENERATION",
                "reason": "semantic generation/revision is older than event expectation",
                "semantic_generation": entry.get("semantic_generation"),
                "semantic_revision": entry.get("revision"),
                "expected_generation": event["expected_generation"],
                "expected_revision": event["revision"],
            }
            watermark = position
            break
        for dep in entry.get("dependencies", []):
            upstream_pk = partition_key(dep["topic"], dep["partition"])
            actual = watermarks.get(upstream_pk, 0)
            required = dep.get("required_watermark", dep["offset"] + 1)
            if actual < required:
                blocker = {
                    "offset": event["offset"],
                    "message_key": key,
                    "code": "WB_WM_UPSTREAM_WATERMARK",
                    "reason": "upstream offset watermark dependency is not satisfied",
                    "dependency": dep,
                    "upstream_partition": upstream_pk,
                    "required_watermark": required,
                    "actual_watermark": actual,
                }
                watermark = position
                break
        if blocker is not None:
            watermark = position
            break
    backlog = len(events) - watermark
    record = {
        "partition_key": pk,
        "topic": event_topic(pk),
        "partition": int(pk.rsplit("/", 1)[1]),
        "watermark_offset": watermark,
        "delivered_through_offset": None if watermark == 0 else watermark - 1,
        "event_count": len(events),
        "latest_offset": len(events) - 1,
        "backlog": backlog,
        "backlog_limit": topic["backlog_limit"],
        "backlog_limit_exceeded": backlog > topic["backlog_limit"],
        "status": "blocked" if blocker else "deliverable",
        "blocker": blocker,
        "as_of_clock_ms": as_of_clock_ms,
        "semantic_view_hash": semantic_view_hash,
    }
    if record["backlog_limit_exceeded"] and record["status"] == "deliverable":
        record["status"] = "backlog_limit_exceeded"
    record["diagnostic_outcome"] = "failed" if blocker or record["backlog_limit_exceeded"] else "succeeded"
    record["content_hash"] = canonical_hash({key: value for key, value in record.items() if key != "content_hash"})
    return record


def event_topic(pk: str) -> str:
    return pk.rsplit("/", 1)[0]


def compute_all_watermarks(bundle: Dict[str, Any], view: Dict[str, Any], as_of_clock_ms: int) -> Dict[str, Dict[str, Any]]:
    index = index_bundle(bundle, view)
    watermarks: Dict[str, int] = {pk: len(index["events_by_partition"].get(pk, [])) for pk in index["partition_keys"]}
    records: Dict[str, Dict[str, Any]] = {}
    for _ in range(max(1, len(bundle["events"]) * 2 + 4)):
        next_records: Dict[str, Dict[str, Any]] = {}
        next_watermarks: Dict[str, int] = {}
        for pk in index["partition_keys"]:
            topic_name, partition_text = pk.rsplit("/", 1)
            topic = index["topic_by_name"][topic_name]
            record = evaluate_partition(
                pk,
                index["events_by_partition"].get(pk, []),
                index["entry_by_key"],
                topic,
                watermarks,
                as_of_clock_ms,
                view["content_hash"],
            )
            next_records[pk] = record
            next_watermarks[pk] = record["watermark_offset"]
        if next_watermarks == watermarks:
            return next_records
        watermarks = next_watermarks
        records = next_records
    return records


def compute_incremental_watermarks(
    bundle: Dict[str, Any],
    view: Dict[str, Any],
    root_keys: List[str],
    previous_records: Dict[str, Dict[str, Any]],
    as_of_clock_ms: int,
) -> Dict[str, Any]:
    index = index_bundle(bundle, view)
    entry_by_key = index["entry_by_key"]
    closure = closure_from_roots(root_keys, entry_by_key)
    affected_partitions = set(closure["affected_partitions"])
    watermarks: Dict[str, int] = {pk: record["watermark_offset"] for pk, record in previous_records.items()}
    for pk in index["partition_keys"]:
        watermarks.setdefault(pk, len(index["events_by_partition"].get(pk, [])))

    records: Dict[str, Dict[str, Any]] = dict(previous_records)
    for _outer in range(len(index["partition_keys"]) + 2):
        for _inner in range(len(bundle["events"]) * 2 + 4):
            changed = False
            for pk in sorted(affected_partitions):
                topic_name = pk.rsplit("/", 1)[0]
                record = evaluate_partition(
                    pk,
                    index["events_by_partition"].get(pk, []),
                    entry_by_key,
                    index["topic_by_name"][topic_name],
                    watermarks,
                    as_of_clock_ms,
                    view["content_hash"],
                )
                if records.get(pk) != record:
                    records[pk] = record
                if watermarks[pk] != record["watermark_offset"]:
                    watermarks[pk] = record["watermark_offset"]
                    changed = True
            if not changed:
                break
        expansion_nodes: Set[str] = set()
        for entry in view["entries"]:
            if not entry.get("present") or entry["message_key"] in closure["affected_keys"]:
                continue
            for dep in entry.get("dependencies", []):
                upstream_pk = partition_key(dep["topic"], dep["partition"])
                required = dep.get("required_watermark", dep["offset"] + 1)
                if upstream_pk in affected_partitions and watermarks.get(upstream_pk, 0) < required:
                    expansion_nodes.add(entry["message_key"])
        if not expansion_nodes:
            break
        expanded = closure_from_roots(closure["root_keys"] + sorted(expansion_nodes), entry_by_key)
        closure = expanded
        new_partitions = set(closure["affected_partitions"]) - affected_partitions
        affected_partitions = set(closure["affected_partitions"])
        for pk in new_partitions:
            if pk in previous_records:
                watermarks[pk] = previous_records[pk]["watermark_offset"]
    recomputed = {pk: records[pk] for pk in sorted(affected_partitions) if pk in records}
    reused = sorted(pk for pk in index["partition_keys"] if pk not in affected_partitions)
    return {
        "closure": closure,
        "affected_partitions": sorted(affected_partitions),
        "reused_partitions": reused,
        "records": recomputed,
        "all_records": records,
    }
