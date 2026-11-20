from __future__ import annotations

import copy

import pytest

from voice_emotion import BusinessError, VoiceEmotionPipeline
from voice_emotion.audio import read_pcm
from voice_emotion.config import FeatureConfig, RegressionConfig, SpeakerConfig
from voice_emotion.dataset import load_truth
from voice_emotion.errors import E_INTERVAL_INVALID, E_SPEAKER_UNKNOWN
from voice_emotion.features import extract_features
from voice_emotion.regression import aggregate_segments
from voice_emotion.speaker import (
    assign_speakers,
    build_speaker_registry,
    choose_speaker,
)


def test_speaker_assignment_is_deterministic_and_contiguous(demo_dataset) -> None:
    feature_config = FeatureConfig()
    speaker_config = SpeakerConfig()
    registry = build_speaker_registry(
        demo_dataset.enrollments,
        feature_config,
        demo_dataset.enrollment_version,
    )
    dialog = demo_dataset.dialogs[0]
    audio = read_pcm(dialog.audio_path)
    features = extract_features(audio.samples, feature_config)
    first, speakers = assign_speakers(
        features,
        registry,
        dialog.speaker_ids,
        speaker_config,
        feature_config.frame_samples(),
    )
    second, _ = assign_speakers(
        features,
        registry,
        dialog.speaker_ids,
        speaker_config,
        feature_config.frame_samples(),
    )
    assert first.output_hash == second.output_hash
    assert sorted(speakers) == list(dialog.speaker_ids)
    assert set(first.labels.tolist()) <= set(dialog.speaker_ids)
    cursor = 0
    for segment in first.segments:
        assert segment.start_frame == cursor
        assert segment.end_frame > segment.start_frame
        assert segment.speaker_id in dialog.speaker_ids
        cursor = segment.end_frame
    assert cursor == features.frame_count
    truth_labels = [row["speaker_id"] for row in load_truth(dialog.truth_path)]
    accuracy = sum(
        int(predicted == actual)
        for predicted, actual in zip(first.labels.tolist(), truth_labels)
    ) / len(truth_labels)
    assert accuracy >= 0.75


def test_tie_break_and_unknown_speaker(demo_dataset) -> None:
    assert choose_speaker({2: -0.5, 1: -0.5}) == 1
    assert choose_speaker({3: -1.0, 1: -2.0, 2: -0.2}) == 2
    feature_config = FeatureConfig()
    filtered = [
        record for record in demo_dataset.enrollments if record.speaker_id != 3
    ]
    registry = build_speaker_registry(
        filtered,
        feature_config,
        demo_dataset.enrollment_version,
    )
    dialog = demo_dataset.dialogs[0]
    audio = read_pcm(dialog.audio_path)
    features = extract_features(audio.samples, feature_config)
    with pytest.raises(BusinessError) as raised:
        assign_speakers(
            features,
            registry,
            dialog.speaker_ids,
            SpeakerConfig(),
            feature_config.frame_samples(),
        )
    assert raised.value.code == E_SPEAKER_UNKNOWN


def test_segment_regression_intervals_and_independent_aggregation(
    demo_dataset,
    tmp_path,
) -> None:
    config = RegressionConfig()
    pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    run = pipeline.analyze_dataset(demo_dataset)
    for dialog in run.data["dialogs"]:
        for segment in dialog["segments"]:
            for target in ("valence", "arousal"):
                value = segment[target]
                assert value["ci_low"] <= value["score"] <= value["ci_high"]
        recomputed = aggregate_segments(
            dialog["segments"],
            pipeline.config.aggregation,
        )
        assert recomputed["output_hash"] == dialog["dialog"]["output_hash"]
        assert recomputed["scores"] == dialog["dialog"]["scores"]
        assert (
            dialog["dialog"]["aggregation"]["weight_basis"]
            == "segment_duration_ms"
        )
        assert sum(
            item["weight"] for item in dialog["dialog"]["aggregation"]["weights"]
        ) == pytest.approx(dialog["dialog"]["aggregation"]["total_weight"])
    assert config.confidence_z == 1.96


def test_invalid_interval_returns_business_code(demo_dataset, tmp_path) -> None:
    pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    run = pipeline.analyze_dataset(demo_dataset)
    segments = copy.deepcopy(run.data["dialogs"][0]["segments"])
    segments[0]["valence"]["ci_low"], segments[0]["valence"]["ci_high"] = (
        segments[0]["valence"]["ci_high"],
        segments[0]["valence"]["ci_low"],
    )
    with pytest.raises(BusinessError) as raised:
        aggregate_segments(segments, pipeline.config.aggregation)
    assert raised.value.code == E_INTERVAL_INVALID


def test_ranking_and_scores_are_reproducible(demo_dataset, tmp_path) -> None:
    first = VoiceEmotionPipeline(tmp_path / "first").analyze_dataset(demo_dataset)
    second = VoiceEmotionPipeline(tmp_path / "second").analyze_dataset(demo_dataset)
    assert first.result_fingerprint == second.result_fingerprint
    assert first.data["ranking"] == second.data["ranking"]
    sorted_ids = [
        item["dialog_id"]
        for item in sorted(
            first.data["dialogs"],
            key=lambda item: (
                -item["dialog"]["scores"]["valence"]["score"],
                item["dialog_id"],
            ),
        )
    ]
    assert [item["dialog_id"] for item in first.data["ranking"]] == sorted_ids
