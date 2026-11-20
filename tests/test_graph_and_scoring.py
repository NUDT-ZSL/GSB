from __future__ import annotations

import pytest

from dialog_emotion.errors import BusinessError
from dialog_emotion.pipeline import IncrementalPipeline
from dialog_emotion.scoring import (
    aggregate_dialog,
    recompute_aggregate,
    validate_interval,
)
from dialog_emotion.segments import build_segments, validate_segments
from dialog_emotion.speaker import validate_assignment_labels


def _engine(bundle, workdir):
    return IncrementalPipeline(
        bundle,
        cache_dir=workdir / "cache",
        config_snapshot_dir=workdir / "config",
        lineage_dir=workdir / "lineage",
    )


def test_public_top_level_api_imports() -> None:
    from dialog_emotion import (
        AppendRegistration,
        CorrectTruth,
        IncrementalPipeline as PublicPipeline,
        SetFrameShift,
        SetSegmentThreshold,
        compare_runs,
    )

    assert PublicPipeline is IncrementalPipeline
    assert AppendRegistration("a", 1, 10).speaker_id == "a"
    assert CorrectTruth("d", 0.5, 0.5).dialog_id == "d"
    assert SetFrameShift(15.0).frame_shift_ms == 15.0
    assert SetSegmentThreshold(300.0).segment_threshold_ms == 300.0
    assert callable(compare_runs)


def test_assignment_and_segment_contracts(bundle, workdir) -> None:
    result = _engine(bundle, workdir).run()
    for dialog_id, dialog in bundle.dialogs.items():
        assignment = result.assignments[dialog_id]
        assert set(assignment["labels"]) <= set(dialog.speaker_ids)
        assert len(assignment["labels"]) == assignment["frame_count"]
        segments = result.boundaries[dialog_id]["segments"]
        validate_segments(
            dialog_id, segments, result.features[dialog_id]["frame_count"]
        )
        assert segments[0]["start_frame"] == 0
        assert segments[-1]["end_frame"] == result.features[dialog_id]["frame_count"]


def test_unknown_speaker_and_segment_gap_codes(bundle) -> None:
    dialog = bundle.dialogs["dlg-001"]
    with pytest.raises(BusinessError) as unknown:
        validate_assignment_labels(
            dialog.dialog_id, ["unknown"], dialog.speaker_ids
        )
    assert unknown.value.code == "E_SPEAKER_UNKNOWN"
    with pytest.raises(BusinessError) as gap:
        validate_segments(
            dialog.dialog_id,
            [
                {"segment_index": 0, "start_frame": 0, "end_frame": 2},
                {"segment_index": 1, "start_frame": 3, "end_frame": 4},
            ],
            4,
        )
    assert gap.value.code == "E_SEGMENT_GAP"


def test_segment_build_merges_short_runs() -> None:
    labels = ["a"] * 20 + ["b"] * 2 + ["a"] * 20
    result = build_segments("d", labels, threshold_ms=50, hop_ms=10, frame_count=42)
    assert len(result["segments"]) == 1


def test_interval_validation_code() -> None:
    with pytest.raises(BusinessError) as captured:
        validate_interval(0.8, 0.2, dialog_id="d", segment_index=3)
    assert captured.value.code == "E_INTERVAL_INVALID"


def test_regression_intervals_are_ordered_and_aggregate_recomputes(
    bundle, workdir
) -> None:
    result = _engine(bundle, workdir).run()
    for scores in result.segment_scores.values():
        for score in scores:
            assert score["valence_ci"]["low"] <= score["valence"]
            assert score["valence"] <= score["valence_ci"]["high"]
            assert score["arousal_ci"]["low"] <= score["arousal"]
            assert score["arousal"] <= score["arousal_ci"]["high"]
    for dialog_id, aggregate in result.aggregates.items():
        recomputed = recompute_aggregate(aggregate)
        assert recomputed["valence"] == aggregate["valence"]
        assert recomputed["arousal"] == aggregate["arousal"]
        assert aggregate["weight_basis"] == "segment_duration_ms"
        assert aggregate["total_weight"] == sum(
            item["weight"] for item in aggregate["contributions"]
        )
        assert aggregate["independent_recompute"]["matches"] is True


def test_aggregation_rejects_nonpositive_weight(bundle) -> None:
    with pytest.raises(BusinessError) as captured:
        aggregate_dialog(
            "d",
            [
                {
                    "segment_index": 0,
                    "duration_ms": 0,
                    "valence": 0.5,
                    "arousal": 0.5,
                }
            ],
            "segment_duration_ms",
        )
    assert captured.value.code == "E_RESOURCE_LIMIT"
