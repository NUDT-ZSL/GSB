"""Lineage recording, replay and stale-result rejection."""

from __future__ import annotations

from .align import resolve_segment
from .errors import LineageBrokenError

LINEAGE_DEPENDENCIES = (
    "map_cache_key",
    "calibration_version",
    "calibration_hash",
    "seam_set_version",
    "seam_set_hash",
    "crossed_seams_hash",
    "frame_hop",
    "config_version",
    "pcm_hash",
)

# Dependencies that actually compose the segment's cache key; a stale value
# of any of these invalidates the recorded result.  seam_set_version and
# seam_set_hash are recorded for replay context, but a segment only depends
# on the seams it crosses (targeted invalidation requirement).
VERIFIED_DEPENDENCIES = (
    "calibration_version",
    "calibration_hash",
    "crossed_seams_hash",
    "frame_hop",
    "config_version",
    "pcm_hash",
)


def attach_lineage(mapped_segment, *, map_cache_key, calibration_version,
                   calibration_hash, seam_set_version, seam_set_hash,
                   crossed_seams_hash, frame_hop, config_version, pcm_hash):
    mapped_segment["lineage"] = {
        "map_cache_key": map_cache_key,
        "calibration_version": calibration_version,
        "calibration_hash": calibration_hash,
        "seam_set_version": seam_set_version,
        "seam_set_hash": seam_set_hash,
        "crossed_seams_hash": crossed_seams_hash,
        "frame_hop": frame_hop,
        "config_version": config_version,
        "pcm_hash": pcm_hash,
    }
    return mapped_segment


def current_dependencies(global_segment, lineage_state):
    """Resolve the current dependency values for a segment from the state."""
    batch_id = global_segment["batch_id"]
    return {
        "calibration_version": lineage_state["calibration_versions"][batch_id],
        "calibration_hash": lineage_state["calibration_hashes"][batch_id],
        "crossed_seams_hash": lineage_state["crossed_seams"].get(
            global_segment["id"]
        ),
        "frame_hop": lineage_state["frame_hop"],
        "config_version": lineage_state["config_version"],
        "pcm_hash": lineage_state["pcm_hashes"][batch_id],
    }


def verify_lineage(global_segment, lineage_state):
    """Reject stale results: recorded dependencies must match the present.

    Missing lineage or any mismatch raises E_LINEAGE_BROKEN naming the
    segment and the invalidated dependency.
    """
    lineage = global_segment.get("lineage")
    seg_id = global_segment.get("id", "<unknown>")
    if not lineage:
        raise LineageBrokenError(
            f"segment {seg_id} has no lineage record",
            segment_id=seg_id,
            dependency="lineage_record",
        )
    for dep in LINEAGE_DEPENDENCIES:
        if dep not in lineage:
            raise LineageBrokenError(
                f"segment {seg_id} lineage misses {dep}",
                segment_id=seg_id,
                dependency=dep,
            )
    current = current_dependencies(global_segment, lineage_state)
    for dep in VERIFIED_DEPENDENCIES:
        if lineage[dep] != current[dep]:
            raise LineageBrokenError(
                f"segment {seg_id} depends on stale {dep}",
                segment_id=seg_id,
                dependency=dep,
                recorded=lineage[dep],
                current=current[dep],
            )
    return True


def replay_segment(global_segment, original_segments, calibrations,
                   seams_global_ms, gaps_global_ms, config):
    """Re-derive start/end and word offsets from recorded parameters.

    Raises E_LINEAGE_BROKEN when the replay cannot reproduce the recorded
    timeline.
    """
    seg_id = global_segment["id"]
    source = next((s for s in original_segments if s["id"] == seg_id), None)
    if source is None:
        raise LineageBrokenError(
            f"segment {seg_id} source transcript missing",
            segment_id=seg_id,
            dependency="source_segment",
        )
    calibration = calibrations[source["batch_id"]]
    recomputed = resolve_segment(
        source, calibration, seams_global_ms, gaps_global_ms, config
    )
    for field in ("start_ms", "end_ms", "handling", "shift_ms"):
        if recomputed[field] != global_segment[field]:
            raise LineageBrokenError(
                f"segment {seg_id} replay mismatch on {field}",
                segment_id=seg_id,
                dependency="replay",
                field=field,
                recorded=global_segment[field],
                recomputed=recomputed[field],
            )
    recorded_offsets = [w["offset_ms"] for w in global_segment["words"]]
    replay_offsets = [w["offset_ms"] for w in recomputed["words"]]
    if recorded_offsets != replay_offsets:
        raise LineageBrokenError(
            f"segment {seg_id} replay word offsets differ",
            segment_id=seg_id,
            dependency="replay_word_offsets",
        )
    return recomputed
