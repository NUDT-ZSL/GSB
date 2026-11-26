from __future__ import annotations

from typing import Any


CACHE_FIELDS = (
    "content_version",
    "expected_signature",
    "logical_clock",
    "absolute_clock_ms",
    "state",
    "data_present",
    "tombstone",
)


def normalize_cache(cache: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for key in sorted(cache):
        entry = cache[key]
        normalized[key] = {field: entry.get(field) for field in CACHE_FIELDS}
    return normalized


def normalize_named_set(items: list[dict[str, Any]], id_field: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        identity = item[id_field]
        result[identity] = {
            "status": item.get("status"),
            "object_key": item.get("object_key"),
            "content_version": item.get("content_version"),
            "event_seq": item.get("event_seq"),
        }
    return dict(sorted(result.items()))


def _findings_for_key(document: dict[str, Any], key: str) -> list[dict[str, Any]]:
    findings = [item for item in document.get("findings", []) if item.get("object_key") == key]
    return sorted(findings, key=lambda item: item.get("event_seq", 0))


def normalize_clock_timeline(document: dict[str, Any]) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []
    for finding in document.get("findings", []):
        timeline.append(
            {
                "object_key": finding.get("object_key"),
                "event_seq": finding.get("event_seq"),
                "logical_clock": finding.get("logical_clock"),
                "absolute_clock_ms": finding.get("absolute_clock_ms"),
                "content_version": finding.get("content_version"),
                "verdict": finding.get("verdict"),
                "crash_verdict": finding.get("crash_verdict"),
            }
        )
    return sorted(
        timeline,
        key=lambda item: (
            int(item.get("logical_clock") or 0),
            int(item.get("event_seq") or 0),
            str(item.get("object_key", "")),
        ),
    )


def compare_ledger_documents(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    left_name: str = "metadata_first",
    right_name: str = "write_log",
) -> dict[str, Any]:
    left_cache = normalize_cache(left.get("cache", {}))
    right_cache = normalize_cache(right.get("cache", {}))
    left_orphans = normalize_named_set(left.get("orphans", []), "orphan_id")
    right_orphans = normalize_named_set(right.get("orphans", []), "orphan_id")
    left_dirty = normalize_named_set(left.get("dirty", []), "dirty_id")
    right_dirty = normalize_named_set(right.get("dirty", []), "dirty_id")
    left_timeline = normalize_clock_timeline(left)
    right_timeline = normalize_clock_timeline(right)

    divergences: list[dict[str, Any]] = []
    all_keys = sorted(set(left_cache) | set(right_cache))
    first_key: str | None = None
    for key in all_keys:
        left_entry = left_cache.get(key)
        right_entry = right_cache.get(key)
        if left_entry != right_entry:
            changed_fields = sorted(
                field for field in CACHE_FIELDS if (left_entry or {}).get(field) != (right_entry or {}).get(field)
            )
            divergence = {
                "type": "cache_state",
                "object_key": key,
                "changed_fields": changed_fields,
                left_name: left_entry,
                right_name: right_entry,
            }
            divergences.append(divergence)
            if first_key is None:
                first_key = key

    for set_name, left_set, right_set in (
        ("orphan_set", left_orphans, right_orphans),
        ("dirty_set", left_dirty, right_dirty),
    ):
        for identity in sorted(set(left_set) | set(right_set)):
            if left_set.get(identity) != right_set.get(identity):
                item = {
                    "type": set_name,
                    "identity": identity,
                    "object_key": (left_set.get(identity) or right_set.get(identity) or {}).get("object_key"),
                    left_name: left_set.get(identity),
                    right_name: right_set.get(identity),
                }
                divergences.append(item)
                if first_key is None and item["object_key"]:
                    first_key = item["object_key"]

    watermark_fields = ("seq_watermark", "clock_watermark", "completed_seqs")
    watermark_differences = {
        field: {left_name: left.get(field), right_name: right.get(field)}
        for field in watermark_fields
        if left.get(field) != right.get(field)
    }
    if watermark_differences:
        divergences.append({"type": "watermark", "differences": watermark_differences})

    if left_timeline != right_timeline:
        first_clock_difference = None
        for index, (left_item, right_item) in enumerate(zip(left_timeline, right_timeline)):
            if left_item != right_item:
                first_clock_difference = {
                    "index": index,
                    left_name: left_item,
                    right_name: right_item,
                }
                break
        if first_clock_difference is None and len(left_timeline) != len(right_timeline):
            first_clock_difference = {
                "length_difference": {left_name: len(left_timeline), right_name: len(right_timeline)}
            }
        divergences.append(
            {"type": "clock_timeline", "first_difference": first_clock_difference}
        )

    evidence = None
    if first_key is not None:
        evidence = {
            "first_divergence_object_key": first_key,
            left_name: {
                "cache": left_cache.get(first_key),
                "findings": _findings_for_key(left, first_key),
            },
            right_name: {
                "cache": right_cache.get(first_key),
                "findings": _findings_for_key(right, first_key),
            },
            "trigger_evidence": {
                left_name: [
                    finding.get("fault")
                    for finding in _findings_for_key(left, first_key)
                    if finding.get("fault")
                ],
                right_name: [
                    finding.get("fault")
                    for finding in _findings_for_key(right, first_key)
                    if finding.get("fault")
                ],
            },
        }

    return {
        "schema": "write-path-comparison-v1",
        "left_policy": left_name,
        "right_policy": right_name,
        "consistent": not divergences,
        "first_divergence_object_key": first_key,
        "divergence_count": len(divergences),
        "divergences": divergences,
        "first_divergence_evidence": evidence,
        "normalized_views": {
            left_name: {
                "cache": left_cache,
                "orphans": left_orphans,
                "dirty": left_dirty,
                "seq_watermark": left.get("seq_watermark"),
                "clock_watermark": left.get("clock_watermark"),
                "clock_timeline": left_timeline,
            },
            right_name: {
                "cache": right_cache,
                "orphans": right_orphans,
                "dirty": right_dirty,
                "seq_watermark": right.get("seq_watermark"),
                "clock_watermark": right.get("clock_watermark"),
                "clock_timeline": right_timeline,
            },
        },
    }
