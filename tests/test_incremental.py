from __future__ import annotations

from dialog_emotion.pipeline import (
    AppendRegistration,
    CorrectTruth,
    IncrementalPipeline,
    SetFrameShift,
    SetSegmentThreshold,
    compare_runs,
)
from dialog_emotion.truth import TruthStore


def _engine(bundle, root, config=None, truth_store=None):
    return IncrementalPipeline(
        bundle,
        config=config,
        truth_store=truth_store,
        cache_dir=root / "cache",
        config_snapshot_dir=root / "config",
        lineage_dir=root / "lineage",
    )


def _full_copy(engine, root):
    bundle = engine.bundle.copy()
    truth_store = TruthStore(
        {
            dialog_id: dialog.truth_variants
            for dialog_id, dialog in bundle.dialogs.items()
        },
        engine.truth_store.corrections,
    )
    return _engine(
        bundle,
        root,
        config=engine.config,
        truth_store=truth_store,
    ).run()


def test_truth_correction_works_with_required_fields_only() -> None:
    store = TruthStore(
        {
            "d": {
                "20/15": [
                    {
                        "frame_index": index,
                        "speaker_id": "a",
                        "valence": 0.5,
                        "arousal": 0.5,
                        "is_speech": True,
                    }
                    for index in range(4)
                ]
            }
        }
    )
    store.add_correction(
        dialog_id="d",
        start_frame=0,
        end_frame=3,
        hop_ms=15.0,
        valence=0.1,
        arousal=0.9,
    )
    rows = store.rows("d", "20/15")
    assert [row["valence"] for row in rows] == [0.1, 0.1, 0.1, 0.5]
    assert [row["arousal"] for row in rows] == [0.9, 0.9, 0.9, 0.5]


def test_registration_append_reuses_features_and_unaffected_dialog(
    bundle, workdir
) -> None:
    engine = _engine(bundle, workdir)
    baseline = engine.run()
    result = engine.run([AppendRegistration("spk_a", 909, 650)])
    assert "frame_features:dlg-001" in result.report["reused_nodes"]
    assert "speaker_assignment:dlg-001" in result.report["recomputed_nodes"]
    assert "frame_features:dlg-002" in result.report["reused_nodes"]
    assert "dialog_aggregate:dlg-002" in result.report["reused_nodes"]
    assert result.report["self_check"]["passed"]
    full = _full_copy(engine, workdir / "full")
    assert compare_runs(result, full)["matched"]


def test_single_segment_truth_correction_is_scoped(bundle, workdir) -> None:
    engine = _engine(bundle, workdir)
    engine.run()
    result = engine.run(
        [CorrectTruth("dlg-002", valence=0.11, arousal=0.89, segment_index=0)]
    )
    assert result.report["affected_segments"] == {"dlg-002": [0]}
    assert "segment_boundary:dlg-002" in result.report["reused_nodes"]
    assert "speaker_assignment:dlg-002" in result.report["reused_nodes"]
    corrected = result.segment_scores["dlg-002"][0]
    assert corrected["valence"] < 0.3
    assert corrected["arousal"] > 0.7
    assert result.report["self_check"]["passed"]
    full = _full_copy(engine, workdir / "full")
    assert compare_runs(result, full)["matched"]


def test_frame_shift_recomputes_feature_suffix(bundle, workdir) -> None:
    engine = _engine(bundle, workdir)
    engine.run()
    result = engine.run([SetFrameShift(15.0)])
    assert all(
        node in result.report["recomputed_nodes"]
        for node in result.report["recomputed_nodes"]
        if node.startswith("frame_features:")
    )
    assert result.features["dlg-001"]["frame_count"] == 119
    full = _full_copy(engine, workdir / "full")
    assert compare_runs(result, full)["matched"]


def test_segment_threshold_reuses_features_and_matches_full(bundle, workdir) -> None:
    engine = _engine(bundle, workdir)
    engine.run()
    result = engine.run([SetSegmentThreshold(320.0)])
    assert "frame_features:dlg-001" in result.report["reused_nodes"]
    assert "speaker_assignment:dlg-001" in result.report["reused_nodes"]
    assert "segment_boundary:dlg-001" in result.report["recomputed_nodes"]
    assert result.report["affected_segments"]
    full = _full_copy(engine, workdir / "full")
    assert compare_runs(result, full)["matched"]


def test_sequential_changes_match_clean_full_run(bundle, workdir) -> None:
    engine = _engine(bundle, workdir)
    engine.run()
    result = engine.run([AppendRegistration("spk_a", 123, 500)])
    result = engine.run(
        [CorrectTruth("dlg-002", valence=0.2, arousal=0.8, segment_index=0)]
    )
    result = engine.run([SetFrameShift(15.0)])
    result = engine.run([SetSegmentThreshold(300.0)])
    full = _full_copy(engine, workdir / "full")
    assert compare_runs(result, full)["matched"]
    assert result.report["self_check"]["passed"]
