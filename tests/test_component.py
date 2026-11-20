from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from dialog_emotion import nodes
from dialog_emotion.cache import CacheStore
from dialog_emotion.config import PipelineConfig
from dialog_emotion.dataset import load_dataset, load_truth, load_wav_pcm
from dialog_emotion.demo import _divergence_case
from dialog_emotion.errors import (
    DomainError,
    E_CONFIG_INVALID,
    E_DUP_DIALOG,
    E_FRAME_COUNT_MISMATCH,
    E_INCREMENTAL_DIVERGENCE,
    E_INCONSISTENT,
    E_INTERVAL_INVALID,
    E_REGISTRY_INVALID,
    E_RESOURCE_LIMIT,
    E_SEGMENT_GAP,
    E_SPEAKER_SET_INVALID,
)
from dialog_emotion.lineage import LineageStore
from dialog_emotion.nodes import (
    build_frame_features,
    build_speaker_assignment,
    cross_node_self_check,
    validate_intervals,
    validate_speaker_output,
)
from dialog_emotion.pipeline import DialogueEmotionPipeline
from dialog_emotion.synth import append_registry_file, generate_dataset
from dialog_emotion.util import canonical_json_bytes, sha256_file_chunks, write_jsonl


def _assert_code(code: str, fn) -> DomainError:
    with pytest.raises(DomainError) as captured:
        fn()
    assert captured.value.code == code
    return captured.value


def test_generation_and_frame_determinism(sample_dataset, tmp_path: Path) -> None:
    root, manifest = sample_dataset
    assert manifest["dialog_count"] == 3
    assert manifest["sample_rate"] == 16000
    dialog_file = root / "dialog.jsonl"
    registry_file = root / "registry.jsonl"
    assert dialog_file.exists() and registry_file.exists()
    assert (root / "corrections.jsonl").exists()
    second_root = tmp_path / "regenerated"
    generate_dataset(second_root, force=True)
    for relative in (
        "dialog.jsonl",
        "registry.jsonl",
        "corrections.jsonl",
        "audio/dlg_001.wav",
        "truth/dlg_001_hop20ms.jsonl",
        "truth/dlg_001_hop10ms.jsonl",
    ):
        assert sha256_file_chunks(root / relative) == sha256_file_chunks(second_root / relative)
    config = PipelineConfig()
    dataset = load_dataset(root, config)
    dialog = dataset.dialogs[0]
    truth = load_truth(dialog.truth_path)
    pcm, sample_rate, _ = load_wav_pcm(dialog.audio_path)
    assert sample_rate == 16000
    audio_hash = sha256_file_chunks(dialog.audio_path)
    first, _ = build_frame_features(
        dialog.dialog_id,
        pcm,
        sample_rate,
        len(truth),
        config,
        audio_hash,
    )
    second, _ = build_frame_features(
        dialog.dialog_id,
        pcm,
        sample_rate,
        len(truth),
        config,
        audio_hash,
    )
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first["frame_count"] == len(truth)
    assert first["frame_length_samples"] == 320
    assert first["feature_names"] == [
        "short_time_energy",
        "zero_crossing_rate",
        "spectral_centroid_hz",
        "spectral_rolloff_hz",
    ]
    mismatch = _assert_code(
        E_FRAME_COUNT_MISMATCH,
        lambda: build_frame_features(
            dialog.dialog_id,
            pcm,
            sample_rate,
            len(truth),
            PipelineConfig(frame_shift_ms=10.0),
            audio_hash,
        ),
    )
    assert mismatch.details["dialog_id"] == dialog.dialog_id
    assert mismatch.details["expected_frame_count"] == len(truth)


def test_speaker_assignment_and_tie_break(sample_dataset, monkeypatch) -> None:
    root, _ = sample_dataset
    config = PipelineConfig()
    dataset = load_dataset(root, config)
    dialog = dataset.dialogs[0]
    frame_payload = {
        "frame_count": 4,
        "features": [
            [0.1, 0.2, 1000.0, 2000.0],
            [0.2, 0.3, 1100.0, 2100.0],
            [0.3, 0.4, 1200.0, 2200.0],
            [0.4, 0.5, 1300.0, 2300.0],
        ],
    }
    monkeypatch.setattr(
        nodes,
        "_reference_matrix",
        lambda sample, config: np.zeros((5, 4), dtype=np.float64),
    )
    speaker_payload = build_speaker_assignment(
        dialog.dialog_id,
        frame_payload,
        dataset.registry,
        dialog.speaker_ids,
        config,
        "sha256:features",
    )
    assert speaker_payload["frame_labels"] == ["spk_a"] * 4
    validate_speaker_output(
        speaker_payload["frame_labels"],
        speaker_payload["segments"],
        set(dialog.speaker_ids),
        dialog.dialog_id,
    )
    invalid = _assert_code(
        E_SPEAKER_SET_INVALID,
        lambda: validate_speaker_output(
            ["spk_unknown"],
            [{"segment_id": "s0", "start_frame": 0, "end_frame": 1, "speaker_id": "spk_unknown"}],
            set(dialog.speaker_ids),
            dialog.dialog_id,
        ),
    )
    assert invalid.details["frame_position"] == 0
    gap = _assert_code(
        E_SEGMENT_GAP,
        lambda: validate_speaker_output(
            ["spk_a", "spk_b"],
            [
                {"segment_id": "s0", "start_frame": 0, "end_frame": 1, "speaker_id": "spk_a"},
                {"segment_id": "s1", "start_frame": 2, "end_frame": 2, "speaker_id": "spk_b"},
            ],
            set(dialog.speaker_ids),
            dialog.dialog_id,
        ),
    )
    assert gap.details["frame_position"] == 1


def test_interval_and_cross_node_errors() -> None:
    invalid = _assert_code(
        E_INTERVAL_INVALID,
        lambda: validate_intervals(
            [
                {
                    "segment_id": "seg_bad",
                    "ci_lower_valence": 0.8,
                    "ci_upper_valence": 0.2,
                    "ci_lower_arousal": -0.2,
                    "ci_upper_arousal": 0.2,
                }
            ],
            "dlg",
        ),
    )
    assert invalid.details["segment_id"] == "seg_bad"
    inconsistent = _assert_code(
        E_INCONSISTENT,
        lambda: cross_node_self_check(
            "dlg",
            {"frame_count": 2},
            {"frame_count": 2, "frame_labels": ["spk_a"], "segments": []},
            {"segments": []},
            {"weights": []},
        ),
    )
    assert "frame_label_count" in inconsistent.details["categories"]


def test_full_cache_and_incremental_updates(sample_dataset, tmp_path: Path) -> None:
    root, _ = sample_dataset
    cache = tmp_path / "cache"
    lineage = tmp_path / "lineage"
    pipeline = DialogueEmotionPipeline(cache, lineage)
    baseline = pipeline.analyze(root, PipelineConfig(), verify_full_recompute=True)
    assert baseline["business_code"] == "OK"
    assert baseline["incremental_verification"]["status"] == "OK"
    assert all(dialog["frame_features"]["frame_count"] == dialog["frame_features"]["truth_frame_count"] for dialog in baseline["dialogs"])
    assert all(dialog["dialog_score"]["recomputation_consistent"] for dialog in baseline["dialogs"])
    assert all(abs(dialog["dialog_score"]["sum_weights"] - 1.0) < 1e-9 for dialog in baseline["dialogs"])

    repeated = pipeline.analyze(root, PipelineConfig(), verify_full_recompute=True)
    assert set(repeated["invalidation"]["reused_node_types"]) == {
        "frame_features",
        "speaker_embedding",
        "segment_emotion",
        "dialog_aggregate",
    }
    assert repeated["invalidation"]["recomputed_nodes"] == []
    assert repeated["run_id"] == baseline["run_id"]

    first_segment = baseline["dialogs"][0]["segments"][0]
    start = first_segment["start_frame"] + 1
    end = min(first_segment["end_frame"], start + 3)
    corrections = root / "test_corrections.jsonl"
    write_jsonl(
        corrections,
        [
            {
                "dialog_id": baseline["dialogs"][0]["dialog_id"],
                "start_frame": start,
                "end_frame": end,
                "valence": 0.95,
                "arousal": -0.9,
                "version": "test-c1",
            }
        ],
    )
    corrected = pipeline.analyze(
        root,
        PipelineConfig(),
        corrections_path=corrections,
        verify_full_recompute=True,
    )
    dlg1_events = corrected["invalidation"]["per_dialog"]["dlg_001"]
    assert dlg1_events["reused"] == ["frame_features", "speaker_embedding"]
    assert dlg1_events["recomputed"] == ["dialog_aggregate", "segment_emotion"]
    assert corrected["invalidation"]["segment_detail_reuse"]["reused"] > 0
    assert corrected["incremental_verification"]["status"] == "OK"

    registry_path = root / "test_registry_appended.jsonl"
    append_registry_file(
        root,
        registry_path,
        {
            "speaker_id": "spk_a",
            "ref_seed": 7199,
            "ref_duration_ms": 1100,
            "embedding_version": "embed-v1",
        },
    )
    registration = pipeline.analyze(
        root,
        PipelineConfig(),
        registry_path=registry_path,
        corrections_path=corrections,
        verify_full_recompute=True,
    )
    assert "registration_append" in registration["invalidation"]["change_types"]
    assert registration["invalidation"]["per_dialog"]["dlg_001"]["reused"] == ["frame_features"]
    assert registration["incremental_verification"]["status"] == "OK"

    shifted = pipeline.analyze(
        root,
        PipelineConfig(frame_shift_ms=10.0),
        registry_path=registry_path,
        corrections_path=corrections,
        verify_full_recompute=True,
    )
    assert "frame_shift" in shifted["invalidation"]["change_types"]
    assert shifted["invalidation"]["recomputed_node_types"] == [
        "dialog_aggregate",
        "frame_features",
        "segment_emotion",
        "speaker_embedding",
    ]
    assert shifted["incremental_verification"]["status"] == "OK"
    assert shifted["dialogs"][0]["frame_features"]["frame_count"] == 399


def test_lineage_replay_is_read_only(sample_dataset, tmp_path: Path) -> None:
    root, _ = sample_dataset
    cache_root = tmp_path / "cache"
    lineage_root = tmp_path / "lineage"
    pipeline = DialogueEmotionPipeline(cache_root, lineage_root)
    report = pipeline.analyze(root, PipelineConfig(), verify_full_recompute=True)
    score_id = report["dialogs"][0]["lineage"]["segment_score_ids"][0]
    dialog_score_id = report["dialogs"][0]["lineage"]["dialog_score_ids"][0]
    snapshot_before = (cache_root / "snapshot.json").read_bytes()
    files_before = sorted(path.relative_to(cache_root) for path in cache_root.rglob("*"))
    result = LineageStore(lineage_root).replay(score_id, CacheStore(cache_root, read_only=True))
    dialog_result = LineageStore(lineage_root).replay(
        dialog_score_id, CacheStore(cache_root, read_only=True)
    )
    snapshot_after = (cache_root / "snapshot.json").read_bytes()
    files_after = sorted(path.relative_to(cache_root) for path in cache_root.rglob("*"))
    assert result["status"] == "OK"
    assert dialog_result["status"] == "OK"
    assert result["cache_writes_performed"] == 0
    assert result["cache_keys"] == {
        key: report["dialogs"][0]["cache_keys"][key]
        for key in result["cache_keys"]
    }
    assert snapshot_before == snapshot_after
    assert files_before == files_after


def test_incremental_divergence_reports_first_segment() -> None:
    case = _divergence_case()
    assert case["actual_code"] == E_INCREMENTAL_DIVERGENCE
    assert case["result"]["details"]["first_divergent_segment"] == "seg_0000"


def test_validation_and_resource_codes(sample_dataset, tmp_path: Path) -> None:
    root, _ = sample_dataset
    _assert_code(E_CONFIG_INVALID, lambda: PipelineConfig(sample_rate=22050).validate())
    _assert_code(E_CONFIG_INVALID, lambda: PipelineConfig(frame_shift_ms=25.0).validate())
    duplicate = tmp_path / "duplicate"
    duplicate.mkdir()
    row = {
        "dialog_id": "dup",
        "sample_rate": 16000,
        "duration_ms": 20,
        "seed": 1,
        "speaker_ids": ["spk_a"],
        "audio_path": "audio/dup.wav",
        "truth_path": "truth/dup.jsonl",
    }
    write_jsonl(duplicate / "dialog.jsonl", [row, row])
    _assert_code(E_DUP_DIALOG, lambda: load_dataset(duplicate, PipelineConfig()))

    missing = tmp_path / "missing"
    missing.mkdir()
    write_jsonl(missing / "dialog.jsonl", [dict(row, dialog_id="single")])
    write_jsonl(
        missing / "registry.jsonl",
        [{"ref_seed": 1, "ref_duration_ms": 1000, "embedding_version": "embed-v1"}],
    )
    _assert_code(E_REGISTRY_INVALID, lambda: load_dataset(missing, PipelineConfig()))

    config = PipelineConfig()
    dataset = load_dataset(root, config)
    dialog = dataset.dialogs[0]
    pcm, sample_rate, _ = load_wav_pcm(dialog.audio_path)
    truth = load_truth(dialog.truth_path)
    audio_hash = sha256_file_chunks(dialog.audio_path)
    _assert_code(
        E_RESOURCE_LIMIT,
        lambda: build_frame_features(
            dialog.dialog_id,
            pcm,
            sample_rate,
            len(truth),
            PipelineConfig(max_feature_memory_mb=0.000001),
            audio_hash,
        ),
    )
    degraded, categories = build_frame_features(
        dialog.dialog_id,
        pcm,
        sample_rate,
        len(truth),
        PipelineConfig(max_feature_memory_mb=0.000001, resource_policy="degrade"),
        audio_hash,
    )
    assert degraded["feature_dtype"] == "float32"
    assert categories == ["feature_precision_float32"]
    effective, parallel_categories = PipelineConfig(
        max_parallel_nodes=1,
        requested_parallel_nodes=3,
    ).effective_parallel_nodes()
    assert effective == 1
    assert parallel_categories == ["parallelism_capped"]
