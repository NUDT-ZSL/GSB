from __future__ import annotations

from meeting_audit.audio import extract_all_features
from meeting_audit.config import AuditConfig
from meeting_audit.detector import detect_turns
from meeting_audit.identity import (
    IdentityAdjudicator,
    build_speech_segments,
)
from meeting_audit.sample_generator import generate_sample
from meeting_audit.segmentation import (
    merge_segments,
    readjudicate_with_lineage,
    split_segment,
)


CONFIG = AuditConfig()


def _baseline():
    sample = generate_sample(CONFIG)
    features = extract_all_features(sample.pcm_int16, CONFIG)
    detection = detect_turns(features, CONFIG)
    segments = build_speech_segments(
        detection, sample.pcm_int16, features, CONFIG
    )
    adjudicator = IdentityAdjudicator(CONFIG)
    for segment in segments:
        adjudicator.assign(segment)
    return sample, features, segments, adjudicator


def test_split_and_merge_preserve_unaffected_labels() -> None:
    sample, features, segments, adjudicator = _baseline()
    original = {
        key: value["speaker_id"] for key, value in adjudicator.assignments.items()
    }
    split = split_segment(
        segments, "SEG-0002", 4800, sample.pcm_int16, features, CONFIG
    )
    result = readjudicate_with_lineage(split, adjudicator, CONFIG)
    assignments = result["assignments"]
    left = "SEG-0002__L4800"
    right = "SEG-0002__R4800"
    assert assignments[left]["speaker_id"] == original["SEG-0002"]
    assert assignments[right]["speaker_id"] == original["SEG-0002"]
    assert assignments[left]["status"] == "stable_inherited"
    assert all(
        original[segment_id] == assignments[segment_id]["speaker_id"]
        for segment_id in original
        if segment_id != "SEG-0002"
    )

    merged = merge_segments(
        split, [left, right], sample.pcm_int16, features, CONFIG
    )
    merged_result = readjudicate_with_lineage(merged, adjudicator, CONFIG)
    merged_id = next(
        segment["segment_id"]
        for segment in merged
        if segment["segment_id"] not in {item["segment_id"] for item in segments}
    )
    assert (
        merged_result["assignments"][merged_id]["speaker_id"]
        == original["SEG-0002"]
    )
    assert merged_result["recomputed"] == [merged_id]
