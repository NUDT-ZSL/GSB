"""Requirement 3: segment alignment, conflict adjudication, validation."""

import pytest

from meeting_align.align import (map_segment, resolve_segment,
                                 validate_global_segments)
from meeting_align.errors import OutOfRangeError, OverlapError


def _segment(seg_id="S1", start=1000.0, end=2000.0, batch="A"):
    return {
        "id": seg_id,
        "batch_id": batch,
        "speaker": "spk0",
        "start_ms": start,
        "end_ms": end,
        "words": [
            {"text": "w0", "offset_ms": 0.0, "confidence": 0.9},
            {"text": "w1", "offset_ms": 500.0, "confidence": 0.8},
            {"text": "w2", "offset_ms": 900.0, "confidence": 0.7},
        ],
    }


CAL = {"scale": 1.0, "offset_ms": 100.0, "residual_rms_ms": 1.5}


def test_map_segment_applies_scale_and_offset():
    seg = _segment()
    assert map_segment(seg, CAL) == (1100.0, 2100.0)
    assert map_segment(seg, {**CAL, "scale": 0.5}) == (600.0, 1100.0)


def test_truncate_strategy_cuts_at_seam(config):
    config.conflict_strategy = "truncate"
    seg = _segment()  # global 1100..2100
    mapped = resolve_segment(seg, CAL, [1500.0], [], config)
    assert mapped["handling"] == "truncated"
    assert mapped["end_ms"] == 1500.0
    assert [w["text"] for w in mapped["words"]] == ["w0"]  # w1@1600, w2@1900 cut
    assert mapped["dropped_words"] == ["w1", "w2"]


def test_truncate_strategy_cuts_at_gap(config):
    config.conflict_strategy = "truncate"
    seg = _segment()
    mapped = resolve_segment(seg, CAL, [], [[1300.0, 1700.0]], config)
    assert mapped["handling"] == "truncated"
    assert mapped["end_ms"] == 1300.0


def test_shift_strategy_moves_whole_segment(config):
    config.conflict_strategy = "shift"
    seg = _segment()
    mapped = resolve_segment(seg, CAL, [], [[1300.0, 1700.0]], config)
    assert mapped["handling"] == "shifted"
    assert mapped["start_ms"] == 1700.0
    assert mapped["end_ms"] == 2700.0
    # word offsets inside the segment are preserved (whole-segment shift)
    assert [w["offset_ms"] for w in mapped["words"]] == [0.0, 500.0, 900.0]
    assert mapped["shift_ms"] == 600.0


def test_no_conflict_marks_none(config):
    mapped = resolve_segment(_segment(), CAL, [5000.0], [[3000.0, 4000.0]], config)
    assert mapped["handling"] == "none"
    assert mapped["start_ms"] == 1100.0 and mapped["end_ms"] == 2100.0


def test_overlap_rejected_with_segment_id():
    segs = [
        {"id": "S1", "start_ms": 100.0, "end_ms": 500.0},
        {"id": "S2", "start_ms": 400.0, "end_ms": 900.0},
    ]
    with pytest.raises(OverlapError) as exc:
        validate_global_segments(segs, 10000.0)
    assert exc.value.code == "E_OVERLAP"
    assert exc.value.context["segment_id"] == "S2"


def test_out_of_range_rejected_with_segment_id():
    segs = [{"id": "S9", "start_ms": 900.0, "end_ms": 1500.0}]
    with pytest.raises(OutOfRangeError) as exc:
        validate_global_segments(segs, 1000.0)
    assert exc.value.code == "E_OUT_OF_RANGE"
    assert exc.value.context["segment_id"] == "S9"


def test_output_sorted_by_global_time():
    segs = [
        {"id": "B", "start_ms": 500.0, "end_ms": 600.0},
        {"id": "A", "start_ms": 100.0, "end_ms": 200.0},
    ]
    ordered = validate_global_segments(segs, 10000.0)
    assert [s["id"] for s in ordered] == ["A", "B"]
