"""Canonical merged policy view and time-aware policy selection."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .constants import CODE_POLICY_CONFLICT


def _semantic_key(candidate: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        candidate["policy_key"],
        candidate["generation"],
        candidate["effective_at_ms"],
        candidate.get("max_mask"),
        tuple(candidate.get("allowed_masks") or []),
        candidate.get("required_mask"),
        tuple(candidate.get("map_kinds") or []),
        tuple(candidate.get("effective_window_ms") or []),
        bool(candidate.get("tombstone")),
        bool(candidate.get("disabled")),
    )


def _public_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "policy_key": candidate["policy_key"],
        "generation": candidate["generation"],
        "effective_at_ms": candidate["effective_at_ms"],
        "effective_interval_ms": candidate.get("effective_interval_ms"),
        "time_quantization_error_bound_ms": candidate.get(
            "time_quantization_error_bound_ms", 0
        ),
        "effective_window_ms": candidate.get("effective_window_ms"),
        "max_mask": candidate.get("max_mask"),
        "allowed_masks": candidate.get("allowed_masks"),
        "required_mask": candidate.get("required_mask"),
        "map_kinds": candidate.get("map_kinds"),
        "tombstone": candidate.get("tombstone", False),
        "disabled": candidate.get("disabled", False),
        "target": candidate.get("target", {}),
        "source": candidate.get("source", {}),
        "sources": candidate.get("sources", [candidate.get("source", {})]),
    }


def _diff_fields(a: Dict[str, Any], b: Dict[str, Any]) -> List[str]:
    fields = []
    if a.get("max_mask") != b.get("max_mask") or a.get(
        "allowed_masks"
    ) != b.get("allowed_masks"):
        fields.append("max_protection")
    if a.get("map_kinds") != b.get("map_kinds"):
        fields.append("map_kinds")
    if a.get("effective_window_ms") != b.get("effective_window_ms"):
        fields.append("effective_window")
    if a.get("tombstone") != b.get("tombstone"):
        fields.append("tombstone")
    if a.get("disabled") != b.get("disabled"):
        fields.append("disabled")
    return fields


def build_policy_view(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate["policy_key"], []).append(candidate)

    keys: Dict[str, Dict[str, Any]] = {}
    conflicts: List[Dict[str, Any]] = []

    for policy_key, items in sorted(grouped.items()):
        deduped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        for candidate in items:
            identity = _semantic_key(candidate)
            if identity not in deduped:
                candidate = dict(candidate)
                candidate["sources"] = [candidate.get("source", {})]
                deduped[identity] = candidate
            else:
                existing = deduped[identity]
                existing.setdefault("sources", []).append(candidate.get("source", {}))

        records = [_public_candidate(item) for item in deduped.values()]
        records.sort(
            key=lambda item: (
                item["generation"],
                item["effective_at_ms"],
                1 if item["tombstone"] else 0,
            )
        )

        key_conflicts = []
        for index, left in enumerate(records):
            for right in records[index + 1 :]:
                fields = _diff_fields(left, right)
                if fields:
                    conflict = {
                        "business_code": CODE_POLICY_CONFLICT,
                        "policy_key": policy_key,
                        "fields": fields,
                        "side_a": left,
                        "side_b": right,
                        "resolution_rule": (
                            "authority is max(generation, effective_at_ms, "
                            "tombstone_precedence); both bases are retained"
                        ),
                    }
                    key_conflicts.append(conflict)
                    conflicts.append(conflict)

        authority = records[-1] if records else None
        unresolved = False
        if len(records) >= 2:
            top_rank = (
                records[-1]["generation"],
                records[-1]["effective_at_ms"],
                records[-1]["tombstone"],
            )
            tied = [
                record
                for record in records
                if (
                    record["generation"],
                    record["effective_at_ms"],
                    record["tombstone"],
                )
                == top_rank
            ]
            if len({_semantic_key(record) for record in tied}) > 1:
                unresolved = True

        keys[policy_key] = {
            "policy_key": policy_key,
            "authority": authority,
            "unresolved_conflict": unresolved,
            "timeline": records,
            "conflicts": key_conflicts,
        }

    return {
        "schema": "memguard-policy-view-v1",
        "algebra_version": "policy-algebra-v2",
        "keys": keys,
        "conflicts": conflicts,
    }


def resolve_policy_key(
    view: Dict[str, Any], region: Dict[str, Any]
) -> Optional[str]:
    explicit = region.get("policy_key")
    if explicit and explicit in view.get("keys", {}):
        return explicit

    endpoint = region.get("endpoint_id")
    process_guid = region.get("process_guid")
    module = region.get("module")
    map_kind = region.get("map_kind")
    matches = []
    for key, entry in view.get("keys", {}).items():
        for record in entry.get("timeline", []):
            target = record.get("target") or {}
            if target.get("endpoint_id", endpoint) not in (endpoint, "*"):
                continue
            if target.get("process_guid", process_guid) not in (process_guid, "*"):
                continue
            if target.get("module", module) not in (module, "*"):
                continue
            kinds = record.get("map_kinds")
            if kinds and map_kind not in kinds:
                continue
            matches.append(key)
            break
    return sorted(matches)[0] if matches else explicit


def _window_contains(record: Dict[str, Any], at_ms: int) -> bool:
    window = record.get("effective_window_ms")
    if not window:
        return True
    start, end = window
    return start <= at_ms < end


def select_policy(
    view: Dict[str, Any],
    policy_key: Optional[str],
    at_ms: int,
    map_kind: Optional[str] = None,
) -> Dict[str, Any]:
    if not policy_key or policy_key not in view.get("keys", {}):
        return {"status": "missing", "policy_key": policy_key}

    entry = view["keys"][policy_key]
    applicable = []
    for record in entry.get("timeline", []):
        if record["effective_at_ms"] > at_ms:
            continue
        if not _window_contains(record, at_ms):
            continue
        kinds = record.get("map_kinds")
        if kinds and map_kind and map_kind not in kinds:
            continue
        applicable.append(record)

    if not applicable:
        return {
            "status": "missing",
            "policy_key": policy_key,
            "entry": entry,
        }

    applicable.sort(
        key=lambda record: (
            record["generation"],
            record["effective_at_ms"],
            1 if record.get("tombstone") else 0,
        )
    )
    authority = applicable[-1]
    top_rank = (
        authority["generation"],
        authority["effective_at_ms"],
        authority.get("tombstone", False),
    )
    tied = [
        record
        for record in applicable
        if (
            record["generation"],
            record["effective_at_ms"],
            record.get("tombstone", False),
        )
        == top_rank
    ]
    if len({_semantic_key(record) for record in tied}) > 1:
        return {
            "status": "conflict",
            "policy_key": policy_key,
            "candidates": tied,
        }

    time_bound = int(authority.get("time_quantization_error_bound_ms", 0) or 0)
    time_boundary = bool(
        time_bound
        and authority["effective_at_ms"]
        <= at_ms
        < authority["effective_at_ms"] + time_bound
    )

    if authority.get("tombstone"):
        status = "tombstoned"
    elif authority.get("disabled"):
        status = "disabled"
    else:
        status = "active"

    allowed_masks = authority.get("allowed_masks")
    if allowed_masks is None and authority.get("max_mask") is not None:
        max_mask = int(authority["max_mask"])
        allowed_masks = [
            mask for mask in range(max_mask + 1) if (mask & ~max_mask) == 0
        ]
    elif allowed_masks is None:
        allowed_masks = []

    return {
        "status": status,
        "policy_key": policy_key,
        "authority": authority,
        "generation": authority["generation"],
        "allowed_masks_resolved": sorted(set(int(m) for m in allowed_masks)),
        "time_boundary_uncertain": time_boundary,
        "time_quantization_error_bound_ms": time_bound,
    }
