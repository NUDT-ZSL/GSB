"""Cross-stage consistency checks for frame sets and resume accounting."""

from __future__ import annotations

from typing import Any, Iterable

from .canonical import canonical_bytes, sha256_bytes


def _frame_set(stage: str, payload: dict[str, Any]) -> set[int]:
    if stage == "sampling":
        return {int(item["frame_index"]) for item in payload.get("timeline", [])}
    return {int(item["frame_index"]) for item in payload.get("frames", [])}


def validate_pipeline(
    stage_payloads: dict[str, dict[str, Any]],
    *,
    clip_id: str,
    expected_frames: Iterable[int],
    affected_frames: Iterable[int] | None = None,
    reused_frames: Iterable[int] = (),
    recomputed_frames: Iterable[int] = (),
) -> dict[str, Any]:
    """Check clip/frame set alignment and resume-set partitioning."""

    expected = set(int(index) for index in expected_frames)
    affected = set(expected if affected_frames is None else affected_frames)
    reused = set(int(index) for index in reused_frames)
    recomputed = set(int(index) for index in recomputed_frames)
    issues: list[dict[str, Any]] = []
    frame_sets: dict[str, list[int]] = {}
    categories: set[str] = set()

    for stage, payload in stage_payloads.items():
        payload_clip = payload.get("clip_id")
        if payload_clip != clip_id:
            categories.add("clip_id_mismatch")
            issues.append({"category": "clip_id_mismatch", "stage": stage, "expected": clip_id, "actual": payload_clip})
        frames = _frame_set(stage, payload)
        frame_sets[stage] = sorted(frames)
        if frames != expected:
            categories.add("frame_set_mismatch")
            issues.append(
                {
                    "category": "frame_set_mismatch",
                    "stage": stage,
                    "missing": sorted(expected - frames),
                    "unexpected": sorted(frames - expected),
                }
            )

    overlap = reused & recomputed
    if overlap:
        categories.add("reuse_recompute_overlap")
        issues.append({"category": "reuse_recompute_overlap", "frames": sorted(overlap)})
    coverage = reused | recomputed
    if not affected.issubset(coverage):
        categories.add("affected_frames_uncovered")
        issues.append(
            {
                "category": "affected_frames_uncovered",
                "frames": sorted(affected - coverage),
            }
        )
    if not recomputed.issubset(expected) or not reused.issubset(expected):
        categories.add("reuse_recompute_out_of_range")
        issues.append(
            {
                "category": "reuse_recompute_out_of_range",
                "reused_outside": sorted(reused - expected),
                "recomputed_outside": sorted(recomputed - expected),
            }
        )
    result = {
        "ok": not issues,
        "clip_id": clip_id,
        "categories": sorted(categories),
        "issues": issues,
        "frame_sets": frame_sets,
        "reused_frames": sorted(reused),
        "recomputed_frames": sorted(recomputed),
        "affected_frames": sorted(affected),
        "checksum": sha256_bytes(canonical_bytes(issues)),
    }
    return result

