"""Attribution of missing observations and abrupt motion on the temporal chain."""

from __future__ import annotations

import math
from typing import Any, Iterable

from .canonical import canonical_bytes, r6, sha256_bytes
from .codes import ATTRIBUTION_CATEGORIES, E_ATTRIBUTION_FAIL
from .errors import BusinessError
from .models import ClipSpec


def diagnose_attribution(
    spec: ClipSpec,
    aggregation_payload: dict[str, Any],
    encoding_payload: dict[str, Any],
    *,
    acceleration_threshold: float = 800.0,
    categories: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return located, causally typed records without mutating prediction output."""

    selected = list(categories) if categories is not None else list(ATTRIBUTION_CATEGORIES)
    unknown = [category for category in selected if category not in ATTRIBUTION_CATEGORIES]
    if unknown:
        raise BusinessError(
            E_ATTRIBUTION_FAIL,
            f"uncontrolled attribution categories: {unknown}",
            spec.clip_id,
            None,
            {"allowed": list(ATTRIBUTION_CATEGORIES), "unknown": unknown, "located": []},
            located_frames=[],
        )
    aggregate_frames = {int(frame["frame_index"]): frame for frame in aggregation_payload.get("frames", [])}
    encoded_frames = {int(frame["frame_index"]): frame for frame in encoding_payload.get("frames", [])}
    expected = set(range(spec.frame_count))
    missing_locations = sorted(expected - set(aggregate_frames))
    if missing_locations:
        raise BusinessError(
            E_ATTRIBUTION_FAIL,
            "aggregation output cannot locate every frame",
            spec.clip_id,
            missing_locations[0],
            {"missing_frames": missing_locations, "located": sorted(aggregate_frames)},
            located_frames=sorted(aggregate_frames),
        )

    records: list[dict[str, Any]] = []
    if "observation_missing" in selected:
        for frame_index in sorted(aggregate_frames):
            aggregate = aggregate_frames[frame_index]
            encoded = encoded_frames.get(frame_index, {})
            affected: list[int] = []
            kinds: set[str] = set()
            ages: dict[str, int] = {}
            statuses = {int(item["object_id"]): str(item["status"]) for item in encoded.get("objects", [])}
            if not aggregate.get("observed", True):
                kinds.add("frame_missing")
                affected = list(range(spec.object_count))
            else:
                for object_id in range(spec.object_count):
                    status = statuses.get(object_id, "missing")
                    if status in {"missing", "occluded"}:
                        affected.append(object_id)
                        kinds.add("target_occluded" if status == "occluded" else "target_missing")
            if affected:
                for item in aggregate.get("objects", []):
                    if int(item["object_id"]) in affected:
                        ages[str(item["object_id"])] = int(item.get("extrapolation_age", 0))
                records.append(
                    {
                        "clip_id": spec.clip_id,
                        "frame_index": frame_index,
                        "category": "observation_missing",
                        "observation_kind": sorted(kinds)[0],
                        "affected_targets": sorted(set(affected)),
                        "history_window": aggregate["history_window"],
                        "state_hash": aggregate["state_hash"],
                        "deviation": {
                            "kind": "extrapolation_age_frames",
                            "value": max(ages.values()) if ages else 0,
                            "per_target": ages,
                        },
                        "located": True,
                    }
                )

    if "motion_abrupt" in selected:
        for frame_index in sorted(encoded_frames):
            encoded = encoded_frames[frame_index]
            affected: list[int] = []
            magnitudes: dict[str, float] = {}
            for item in encoded.get("objects", []):
                acceleration = item.get("acceleration")
                if acceleration is None:
                    continue
                magnitude = math.hypot(float(acceleration[0]), float(acceleration[1]))
                if magnitude > acceleration_threshold:
                    affected.append(int(item["object_id"]))
                    magnitudes[str(item["object_id"])] = r6(magnitude)
            if affected:
                aggregate = aggregate_frames[frame_index]
                records.append(
                    {
                        "clip_id": spec.clip_id,
                        "frame_index": frame_index,
                        "category": "motion_abrupt",
                        "observation_kind": "acceleration_threshold_exceeded",
                        "affected_targets": affected,
                        "history_window": aggregate["history_window"],
                        "state_hash": aggregate["state_hash"],
                        "deviation": {
                            "kind": "acceleration_px_per_s2",
                            "value": max(magnitudes.values()),
                            "threshold": r6(acceleration_threshold),
                            "per_target": magnitudes,
                        },
                        "located": True,
                    }
                )
    records.sort(key=lambda item: (item["frame_index"], item["category"], item["affected_targets"]))
    payload = {
        "clip_id": spec.clip_id,
        "records": records,
        "located_frames": [record["frame_index"] for record in records],
        "categories": selected,
    }
    payload["content_checksum"] = sha256_bytes(canonical_bytes(payload))
    return payload

