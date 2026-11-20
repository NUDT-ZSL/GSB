from __future__ import annotations

import copy

import pytest

from voice_emotion import (
    AggregationConfig,
    BusinessError,
    FeatureConfig,
    PipelineConfig,
    ResourceLimits,
    SpeakerConfig,
    VoiceEmotionPipeline,
    load_dataset,
    validate_run_consistency,
)
from voice_emotion.dataset import (
    DialogRecord,
    EnrollmentRecord,
    build_demo_dataset,
    regenerate_truth,
    write_jsonl,
    write_truth,
)
from voice_emotion.audio import write_wav
from voice_emotion.synthesis import synthesize_dialog, synthesize_truth
from voice_emotion.errors import (
    E_INCONSISTENT,
    E_RESOURCE_LIMIT,
    E_SPEAKER_UNKNOWN,
)


def _rewrite_truth_for_segment(dataset, segment: dict, delta: float = 0.25) -> None:
    from voice_emotion.dataset import read_jsonl

    rows = read_jsonl(dataset.dialogs[0].truth_path)
    for frame in range(segment["start_frame"], segment["end_frame"]):
        rows[frame]["valence"] = round(min(1.0, float(rows[frame]["valence"]) + delta), 6)
    write_jsonl(dataset.dialogs[0].truth_path, rows)


def test_truth_correction_recomputes_only_affected_segment_and_dialog(
    demo_dataset,
    tmp_path,
) -> None:
    pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    initial = pipeline.analyze_dataset(demo_dataset)
    target_dialog = initial.data["dialogs"][0]
    target = target_dialog["segments"][1]
    _rewrite_truth_for_segment(demo_dataset, target)
    updated_dataset = load_dataset(
        demo_dataset.manifest_path,
        demo_dataset.enrollment_path,
    )
    updated = pipeline.analyze_dataset(updated_dataset, previous_run=initial)
    report = updated.data["report"]
    recomputed_for_dialog = {
        (item["node"], item["segment_id"])
        for item in report["recomputed_nodes"]
        if item["dialog_id"] == demo_dataset.dialogs[0].dialog_id
    }
    assert recomputed_for_dialog == {
        ("segment", target["segment_id"]),
        ("dialog", None),
    }
    reused_for_dialog = {
        item["node"] for item in report["reused_nodes"]
        if item["dialog_id"] == demo_dataset.dialogs[0].dialog_id
    }
    assert {"features", "assignment", "segment"} <= reused_for_dialog
    assert report["affected_frames"][demo_dataset.dialogs[0].dialog_id] == list(
        range(target["start_frame"], target["end_frame"])
    )
    assert report["affected_segments"][demo_dataset.dialogs[0].dialog_id] == [
        target["segment_id"]
    ]
    fresh = VoiceEmotionPipeline(tmp_path / "fresh").analyze_dataset(updated_dataset)
    assert updated.result_fingerprint == fresh.result_fingerprint
    assert pipeline.self_check(updated)["status"] == "ok"


def test_enrollment_append_reuses_features_only(demo_dataset, tmp_path) -> None:
    pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    initial = pipeline.analyze_dataset(demo_dataset)
    records = list(demo_dataset.enrollments)
    records.append(
        EnrollmentRecord(
            speaker_id=1,
            seed=20261121,
            duration_ms=650,
            sample_index=1,
        )
    )
    write_jsonl(
        demo_dataset.enrollment_path,
        [record.to_manifest(demo_dataset.root) for record in records],
    )
    updated_dataset = load_dataset(
        demo_dataset.manifest_path,
        demo_dataset.enrollment_path,
    )
    updated = pipeline.analyze_dataset(updated_dataset, previous_run=initial)
    nodes = {item["node"] for item in updated.data["report"]["recomputed_nodes"]}
    assert "features" not in nodes
    assert {"assignment", "segment", "dialog"} <= nodes
    assert updated.data["dialogs"][0]["feature"]["cache_key"] == initial.data["dialogs"][0][
        "feature"
    ]["cache_key"]
    fresh = VoiceEmotionPipeline(tmp_path / "fresh").analyze_dataset(updated_dataset)
    assert updated.result_fingerprint == fresh.result_fingerprint


def test_registration_change_is_scoped_to_dialogs_using_that_speaker(
    tmp_path,
) -> None:
    dataset = build_demo_dataset(
        tmp_path / "samples",
        seed=20261121,
        dialog_count=1,
        speaker_ids=(1, 2),
        duration_ms=1_200,
    )
    root = dataset.root
    speaker_three_audio, timeline = synthesize_dialog(
        "dlg_speaker_3",
        seed=20261121,
        duration_ms=1_200,
        speaker_ids=(3,),
    )
    audio_path = root / "audio" / "dlg_speaker_3.wav"
    truth_path = root / "truth" / "dlg_speaker_3.truth.jsonl"
    write_wav(audio_path, speaker_three_audio)
    write_jsonl(
        truth_path,
        synthesize_truth(timeline, seed=20261121, frame_ms=20, frame_shift_ms=20),
    )
    third_dialog = DialogRecord(
        dialog_id="dlg_speaker_3",
        sample_rate=16_000,
        duration_ms=1_200,
        speaker_ids=(3,),
        seed=20261121,
        audio_path=audio_path,
        truth_path=truth_path,
    )
    enrollments = list(dataset.enrollments) + [
        EnrollmentRecord(speaker_id=3, seed=20261121, duration_ms=900, sample_index=0)
    ]
    write_jsonl(
        dataset.enrollment_path,
        [record.to_manifest(root) for record in enrollments],
    )
    write_jsonl(
        dataset.manifest_path,
        [record.to_manifest(root) for record in (*dataset.dialogs, third_dialog)],
    )
    expanded = load_dataset(dataset.manifest_path, dataset.enrollment_path)
    pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    initial = pipeline.analyze_dataset(expanded)

    enrollments.append(
        EnrollmentRecord(
            speaker_id=1,
            seed=20261121,
            duration_ms=700,
            sample_index=1,
        )
    )
    write_jsonl(
        dataset.enrollment_path,
        [record.to_manifest(root) for record in enrollments],
    )
    updated_dataset = load_dataset(dataset.manifest_path, dataset.enrollment_path)
    updated = pipeline.analyze_dataset(updated_dataset, previous_run=initial)
    dlg1_nodes = {
        item["node"]
        for item in updated.data["report"]["recomputed_nodes"]
        if item["dialog_id"] == "dlg_0001"
    }
    dlg3_nodes = {
        item["node"]
        for item in updated.data["report"]["recomputed_nodes"]
        if item["dialog_id"] == "dlg_speaker_3"
    }
    assert {"assignment", "segment", "dialog"} <= dlg1_nodes
    assert "features" not in dlg1_nodes
    assert dlg3_nodes == set()
    fresh = VoiceEmotionPipeline(tmp_path / "fresh").analyze_dataset(updated_dataset)
    assert updated.result_fingerprint == fresh.result_fingerprint


def test_acoustic_parameter_change_invalidates_feature_prefix(
    demo_dataset,
    tmp_path,
) -> None:
    first_pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    initial = first_pipeline.analyze_dataset(demo_dataset)
    changed_config = PipelineConfig(feature=FeatureConfig(fft_size=1024))
    changed_pipeline = VoiceEmotionPipeline(tmp_path / "cache", changed_config)
    updated = changed_pipeline.analyze_dataset(demo_dataset, previous_run=initial)
    nodes = {item["node"] for item in updated.data["report"]["recomputed_nodes"]}
    assert {"features", "assignment", "segment", "dialog"} <= nodes
    assert updated.data["report"]["reused_nodes"] == []
    fresh = VoiceEmotionPipeline(tmp_path / "fresh", changed_config).analyze_dataset(
        demo_dataset
    )
    assert updated.result_fingerprint == fresh.result_fingerprint


def test_frame_shift_change_regenerates_truth_and_downstream(
    demo_dataset,
    tmp_path,
) -> None:
    first_pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    initial = first_pipeline.analyze_dataset(demo_dataset)
    shifted_counts = []
    for dialog in demo_dataset.dialogs:
        shifted_rows = regenerate_truth(dialog, frame_ms=20.0, frame_shift_ms=10.0)
        shifted_counts.append(len(shifted_rows))
        write_truth(dialog.truth_path, shifted_rows)
    updated_dataset = load_dataset(
        demo_dataset.manifest_path,
        demo_dataset.enrollment_path,
    )
    shifted_config = PipelineConfig(feature=FeatureConfig(frame_shift_ms=10.0))
    shifted_pipeline = VoiceEmotionPipeline(tmp_path / "cache", shifted_config)
    updated = shifted_pipeline.analyze_dataset(updated_dataset, previous_run=initial)
    nodes = {item["node"] for item in updated.data["report"]["recomputed_nodes"]}
    assert {"features", "assignment", "segment", "dialog"} <= nodes
    assert [
        item["feature"]["frame_count"] for item in updated.data["dialogs"]
    ] == shifted_counts
    fresh = VoiceEmotionPipeline(tmp_path / "fresh", shifted_config).analyze_dataset(
        updated_dataset
    )
    assert updated.result_fingerprint == fresh.result_fingerprint


def test_aggregation_only_change_recomputes_dialog(demo_dataset, tmp_path) -> None:
    first_pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    initial = first_pipeline.analyze_dataset(demo_dataset)
    config = PipelineConfig(
        aggregation=AggregationConfig(weight_basis="segment_frame_count")
    )
    second_pipeline = VoiceEmotionPipeline(tmp_path / "cache", config)
    updated = second_pipeline.analyze_dataset(demo_dataset, previous_run=initial)
    recomputed = updated.data["report"]["recomputed_nodes"]
    assert {item["node"] for item in recomputed} == {"dialog"}
    assert len(recomputed) == len(demo_dataset.dialogs)
    assert updated.data["report"]["affected_frames"] == {
        item["dialog_id"]: [] for item in updated.data["dialogs"]
    }


def test_segment_threshold_change_invalidates_assignment_suffix(
    demo_dataset,
    tmp_path,
) -> None:
    first_pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    initial = first_pipeline.analyze_dataset(demo_dataset)
    config = PipelineConfig(speaker=SpeakerConfig(segment_threshold=4))
    second_pipeline = VoiceEmotionPipeline(tmp_path / "cache", config)
    updated = second_pipeline.analyze_dataset(demo_dataset, previous_run=initial)
    nodes = {item["node"] for item in updated.data["report"]["recomputed_nodes"]}
    assert "features" not in nodes
    assert {"assignment", "segment", "dialog"} <= nodes
    assert updated.data["report"]["affected_frames"][demo_dataset.dialogs[0].dialog_id] == list(
        range(updated.data["dialogs"][0]["feature"]["frame_count"])
    )
    fresh = VoiceEmotionPipeline(tmp_path / "fresh", config).analyze_dataset(
        demo_dataset
    )
    assert updated.result_fingerprint == fresh.result_fingerprint


def test_resource_limits_and_degradation(demo_dataset, tmp_path) -> None:
    frame_limited = VoiceEmotionPipeline(
        tmp_path / "frame-limit",
        PipelineConfig(
            resources=ResourceLimits(max_frames_per_analysis=10, on_limit="error")
        ),
    )
    with pytest.raises(BusinessError) as raised:
        frame_limited.analyze_dataset(demo_dataset)
    assert raised.value.code == E_RESOURCE_LIMIT
    assert raised.value.details["resource"] == "max_frames_per_analysis"

    cache_limited = VoiceEmotionPipeline(
        tmp_path / "cache-limit",
        PipelineConfig(
            resources=ResourceLimits(max_cache_entries=1, on_limit="error")
        ),
    )
    with pytest.raises(BusinessError) as raised:
        cache_limited.analyze_dataset(demo_dataset)
    assert raised.value.code == E_RESOURCE_LIMIT
    assert raised.value.details["resource"] == "max_cache_entries"

    degraded = VoiceEmotionPipeline(
        tmp_path / "degraded",
        PipelineConfig(
            resources=ResourceLimits(
                max_frames_per_analysis=10,
                max_feature_bytes=2_500,
                on_limit="degrade",
            )
        ),
    )
    run = degraded.analyze_dataset(demo_dataset)
    assert "frame_budget_soft_limit" in run.data["degraded_categories"]
    assert "feature_precision_float32" in run.data["degraded_categories"]
    assert run.data["dialogs"][0]["feature"]["dtype"] == "float32"
    assert degraded.self_check(run)["status"] == "ok"

    cache_degraded = VoiceEmotionPipeline(
        tmp_path / "cache-degraded",
        PipelineConfig(
            resources=ResourceLimits(max_cache_entries=2, on_limit="degrade")
        ),
    )
    partial = cache_degraded.analyze_dataset(demo_dataset)
    assert "cache_retention_limited" in partial.data["degraded_categories"]
    assert partial.data["dialogs"][0]["lineage"]["traceable"] is False
    assert cache_degraded.self_check(partial)["status"] == "ok"


def test_run_consistency_failure(demo_dataset, tmp_path) -> None:
    pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    run = pipeline.analyze_dataset(demo_dataset)
    broken = copy.deepcopy(run.data)
    broken["report"]["recomputed_nodes"].append(
        copy.deepcopy(broken["report"]["recomputed_nodes"][0])
    )
    with pytest.raises(BusinessError) as raised:
        validate_run_consistency(broken)
    assert raised.value.code == E_INCONSISTENT


def test_truth_speaker_outside_declaration(demo_dataset, tmp_path) -> None:
    from voice_emotion.dataset import read_jsonl

    rows = read_jsonl(demo_dataset.dialogs[0].truth_path)
    rows[0]["speaker_id"] = 999
    write_jsonl(demo_dataset.dialogs[0].truth_path, rows)
    with pytest.raises(BusinessError) as raised:
        VoiceEmotionPipeline(tmp_path / "cache").analyze_dataset(demo_dataset)
    assert raised.value.code == E_SPEAKER_UNKNOWN
