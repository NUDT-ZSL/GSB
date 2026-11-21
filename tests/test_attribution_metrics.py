from __future__ import annotations

from dataclasses import replace

import pytest

from motion_temporal.aggregation import aggregate_temporal
from motion_temporal.attribution import diagnose_attribution
from motion_temporal.codes import E_ATTRIBUTION_FAIL
from motion_temporal.encoding import encode_motion
from motion_temporal.errors import BusinessError
from motion_temporal.generator import generate_clip
from motion_temporal.metrics import evaluate_quality, recompute_metrics_from_details
from motion_temporal.sampling import sampling_payload


def _payloads(spec, clip):
    sampling = sampling_payload(spec, clip.frames)
    encoding = encode_motion(spec, clip.frames, sampling)
    aggregation = aggregate_temporal(spec, encoding, window_length=3, decay=0.85)
    return encoding, aggregation


def test_missing_observation_and_motion_jump_are_distinct(spec):
    clip = generate_clip(spec, missing_frames=[4])
    encoding, aggregation = _payloads(spec, clip)
    frames = list(clip.frames)
    target = frames[6].targets[0]
    frames[6] = replace(
        frames[6],
        targets=(
            replace(
                target,
                box=(target.box[0] + 35.0, target.box[1], target.box[2] + 35.0, target.box[3]),
                mask_coords=tuple((y, min(spec.canvas_width - 1, x + 35)) for y, x in target.mask_coords),
            ),
            *frames[6].targets[1:],
        ),
    )
    encoding = encode_motion(spec, frames, sampling_payload(spec, clip.frames))
    aggregation = aggregate_temporal(spec, encoding, window_length=3, decay=0.85)
    attribution = diagnose_attribution(spec, aggregation, encoding, acceleration_threshold=500.0)
    categories = {item["category"] for item in attribution["records"]}
    assert categories == {"observation_missing", "motion_abrupt"}
    missing = next(
        item
        for item in attribution["records"]
        if item["category"] == "observation_missing" and item["frame_index"] == 4
    )
    assert missing["frame_index"] == 4
    assert missing["history_window"]["digest"]
    abrupt = next(item for item in attribution["records"] if item["category"] == "motion_abrupt")
    assert abrupt["frame_index"] == 6
    assert 0 in abrupt["affected_targets"]
    assert abrupt["deviation"]["value"] > 500.0


def test_invalid_attribution_category_fails_but_keeps_locations(spec, clip):
    encoding, aggregation = _payloads(spec, clip)
    with pytest.raises(BusinessError) as caught:
        diagnose_attribution(spec, aggregation, encoding, categories=["unknown"])
    assert caught.value.code == E_ATTRIBUTION_FAIL
    assert caught.value.located_frames == []
    assert "located" in caught.value.details


def test_unlocatable_frame_returns_attribution_fail_with_located_list(spec, clip):
    encoding, aggregation = _payloads(spec, clip)
    broken = dict(aggregation)
    broken["frames"] = [frame for frame in aggregation["frames"] if frame["frame_index"] != 5]
    with pytest.raises(BusinessError) as caught:
        diagnose_attribution(spec, broken, encoding)
    assert caught.value.code == E_ATTRIBUTION_FAIL
    assert caught.value.frame_index == 5
    assert 4 in caught.value.located_frames
    assert 5 not in caught.value.located_frames


def test_metrics_recompute_from_details_exactly(spec, clip):
    encoding, aggregation = _payloads(spec, clip)
    from motion_temporal.output import build_output

    output = build_output(spec, aggregation, sampling_payload(spec, clip.frames))
    metrics = evaluate_quality(spec, output, clip.frames, hit_iou_threshold=0.3)
    recomputed = recompute_metrics_from_details(metrics["details"])
    for key, value in recomputed.items():
        assert metrics[key] == value
    assert metrics["sample_counts"]["object_frames"] == len(metrics["details"])

