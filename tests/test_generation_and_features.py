from __future__ import annotations

import json
from pathlib import Path

import pytest

from voice_emotion import (
    BusinessError,
    FeatureConfig,
    VoiceEmotionPipeline,
    build_demo_dataset,
    load_dataset,
)
from voice_emotion.audio import read_pcm
from voice_emotion.canonical import canonical_json
from voice_emotion.dataset import (
    load_dialogs,
    load_enrollments,
    load_truth,
    read_jsonl,
    write_jsonl,
)
from voice_emotion.errors import (
    E_DUPLICATE_DIALOG,
    E_ENROLLMENT_MISSING,
    E_FRAME_CONFIG,
    E_FRAME_COUNT,
    E_SAMPLE_RATE,
)
from voice_emotion.features import extract_features


def test_sample_generation_is_byte_deterministic(tmp_path: Path) -> None:
    first = build_demo_dataset(tmp_path / "first", dialog_count=2, duration_ms=1_000)
    second = build_demo_dataset(tmp_path / "second", dialog_count=2, duration_ms=1_000)
    first_files = sorted(
        path.relative_to(first.root).as_posix()
        for path in first.root.rglob("*")
        if path.is_file()
    )
    second_files = sorted(
        path.relative_to(second.root).as_posix()
        for path in second.root.rglob("*")
        if path.is_file()
    )
    assert first_files == second_files
    for relative in first_files:
        assert (first.root / relative).read_bytes() == (second.root / relative).read_bytes()
    assert first.enrollment_version == second.enrollment_version
    assert len(load_truth(first.dialogs[0].truth_path)) == 50


def test_feature_extraction_is_reproducible_and_frame_aligned(
    demo_dataset,
) -> None:
    dialog = demo_dataset.dialogs[0]
    audio = read_pcm(dialog.audio_path)
    config = FeatureConfig()
    first = extract_features(audio.samples, config)
    second = extract_features(audio.samples, config)
    truth = load_truth(dialog.truth_path)
    assert first.frame_count == len(truth)
    assert canonical_json(first.to_payload()) == canonical_json(second.to_payload())
    assert first.output_hash == second.output_hash
    assert first.frame_starts.tolist() == list(range(0, first.frame_count * 320, 320))
    assert first.features.shape == (first.frame_count, 4)
    assert first.feature_names == (
        "rms_energy",
        "zero_crossing_rate",
        "spectral_centroid_hz",
        "spectral_rolloff_hz",
    )


def test_frame_count_mismatch_returns_business_code(demo_dataset, tmp_path: Path) -> None:
    dialog = demo_dataset.dialogs[0]
    rows = read_jsonl(dialog.truth_path)
    write_jsonl(dialog.truth_path, rows[:-1])
    pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    with pytest.raises(BusinessError) as raised:
        pipeline.analyze_dataset(demo_dataset)
    assert raised.value.code == E_FRAME_COUNT


def test_manifest_validation_codes(demo_dataset, tmp_path: Path) -> None:
    rows = read_jsonl(demo_dataset.manifest_path)
    bad_rate = [dict(rows[0], sample_rate=8_000)] + rows[1:]
    rate_path = tmp_path / "bad-rate.jsonl"
    write_jsonl(rate_path, bad_rate)
    with pytest.raises(BusinessError) as raised:
        load_dialogs(rate_path)
    assert raised.value.code == E_SAMPLE_RATE

    duplicate_path = tmp_path / "duplicate.jsonl"
    write_jsonl(duplicate_path, [rows[0], rows[0]])
    with pytest.raises(BusinessError) as raised:
        load_dialogs(duplicate_path)
    assert raised.value.code == E_DUPLICATE_DIALOG

    enrollment_rows = read_jsonl(demo_dataset.enrollment_path)
    missing = dict(enrollment_rows[0])
    missing.pop("speaker_id")
    enrollment_path = tmp_path / "missing-enrollment.jsonl"
    write_jsonl(enrollment_path, [missing])
    with pytest.raises(BusinessError) as raised:
        load_enrollments(enrollment_path)
    assert raised.value.code == E_ENROLLMENT_MISSING

    with pytest.raises(BusinessError) as raised:
        FeatureConfig(frame_ms=0).validate()
    assert raised.value.code == E_FRAME_CONFIG
    with pytest.raises(BusinessError) as raised:
        FeatureConfig(frame_ms=20, frame_shift_ms=30).validate()
    assert raised.value.code == E_FRAME_CONFIG


def test_dataset_load_round_trip(demo_dataset) -> None:
    loaded = load_dataset(demo_dataset.manifest_path, demo_dataset.enrollment_path)
    assert loaded.dialogs == demo_dataset.dialogs
    assert loaded.enrollments == demo_dataset.enrollments
    assert loaded.enrollment_version == demo_dataset.enrollment_version
