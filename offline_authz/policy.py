"""Canonical policy normalization, authority selection and conflict detection."""

from __future__ import annotations

from typing import Any

from .codes import (
    E_INVALID_EXPIRY_SECONDS,
    E_INVALID_FIELD,
    E_INVALID_POLICY_GENERATION,
    E_INVALID_TIMESTAMP_MS,
    E_POLICY_CONFLICT,
)
from .parsing import FACTOR_TYPES, Bundle, rejection, valid_id


def canonical_policy_key(raw_key: str) -> str:
    if raw_key.startswith("policy:"):
        return raw_key
    return f"policy:{raw_key}"


def _factor_list(value: Any) -> list[str] | None:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = value
    else:
        return None
    if any(item not in FACTOR_TYPES for item in values):
        return None
    return sorted(set(values))


def normalize_v1_policy(
    record: dict[str, Any], seq: int, observation_clock_ms: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    category = "policy_v1"
    key = record.get("policy_key")
    if not valid_id(key):
        return None, rejection(category, seq, E_INVALID_FIELD, "invalid policy_key")
    expires_in = record.get("expires_in_seconds")
    if not (isinstance(expires_in, int) and not isinstance(expires_in, bool) and expires_in >= 0):
        return None, rejection(category, seq, E_INVALID_EXPIRY_SECONDS, "expires_in_seconds must be nonnegative integer", actual=expires_in)
    factors = _factor_list(record.get("required_factors", []))
    if factors is None:
        return None, rejection(category, seq, E_INVALID_FIELD, "required_factors contains unsupported type")
    binding = record.get("binding_requirement", "NONE")
    if binding not in {"NONE", "CANONICAL_FINGERPRINT"}:
        return None, rejection(category, seq, E_INVALID_FIELD, "invalid binding_requirement")
    generation = record.get("generation", 0)
    if not (isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0):
        return None, rejection(category, seq, E_INVALID_POLICY_GENERATION, "generation must be nonnegative")
    tier = record.get("min_digest_tier", 1)
    iterations = record.get("min_iterations", 1)
    if not (isinstance(tier, int) and tier > 0 and isinstance(iterations, int) and iterations > 0):
        return None, rejection(category, seq, E_INVALID_FIELD, "min_digest_tier/min_iterations must be positive")
    effective_at = record.get("effective_at_ms", observation_clock_ms)
    if not (isinstance(effective_at, int) and effective_at >= 0):
        return None, rejection(category, seq, E_INVALID_TIMESTAMP_MS, "effective_at_ms must be nonnegative")
    return {
        "policy_key": key,
        "canonical_policy_key": canonical_policy_key(key),
        "generation": generation,
        "effective_at_ms": effective_at,
        "expires_at_ms": observation_clock_ms + expires_in * 1000,
        "required_factors": factors,
        "binding_requirement": binding,
        "min_digest_tier": tier,
        "min_iterations": iterations,
        "tombstoned": False,
        "batch_watermark": record.get("batch_watermark", 1),
        "source_category": category,
        "source": f"policy_v1#record-{seq}",
        "record_seq": seq,
        "source_priority": 1,
    }, None


def normalize_v2_policy(record: dict[str, Any], seq: int, source: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    category = "policy_v2"
    key = record.get("policy_key")
    if not valid_id(key):
        return None, rejection(category, seq, E_INVALID_FIELD, "invalid policy_key", source_file=record.get("_source_file"))
    generation = record.get("generation")
    if not (isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0):
        return None, rejection(category, seq, E_INVALID_POLICY_GENERATION, "generation must be nonnegative integer")
    effective = record.get("effective_at_ms")
    if not (isinstance(effective, int) and not isinstance(effective, bool) and effective >= 0):
        return None, rejection(category, seq, E_INVALID_TIMESTAMP_MS, "effective_at_ms must be nonnegative integer")
    expires = record.get("expires_at_ms")
    if expires is not None and not (isinstance(expires, int) and not isinstance(expires, bool) and expires >= 0):
        return None, rejection(category, seq, E_INVALID_TIMESTAMP_MS, "expires_at_ms must be nonnegative integer")
    factors = _factor_list(record.get("required_factors", []))
    if factors is None:
        return None, rejection(category, seq, E_INVALID_FIELD, "required_factors contains unsupported type")
    binding = record.get("binding_requirement", "NONE")
    if binding not in {"NONE", "CANONICAL_FINGERPRINT"}:
        return None, rejection(category, seq, E_INVALID_FIELD, "invalid binding_requirement")
    tier = record.get("min_digest_tier", 1)
    iterations = record.get("min_iterations", 1)
    if not (isinstance(tier, int) and tier > 0 and isinstance(iterations, int) and iterations > 0):
        return None, rejection(category, seq, E_INVALID_FIELD, "min thresholds must be positive")
    return {
        "policy_key": key,
        "canonical_policy_key": canonical_policy_key(key),
        "generation": generation,
        "effective_at_ms": effective,
        "expires_at_ms": expires,
        "required_factors": factors,
        "binding_requirement": binding,
        "min_digest_tier": tier,
        "min_iterations": iterations,
        "tombstoned": bool(record.get("tombstoned", False)),
        "batch_watermark": record.get("batch_watermark", 1),
        "source_category": category if "sqlite" not in source else "policy_v2_sqlite",
        "source": source,
        "record_seq": seq,
        "source_priority": 2,
    }, None


def normalize_binding(record: dict[str, Any], seq: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    category = "device_binding_v2"
    binding_id = record.get("binding_id")
    policy_key = record.get("policy_key")
    user_id = record.get("user_id")
    device_id = record.get("device_id")
    fp_key = record.get("canonical_fingerprint_key")
    if not valid_id(binding_id) or not valid_id(policy_key) or not valid_id(user_id) or not valid_id(device_id):
        return None, rejection(category, seq, E_INVALID_FIELD, "binding identifiers are invalid")
    if not isinstance(fp_key, str) or not fp_key:
        return None, rejection(category, seq, E_INVALID_FIELD, "canonical_fingerprint_key required")
    generation = record.get("generation")
    effective = record.get("effective_at_ms")
    if not (isinstance(generation, int) and generation >= 0):
        return None, rejection(category, seq, E_INVALID_POLICY_GENERATION, "binding generation must be nonnegative")
    if not (isinstance(effective, int) and effective >= 0):
        return None, rejection(category, seq, E_INVALID_TIMESTAMP_MS, "effective_at_ms must be nonnegative")
    return {
        "binding_id": binding_id,
        "canonical_binding_key": f"binding:{binding_id}",
        "policy_key": policy_key,
        "canonical_policy_key": canonical_policy_key(policy_key),
        "user_id": user_id,
        "device_id": device_id,
        "canonical_fingerprint_key": fp_key,
        "canonical_vector": record.get("canonical_vector"),
        "generation": generation,
        "effective_at_ms": effective,
        "tombstoned": bool(record.get("tombstoned", False)),
        "batch_watermark": record.get("batch_watermark", 1),
        "source": record.get("_source", f"policy_v2_binding#{seq}"),
        "record_seq": seq,
    }, None


def build_unified_view(bundle: Bundle) -> dict[str, Any]:
    revisions: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    if bundle.v1_document:
        raw_policies = bundle.v1_document.get("policies", [])
        if isinstance(bundle.v1_document, dict) and "policy_key" in bundle.v1_document:
            raw_policies = [bundle.v1_document]
        if not isinstance(raw_policies, list):
            bundle.rejections.append(rejection("policy_v1", 0, E_INVALID_FIELD, "policies must be a list"))
        else:
            for seq, raw in enumerate(raw_policies, start=1):
                if not isinstance(raw, dict):
                    bundle.rejections.append(rejection("policy_v1", seq, E_INVALID_FIELD, "policy must be object"))
                    continue
                normalized, error = normalize_v1_policy(raw, seq, bundle.observation_clock_ms)
                if error:
                    bundle.rejections.append(error)
                elif normalized:
                    revisions.append(normalized)

    for raw in bundle.v2_shard_records:
        seq = int(raw.get("_source_seq", 0))
        source = str(raw.get("_source", "policy_v2"))
        kind = raw.get("record_kind", "policy")
        if kind == "binding":
            normalized, error = normalize_binding(raw, seq)
        else:
            normalized, error = normalize_v2_policy(raw, seq, source)
        if error:
            bundle.rejections.append(error)
        elif normalized:
            if kind == "binding":
                bindings.append(normalized)
            else:
                revisions.append(normalized)

    for raw in bundle.v2_meta_rows:
        if int(raw.get("tombstoned", 0) or 0) == 1:
            normalized, error = normalize_v2_policy(raw, int(raw.get("record_seq") or raw.get("_source_seq", 0)), raw["_source"])
            if error:
                bundle.rejections.append(error)
            elif normalized:
                normalized["tombstoned"] = True
                normalized["source_category"] = "policy_v2_sqlite"
                revisions.append(normalized)

    groups: dict[str, list[dict[str, Any]]] = {}
    for revision in revisions:
        groups.setdefault(revision["canonical_policy_key"], []).append(revision)

    policy_view: dict[str, dict[str, Any]] = {}
    group_views: dict[str, Any] = {}
    for key, items in sorted(groups.items()):
        ordered = sorted(
            items,
            key=lambda item: (
                item["generation"],
                item["effective_at_ms"],
                item["source_priority"],
                item["source"],
            ),
        )
        winner = ordered[-1]
        same_authority_tie = (
            len(ordered) >= 2
            and ordered[-1]["generation"] == ordered[-2]["generation"]
            and ordered[-1]["effective_at_ms"] == ordered[-2]["effective_at_ms"]
            and ordered[-1]["source_category"] != ordered[-2]["source_category"]
        )
        conflict = _detect_conflict(key, ordered)
        if conflict:
            conflict["authoritative_source"] = winner["source"]
            conflict["authority_resolved"] = not same_authority_tie
            conflicts.append(conflict)
        active = None if winner["tombstoned"] else winner
        if active:
            policy_view[key] = active
        group_views[key] = {
            "canonical_policy_key": key,
            "authoritative": winner,
            "active_policy": active,
            "revisions": ordered,
            "authority_resolved": not same_authority_tie,
            "revision_count": len(ordered),
            "conflict": conflict,
        }

    binding_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for binding in bindings:
        binding_groups.setdefault(
            (binding["canonical_policy_key"], binding["user_id"], binding["device_id"]), []
        ).append(binding)

    return {
        "observation_clock_ms": bundle.observation_clock_ms,
        "policies": policy_view,
        "policy_groups": group_views,
        "bindings": bindings,
        "binding_groups": {
            "|".join(key): sorted(items, key=lambda b: (b["generation"], b["effective_at_ms"], b["source"]))
            for key, items in binding_groups.items()
        },
        "conflicts": conflicts,
    }


def _conclusion(revision: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": revision["source"],
        "record_seq": revision["record_seq"],
        "generation": revision["generation"],
        "effective_at_ms": revision["effective_at_ms"],
        "required_factors": revision["required_factors"],
        "binding_requirement": revision["binding_requirement"],
        "expires_at_ms": revision["expires_at_ms"],
        "tombstoned": revision["tombstoned"],
    }


def _detect_conflict(key: str, revisions: list[dict[str, Any]]) -> dict[str, Any] | None:
    v1 = [r for r in revisions if r["source_category"] == "policy_v1" and not r["tombstoned"]]
    v2 = [r for r in revisions if r["source_category"].startswith("policy_v2") and not r["tombstoned"]]
    if not v1 or not v2:
        return None
    left = max(v1, key=lambda r: (r["generation"], r["effective_at_ms"]))
    right = max(v2, key=lambda r: (r["generation"], r["effective_at_ms"]))
    different_fields: list[str] = []
    if left["required_factors"] != right["required_factors"]:
        different_fields.append("required_factors")
    if left["binding_requirement"] != right["binding_requirement"]:
        different_fields.append("binding_requirement")
    if left["expires_at_ms"] != right["expires_at_ms"]:
        different_fields.append("expires_at_ms")
    if not different_fields:
        return None
    return {
        "code": E_POLICY_CONFLICT,
        "source_category": "policy_merge",
        "record_seq": 0,
        "policy_key": key,
        "canonical_policy_key": key,
        "fields": different_fields,
        "message": "v1 and v2 policy conclusions disagree",
        "left": _conclusion(left),
        "right": _conclusion(right),
    }


def select_policy_at(
    view: dict[str, Any], canonical_key: str, at_ms: int
) -> tuple[dict[str, Any] | None, str | None]:
    group = view.get("policy_groups", {}).get(canonical_key)
    if not group:
        return None, "missing"
    revisions = group.get("revisions", [])
    effective = [r for r in revisions if r["effective_at_ms"] <= at_ms]
    if not effective:
        return None, "not_effective"
    selected = max(effective, key=lambda r: (r["generation"], r["effective_at_ms"], r["source_priority"]))
    if selected.get("tombstoned"):
        return selected, "tombstoned"
    if selected["expires_at_ms"] is not None and at_ms >= selected["expires_at_ms"]:
        return selected, "expired"
    return selected, None


def select_binding_at(
    view: dict[str, Any], canonical_policy_key: str, user_id: str, device_id: str, at_ms: int
) -> dict[str, Any] | None:
    group_key = f"{canonical_policy_key}|{user_id}|{device_id}"
    items = view.get("binding_groups", {}).get(group_key, [])
    effective = [item for item in items if item["effective_at_ms"] <= at_ms]
    if not effective:
        return None
    return max(effective, key=lambda item: (item["generation"], item["effective_at_ms"], item["source"]))


def select_device_binding_at(
    view: dict[str, Any], canonical_policy_key: str, user_id: str, device_id: str, at_ms: int
) -> dict[str, Any] | None:
    """Select a policy-specific binding, falling back to a device-wide binding."""

    exact = select_binding_at(view, canonical_policy_key, user_id, device_id, at_ms)
    if exact is not None:
        return exact
    suffix = f"|{user_id}|{device_id}"
    candidates: list[dict[str, Any]] = []
    for group_key, items in view.get("binding_groups", {}).items():
        if not group_key.endswith(suffix):
            continue
        candidates.extend(item for item in items if item["effective_at_ms"] <= at_ms)
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item["generation"], item["effective_at_ms"], item["source"]))
