from __future__ import annotations

from typing import Any, Dict, List

from .model import Manifest
from .replay import ReplayResult, state_at_logic_ms


COMPARED_FIELDS = ("keyframe_seq", "play_position_ms", "generation")


def compare_merged_with_replay(
    manifest: Manifest,
    merged: Dict[str, Any],
    replay: ReplayResult,
) -> Dict[str, Any]:
    mismatches: List[Dict[str, Any]] = []
    compared_points = 0
    field_checks = 0
    skipped_tombstones = 0

    for point in merged.get("points", []):
        winner = point["winner"]
        canonical_id = point["canonical_screen_id"]
        logic_ms = point["logic_ms"]
        if winner.get("tombstone"):
            skipped_tombstones += 1
            continue

        pair = manifest.canonical_index.get(canonical_id)
        if pair is None:
            mismatches.append(
                {
                    "logic_ms": logic_ms,
                    "canonical_screen_id": canonical_id,
                    "field": "canonical_screen_id",
                    "expected": None,
                    "actual": canonical_id,
                    "message": "merged record references unknown canonical screen",
                    "source_locator": winner.get("source_locator"),
                    "shard_id": winner.get("shard_id"),
                }
            )
            continue

        _session, screen = pair
        expected_state = state_at_logic_ms(manifest, replay, screen.screen_id, logic_ms)
        compared_points += 1

        for field_name in COMPARED_FIELDS:
            field_checks += 1
            expected_value = expected_state.get(field_name)
            actual_value = winner.get(field_name)
            if expected_value != actual_value:
                mismatches.append(
                    {
                        "logic_ms": logic_ms,
                        "canonical_screen_id": canonical_id,
                        "screen_id": screen.screen_id,
                        "field": field_name,
                        "expected_replay_value": expected_value,
                        "actual_merged_value": actual_value,
                        "replay_state_clock_ms": expected_state.get("clock_ms"),
                        "source_locator": winner.get("source_locator"),
                        "shard_id": winner.get("shard_id"),
                        "message": (
                            f"first deviation at logic_ms={logic_ms} "
                            f"screen={canonical_id} field={field_name}"
                        ),
                    }
                )

    first_mismatch = mismatches[0] if mismatches else None
    return {
        "status": "E_REPLAY" if mismatches else "PASS",
        "error_code": "E_REPLAY" if mismatches else None,
        "first_mismatch": first_mismatch,
        "mismatches": mismatches,
        "stats": {
            "merged_points": len(merged.get("points", [])),
            "compared_points": compared_points,
            "field_checks": field_checks,
            "mismatch_count": len(mismatches),
            "skipped_tombstone_points": skipped_tombstones,
            "merge_conflict_count": len(merged.get("conflicts", [])),
            "digest_failure_count": len(merged.get("digest_failures", [])),
        },
    }
