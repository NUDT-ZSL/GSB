from __future__ import annotations

from typing import Any

from .canonical import canonical_dumps, sha256_hex
from .clock import ClockProfile, decide_snapshot_candidate, v1_effective_time
from .validation import FixtureBundle


def build_unified_view(bundle: FixtureBundle) -> dict[str, Any]:
    profile: ClockProfile = bundle.clock_profile
    v1_document = bundle.v1
    snapshot_conversion = v1_effective_time(
        profile,
        int(v1_document["declared_clock"]),
        v1_document["effective_relative_seconds"],
    )

    decisions: dict[str, Any] = {}
    for key in sorted(set(bundle.v1_records) | set(bundle.v2_records)):
        v1_record = bundle.v1_records.get(key)
        v2_record = bundle.v2_records.get(key)
        v1_candidate = None
        if v1_record is not None:
            v1_candidate = {
                "source": "v1",
                "object_key": key,
                "content_version": v1_record["content_version"],
                "signature": v1_record["signature"],
                "cache_generation": v1_record["cache_generation"],
                "logical_clock": v1_record["logical_clock"],
                "decision_clock_ms": profile.clock_to_ms(v1_record["logical_clock"]),
                "tombstone": False,
                "trace": v1_record["trace"],
            }
        v2_candidate = None
        if v2_record is not None:
            logical_clock = v2_record.get("logical_clock")
            v2_candidate = {
                "source": "v2",
                "object_key": key,
                "content_version": v2_record["content_version"],
                "signature": v2_record["signature"],
                "cache_generation": int(v2_record["generation"]),
                "logical_clock": logical_clock,
                "decision_clock_ms": int(v2_record["absolute_clock_ms"]),
                "tombstone": bool(v2_record.get("tombstone", False)),
                "shard_id": v2_record.get("shard_id"),
            }
        decision = decide_snapshot_candidate(v1_candidate, v2_candidate)
        decisions[key] = decision

    view = {
        "schema": "unified-snapshot-view-v1",
        "clock_profile": {
            "anchor_ms": profile.anchor_ms,
            "tick_ms": profile.tick_ms,
            "conversion": f"absolute_ms={profile.anchor_ms}+logical_clock*{profile.tick_ms}",
        },
        "v1_snapshot_time_conversion": snapshot_conversion,
        "key_count": len(decisions),
        "decisions": decisions,
    }
    view["view_sha256"] = sha256_hex(canonical_dumps({k: v for k, v in view.items() if k != "view_sha256"}).encode())
    return view


def deterministic_view_fingerprint(bundle: FixtureBundle) -> dict[str, Any]:
    first = build_unified_view(bundle)
    second = build_unified_view(bundle)
    first_blob = canonical_dumps(first)
    second_blob = canonical_dumps(second)
    return {
        "identical_on_repeat": first_blob == second_blob,
        "view_sha256": first["view_sha256"],
        "byte_length": len(first_blob.encode("utf-8")),
    }
