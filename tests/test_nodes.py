from __future__ import annotations

from dataclasses import replace

import pytest

from meeting_vad.errors import BusinessError
from meeting_vad.models import Annotation
from meeting_vad.nodes.envelope import compute_envelope
from meeting_vad.nodes.export import compute_export
from meeting_vad.nodes.refine import _assert_no_swallow, compute_refine
from meeting_vad.nodes.speaker import compute_speaker, _score_labels
from meeting_vad.nodes.vad import compute_vad, validate_interval_bounds


def _pipeline(app_config, session):
    envelope = compute_envelope(session.audio_by_clip, app_config)
    vad = compute_vad(envelope, app_config)
    refine = compute_refine(envelope, vad, app_config)
    return envelope, vad, refine


def test_envelope_is_deterministic_and_exactly_framed(app_config, session) -> None:
    first = compute_envelope(session.audio_by_clip, app_config)
    second = compute_envelope(session.audio_by_clip, app_config)
    assert first == second
    for clip in session.clips_list:
        payload = first["clips"][clip.clip_id]
        hop = int(round(app_config.sample_rate * app_config.frame_hop_ms / 1000))
        expected = (clip.expected_samples + hop - 1) // hop
        assert payload["frame_count"] == expected
        assert payload["expected_frame_count"] == expected
        assert payload["frame_start_samples"][0] == 0
        assert payload["frame_end_samples"][-1] == clip.expected_samples
        for index in range(1, len(payload["frame_start_samples"])):
            assert payload["frame_start_samples"][index] <= payload["frame_end_samples"][index - 1]


def test_envelope_frame_mismatch_reports_clip(app_config, session) -> None:
    clip_id = session.clips_list[0].clip_id
    info = session.audio_by_clip[clip_id]
    broken_clip = replace(
        info.clip,
        duration_ms=info.clip.duration_ms + 100,
        expected_samples=info.clip.expected_samples + 1600,
    )
    broken_info = replace(info, clip=broken_clip)
    broken_audio = dict(session.audio_by_clip)
    broken_audio[clip_id] = broken_info
    with pytest.raises(BusinessError) as exc:
        compute_envelope(broken_audio, app_config)
    assert exc.value.code == "E_FRAME_MISMATCH"
    assert exc.value.details["clip_id"] == clip_id
    assert "expected_frame_count" in exc.value.details


def test_envelope_resource_limit(app_config, session) -> None:
    config = replace(
        app_config,
        resources=replace(app_config.resources, max_envelope_frames=1),
    )
    with pytest.raises(BusinessError) as exc:
        compute_envelope(session.audio_by_clip, config)
    assert exc.value.code == "E_RESOURCE_LIMIT"


def test_vad_intervals_are_ordered_in_range_and_non_overlapping(app_config, session) -> None:
    _, vad, refine = _pipeline(app_config, session)
    for clip in session.clips_list:
        intervals = vad["clips"][clip.clip_id]["intervals"]
        starts = [interval["start_sample"] for interval in intervals]
        assert starts == sorted(starts)
        validate_interval_bounds(intervals, clip.duration_ms, "candidate_id")
        refined = refine["clips"][clip.clip_id]["intervals"]
        validate_interval_bounds(refined, clip.duration_ms, "candidate_id")


def test_interval_validation_business_codes() -> None:
    with pytest.raises(BusinessError) as overlap:
        validate_interval_bounds(
            [
                {"candidate_id": "a", "start_ms": 0, "end_ms": 10},
                {"candidate_id": "b", "start_ms": 9, "end_ms": 20},
            ],
            20,
            "candidate_id",
        )
    assert overlap.value.code == "E_OVERLAP"
    assert overlap.value.details["interval_id"] == "b"
    with pytest.raises(BusinessError) as out_of_range:
        validate_interval_bounds(
            [{"candidate_id": "c", "start_ms": 19, "end_ms": 21}],
            20,
            "candidate_id",
        )
    assert out_of_range.value.code == "E_OUT_OF_RANGE"
    assert out_of_range.value.details["interval_id"] == "c"


def test_refinement_stays_within_window(app_config, session) -> None:
    _, _, refine = _pipeline(app_config, session)
    for clip in session.clips_list:
        for interval in refine["clips"][clip.clip_id]["intervals"]:
            assert abs(interval["start_delta_ms"]) <= app_config.refine.window_ms + 1e-9
            assert abs(interval["end_delta_ms"]) <= app_config.refine.window_ms + 1e-9


def test_refinement_swallow_is_rejected() -> None:
    candidates = [
        {"candidate_id": "a", "start_sample": 0, "end_sample": 100},
        {"candidate_id": "b", "start_sample": 120, "end_sample": 180},
    ]
    with pytest.raises(BusinessError) as exc:
        _assert_no_swallow("a", 0, 190, 0, 100, candidates)
    assert exc.value.code == "E_REFINE_INVALID"
    assert exc.value.details["affected_interval_id"] == "b"
    assert "allowed_start_min" in exc.value.details


def test_speaker_unknown_label(app_config, session) -> None:
    envelope, _, refine = _pipeline(app_config, session)
    annotations = [
        replace(session.annotations[0], speaker_label="UNKNOWN")
    ]
    with pytest.raises(BusinessError) as exc:
        compute_speaker(envelope, refine, annotations, app_config)
    assert exc.value.code == "E_SPEAKER_UNKNOWN"


def test_speaker_invalid_confidence_interval(app_config, session) -> None:
    envelope, _, refine = _pipeline(app_config, session)
    annotations = [
        replace(
            session.annotations[0],
            confidence_lower=0.9,
            confidence_upper=0.1,
        )
    ]
    with pytest.raises(BusinessError) as exc:
        compute_speaker(envelope, refine, annotations, app_config)
    assert exc.value.code == "E_INTERVAL_INVALID"


def test_speaker_tie_break_lexicographic(app_config) -> None:
    config = replace(
        app_config,
        speaker=replace(
            app_config.speaker,
            labels=("S02", "S01"),
            templates={
                "S01": {"energy": 0.03, "zcr": 0.04},
                "S02": {"energy": 0.03, "zcr": 0.04},
            },
        ),
    )
    scores = _score_labels(0.03, 0.04, None, "seg-1", config)
    assert scores[0][1] == "S01"


def test_export_invalid_interval_code(app_config, session) -> None:
    envelope, _, refine = _pipeline(app_config, session)
    speaker = compute_speaker(envelope, refine, session.annotations, app_config)
    record = speaker["clips"][speaker["clip_order"][0]]["intervals"][0]
    record["confidence_lower"] = 0.9
    record["confidence_upper"] = 0.1
    with pytest.raises(BusinessError) as exc:
        compute_export(speaker, session.clips, app_config)
    assert exc.value.code == "E_INTERVAL_INVALID"
