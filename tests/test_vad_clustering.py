"""VAD hysteresis, overlap marking, clustering and assignment tests."""
from __future__ import annotations

import numpy as np

from voiceworkbench.audio import scan_wav_files
from voiceworkbench.clustering import (
    agglomerative,
    assign_segments,
    merge_clusters,
    relabel,
    split_cluster,
)
from voiceworkbench.features import extract_all
from voiceworkbench.vad import (
    SpeechSegment,
    apply_minimum_durations,
    classify_frames,
    segment_all,
)


def _segments(config_factory, audio_dir, **overrides):
    cfg = config_factory(**overrides)
    features, failures = extract_all(scan_wav_files(audio_dir), cfg)
    assert failures == []
    return cfg, segment_all(features, cfg)


def test_vad_produces_speech_and_gates(config_factory, audio_dir):
    cfg, segments = _segments(config_factory, audio_dir)
    assert segments
    for segment in segments:
        assert segment.end_frame > segment.start_frame
        assert segment.end_ms > segment.start_ms
        assert segment.gates["energy_db_floor"] == cfg.energy_db_floor
        assert segment.gates["flatness_enter_below"] == cfg.flatness_high
        assert segment.gates["flatness_hold_below"] == cfg.flatness_low
    # segments globally ordered by (file, start frame)
    keys = [(s.file_name, s.start_frame) for s in segments]
    assert keys == sorted(keys)


def test_overlap_pairs_detected(config_factory, audio_dir):
    _, segments = _segments(config_factory, audio_dir)
    overlaps = [s for s in segments if s.overlap]
    # two synthetic overlap pairs -> four marked segments
    assert len(overlaps) == 4
    partners = {s.segment_id: s.overlap_partner for s in overlaps}
    for segment_id, partner in partners.items():
        assert partners[partner] == segment_id
        assert "observed_voiceprint_distance" in next(
            s.gates for s in segments if s.segment_id == segment_id
        )


def test_minimum_duration_merging(config_factory):
    cfg = config_factory()
    # two short runs separated by a short silence get merged
    runs = [(0, 3), (4, 20)]  # gap of 1 frame < min silence
    merged = apply_minimum_durations(runs, 20, cfg)
    assert merged == [(0, 20)]
    # a run shorter than min speech is dropped into surrounding silence
    short = [(0, 1), (cfg.min_speech_frames + 5, cfg.min_speech_frames + 10)]
    kept = apply_minimum_durations(short, 100, cfg)
    assert all(end - start >= cfg.min_speech_frames for start, end in kept)


def test_hysteresis_requires_hold_threshold(config_factory):
    cfg = config_factory()
    from voiceworkbench.features import FrameRecord
    frames = []
    # loud + tonal enters, then a frame that fails the hold gate must exit
    specs = [
        (-30.0, 0.1), (-30.0, 0.1), (-30.0, 0.9), (-60.0, 0.1),
    ]
    for index, (energy, flatness) in enumerate(specs):
        frames.append(FrameRecord(
            frame_index=index, start_sample=index, start_ms=index,
            end_ms=index + 1, energy_db=energy, zcr=0.1, flatness=flatness,
            voiceprint=[0] * cfg.voiceprint_dims, frame_fingerprint=f"fp{index}",
        ))
    voiced = classify_frames(frames, cfg)
    assert voiced[0] is True
    assert voiced[-1] is False


def test_clustering_is_deterministic_and_three_speakers(config_factory, audio_dir):
    cfg, segments = _segments(config_factory, audio_dir)
    first = agglomerative(segments, cfg.cluster_distance_threshold)
    second = agglomerative(segments, cfg.cluster_distance_threshold)
    assert [c.to_dict() for c in first] == [c.to_dict() for c in second]
    assert len(first) == 3
    labels = [c.label for c in first]
    assert labels == sorted(labels)


def test_threshold_changes_cluster_count(config_factory, audio_dir):
    _, segments = _segments(config_factory, audio_dir)
    tight = agglomerative(segments, 0.5)
    loose = agglomerative(segments, 8.0)
    assert len(tight) >= len(loose)
    assert len(loose) == 1


def test_assignment_order_and_tie_break(config_factory, audio_dir):
    cfg, segments = _segments(config_factory, audio_dir)
    clusters = agglomerative(segments, cfg.cluster_distance_threshold)
    assignments, failures = assign_segments(segments, clusters, cfg)
    assert failures == []
    assert len(assignments) == len(segments)
    expected_order = sorted(
        (s.file_name, s.start_frame, s.segment_id) for s in segments
    )
    actual_order = []
    segment_index = {s.segment_id: s for s in segments}
    for assignment in assignments:
        segment = segment_index[assignment.segment_id]
        actual_order.append((segment.file_name, segment.start_frame, segment.segment_id))
    assert actual_order == expected_order
    for assignment in assignments:
        assert assignment.label in {c.label for c in clusters}
        assert 0.0 <= assignment.score <= 1.0


def test_merge_then_split_roundtrip(config_factory, audio_dir):
    _, segments = _segments(config_factory, audio_dir)
    clusters = agglomerative(segments, 1.2)
    labels = sorted(c.label for c in clusters)
    merged = merge_clusters(clusters, labels[0], labels[1], segments)
    assert len(merged) == len(clusters) - 1
    # splitting a multi-member cluster yields two children and restores count
    target = next(c for c in merged if c.origin == "merged")
    split = split_cluster(merged, target.label, segments)
    assert len(split) == len(clusters)
    # canonical labels are re-derived deterministically
    assert [c.label for c in split] == [f"SPK-{i:02d}" for i in range(len(split))]
    total_members = sum(len(c.member_ids) for c in split)
    assert total_members == len(segments)


def test_split_singleton_rejected(config_factory, audio_dir):
    from voiceworkbench.errors import CommandError
    _, segments = _segments(config_factory, audio_dir, cluster_distance_threshold=0.01)
    clusters = agglomerative(segments, 0.01)
    singleton = next((c for c in clusters if len(c.member_ids) == 1), None)
    if singleton is not None:
        with __import__("pytest").raises(CommandError):
            split_cluster(clusters, singleton.label, segments)
