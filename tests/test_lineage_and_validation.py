from __future__ import annotations

import pytest

from dialog_emotion.config import (
    PipelineConfig,
    ResourceLimits,
    validate_dialog_rows,
    validate_registration_rows,
)
from dialog_emotion.errors import BusinessError
from dialog_emotion.pipeline import (
    AppendRegistration,
    CorrectTruth,
    IncrementalPipeline,
    SetFrameShift,
    compare_runs,
)
from dialog_emotion.speaker import assign_speakers


def _engine(bundle, workdir):
    return IncrementalPipeline(
        bundle,
        cache_dir=workdir / "cache",
        config_snapshot_dir=workdir / "config",
        lineage_dir=workdir / "lineage",
    )


def test_old_segment_and_dialog_scores_replay_after_changes(bundle, workdir) -> None:
    engine = _engine(bundle, workdir)
    baseline = engine.run()
    segment_record = next(
        record
        for record in baseline.lineage_records
        if record["kind"] == "segment_score" and record["dialog_id"] == "dlg-002"
    )
    dialog_record = next(
        record
        for record in baseline.lineage_records
        if record["kind"] == "dialog_score" and record["dialog_id"] == "dlg-002"
    )
    engine.run([AppendRegistration("spk_a", 77, 600)])
    engine.run(
        [CorrectTruth("dlg-002", valence=0.15, arousal=0.85, segment_index=0)]
    )
    engine.run([SetFrameShift(15.0)])
    segment_replay = engine.lineage.replay(
        segment_record["record_id"], engine.cache
    )
    dialog_replay = engine.lineage.replay(dialog_record["record_id"], engine.cache)
    assert segment_replay["reproduced"] is True
    assert dialog_replay["reproduced"] is True
    assert segment_replay["score"]["valence"] == segment_record["score"]["valence"]
    assert dialog_replay["score"]["valence"] == dialog_record["score"]["valence"]
    assert segment_replay["node_path"][-1] == "segment_regression"
    assert dialog_replay["node_path"][-1] == "dialog_aggregate"
    assert segment_replay["key_components"]["frame_features"]["identity"][
        "audio_chunk_hashes"
    ]


def test_lineage_missing_key_returns_broken_chain(bundle, workdir) -> None:
    engine = _engine(bundle, workdir)
    result = engine.run()
    record = next(
        item for item in result.lineage_records if item["kind"] == "segment_score"
    )
    key = record["cache_keys"]["segment_regression"]
    cache_path = engine.cache.path_for(key)
    cache_path.unlink()
    with pytest.raises(BusinessError) as captured:
        engine.lineage.replay(record["record_id"], engine.cache)
    assert captured.value.code == "E_LINEAGE_BROKEN"
    assert captured.value.details["missing_key_level"] == "segment_regression"


def test_incremental_drift_detection_reports_first_node(bundle, workdir) -> None:
    engine = _engine(bundle, workdir)
    left = engine.run()
    right = _engine(engine.bundle.copy(), workdir / "right").run()
    right.stage_hashes["speaker_assignment:dlg-001"] = "0" * 64
    with pytest.raises(BusinessError) as captured:
        compare_runs(left, right)
    assert captured.value.code == "E_INCREMENTAL_DRIFT"
    assert captured.value.details["first_divergent_node"] == "speaker_assignment:dlg-001"


def test_required_validation_codes() -> None:
    with pytest.raises(BusinessError) as sample_rate:
        PipelineConfig(sample_rate=8000).validate()
    assert sample_rate.value.code == "E_SAMPLE_RATE"
    with pytest.raises(BusinessError) as frame:
        PipelineConfig(frame_ms=0).validate()
    assert frame.value.code == "E_FRAME_INVALID"
    with pytest.raises(BusinessError) as shift:
        PipelineConfig(frame_shift_ms=25.0).validate()
    assert shift.value.code == "E_FRAME_SHIFT_INVALID"
    with pytest.raises(BusinessError) as duplicate:
        validate_dialog_rows(
            [
                {
                    "dialog_id": "d",
                    "sample_rate": 16000,
                    "duration_ms": 100,
                    "speaker_ids": ["a"],
                    "seed": 1,
                },
                {
                    "dialog_id": "d",
                    "sample_rate": 16000,
                    "duration_ms": 100,
                    "speaker_ids": ["a"],
                    "seed": 2,
                },
            ]
        )
    assert duplicate.value.code == "E_DUPLICATE_DIALOG"
    with pytest.raises(BusinessError) as speaker:
        validate_registration_rows([{"register_seed": 1, "register_ms": 100}])
    assert speaker.value.code == "E_SPEAKER_MISSING"


def test_candidate_comparison_resource_limit(bundle) -> None:
    from dialog_emotion.features import feature_product

    dialog = bundle.dialogs["dlg-001"]
    config = PipelineConfig(
        limits=ResourceLimits(max_candidate_comparisons=1)
    )
    truth = dialog.truth_variants[config.frame_variant_key()]
    features = feature_product("dlg-001", dialog.pcm, config, len(truth))
    with pytest.raises(BusinessError) as captured:
        assign_speakers(
            "dlg-001",
            features,
            dialog.speaker_ids,
            bundle.registrations,
            config,
        )
    assert captured.value.code == "E_RESOURCE_LIMIT"
