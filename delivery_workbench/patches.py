from __future__ import annotations

from typing import Any, Dict, List, Optional

from .canon import canonical_hash
from .watermark import closure_from_roots


def assign_change_sequences(
    changes: List[Dict[str, Any]],
    existing_topic_seq: Optional[Dict[str, int]] = None,
    existing_global_seq: int = 0,
) -> List[Dict[str, Any]]:
    topic_counters = dict(existing_topic_seq or {})
    global_seq = existing_global_seq
    assigned: List[Dict[str, Any]] = []
    for change in sorted(changes, key=lambda item: item["message_key"]):
        topic = change["topic"]
        topic_counters[topic] = topic_counters.get(topic, 0) + 1
        global_seq += 1
        item = dict(change)
        item["topic_seq"] = topic_counters[topic]
        item["global_seq"] = global_seq
        assigned.append(item)
    return assigned


def build_patch_plan(
    bundle: Dict[str, Any],
    view: Dict[str, Any],
    change_events: List[Dict[str, Any]],
    cursors: Dict[str, int],
    closure: Optional[Dict[str, Any]] = None,
    semantic_version_token: Optional[str] = None,
    subscriber_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    entry_by_key = {entry["message_key"]: entry for entry in view["entries"]}
    roots = [event["message_key"] for event in change_events]
    effective_closure = closure or closure_from_roots(roots, entry_by_key)
    patches: List[Dict[str, Any]] = []
    subscribers = sorted(bundle["subscriptions"], key=lambda sub: sub["subscriber_id"])
    if subscriber_filter is not None:
        subscribers = [sub for sub in subscribers if sub["subscriber_id"] == subscriber_filter]
    for subscription in subscribers:
        subscriber_id = subscription["subscriber_id"]
        topic = subscription["topic"]
        cursor = int(cursors.get(subscriber_id, 0))
        pending = sorted(
            (event for event in change_events if event["topic"] == topic and int(event["topic_seq"]) > cursor),
            key=lambda event: event["topic_seq"],
        )
        if not pending:
            continue
        batch_size = subscription["pull_batch_size"]
        for chunk_index in range(0, len(pending), batch_size):
            chunk = pending[chunk_index : chunk_index + batch_size]
            from_seq = cursor + 1 if chunk_index == 0 else chunk[0]["topic_seq"]
            to_seq = chunk[-1]["topic_seq"]
            groups = []
            upstream_offsets = []
            for change in chunk:
                key = change["message_key"]
                entry = entry_by_key.get(key, {})
                impact = effective_closure.get("root_impacts", {}).get(key, {}).get("representative_path")
                if impact is None:
                    single = closure_from_roots([key], entry_by_key)
                    impact = single["root_impacts"][key]["representative_path"]
                deps = entry.get("dependencies", [])
                upstream_offsets.extend(deps)
                groups.append(
                    {
                        "topic_seq": change["topic_seq"],
                        "global_seq": change["global_seq"],
                        "message_key": key,
                        "partition": change["partition"],
                        "offset": change["offset"],
                        "change_kind": change["change_kind"],
                        "trigger_reasons": change["trigger_reasons"],
                        "trigger_semantic_version": change["trigger_semantic_version"],
                        "upstream_offsets": deps,
                        "impact_path": impact,
                        "affected_partitions": effective_closure.get("affected_partitions", []),
                        "semantic_generation": entry.get("semantic_generation"),
                        "revision": entry.get("revision"),
                        "tombstone": entry.get("tombstone", False),
                        "payload_digest": _payload_digest(bundle, key),
                    }
                )
            impact_paths = [group["impact_path"] for group in groups]
            top_impact_path = max(impact_paths, key=lambda path: (len(path), path))
            patch = {
                "patch_id": f"PATCH:{subscriber_id}:{topic}:{from_seq:06d}-{to_seq:06d}:{chunk_index // batch_size:03d}",
                "subscriber_id": subscriber_id,
                "topic": topic,
                "base_cursor": cursor if chunk_index == 0 else from_seq - 1,
                "from_topic_seq": from_seq,
                "to_topic_seq": to_seq,
                "next_cursor": to_seq,
                "chunk_index": chunk_index // batch_size,
                "pull_batch_size": batch_size,
                "trigger_semantic_version": semantic_version_token or view["view_id"],
                "upstream_offsets": sorted(
                    {canonical_hash(dep): dep for dep in upstream_offsets}.values(),
                    key=lambda dep: (dep["topic"], dep["partition"], dep["offset"]),
                ),
                "impact_path": top_impact_path,
                "impact_paths": impact_paths,
                "groups": groups,
                "semantic_view_hash": view["content_hash"],
            }
            patch["content_hash"] = canonical_hash({key: value for key, value in patch.items() if key != "content_hash"})
            patches.append(patch)
    return sorted(patches, key=lambda patch: patch["patch_id"])


def _payload_digest(bundle: Dict[str, Any], message_key: str) -> str:
    for event in bundle["events"]:
        if event["message_key"] == message_key:
            return event["payload_digest"] or canonical_hash(event["payload_summary"])
    return ""
