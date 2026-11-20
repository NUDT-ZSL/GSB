from __future__ import annotations

import copy
from pathlib import Path

import pytest

from voice_emotion import (
    BusinessError,
    CacheStore,
    ConfigStore,
    FeatureConfig,
    PipelineConfig,
    VoiceEmotionPipeline,
    load_dataset,
)
from voice_emotion.canonical import hash_value, read_json, write_json_atomic
from voice_emotion.keys import KEY_SCHEMA
from voice_emotion.dataset import read_jsonl, write_jsonl
from voice_emotion.errors import (
    E_CACHE_BUSY,
    E_CACHE_CORRUPT,
    E_LINEAGE_BROKEN,
)


def test_cache_entries_record_key_composition(demo_dataset, tmp_path: Path) -> None:
    pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    run = pipeline.analyze_dataset(demo_dataset)
    dialog = run.data["dialogs"][0]
    feature = pipeline.cache.get(dialog["feature"]["cache_key"])
    assignment = pipeline.cache.get(dialog["assignment"]["cache_key"])
    segment = pipeline.cache.get(dialog["segment_keys"][0]["cache_key"])
    aggregate = pipeline.cache.get(dialog["dialog"]["cache_key"])
    assert feature.node == "features"
    assert feature.key_components["feature_params_version"] == "acoustic-v1"
    assert feature.key_components["frame_shift_ms"] == 20.0
    assert len(feature.key_components["audio_chunk_hashes"]) >= 1
    assert feature.key_components["upstream_hash"] == feature.key_components["audio_chunk_root"]
    assert assignment.node == "assignment"
    assert assignment.key_components["enrollment_version"] == demo_dataset.enrollment_version
    assert assignment.key_components["segment_threshold"] == 1
    assert assignment.key_components["upstream_cache_key"] == dialog["feature"]["cache_key"]
    assert segment.node == "segment"
    assert segment.key_components["truth_correction_version"].startswith("truthslice-")
    assert segment.key_components["regression_config_hash"]
    assert segment.key_components["upstream_cache_key"] == dialog["assignment"]["cache_key"]
    assert aggregate.node == "dialog"
    assert aggregate.key_components["truth_version"] == dialog["truth_version"]
    assert aggregate.key_components["enrollment_version"] == demo_dataset.enrollment_version
    assert aggregate.key_components["feature_params_version"] == "acoustic-v1"
    assert aggregate.key_components["segment_cache_keys"]
    assert aggregate.key_components["upstream_hash"]
    for entry in (feature, assignment, segment, aggregate):
        assert isinstance(entry.version, int) and entry.version >= 1
        assert entry.checksum
        assert entry.context_versions["config_version"] >= 1


def test_same_key_reuses_snapshot_and_versions_are_monotonic(
    demo_dataset,
    tmp_path: Path,
) -> None:
    pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    first = pipeline.analyze_dataset(demo_dataset)
    count_before = pipeline.cache.manifest.entry_count()
    second = pipeline.analyze_dataset(demo_dataset, previous_run=first)
    assert second.result_fingerprint == first.result_fingerprint
    assert pipeline.cache.manifest.entry_count() == count_before
    assert second.data["report"]["recomputed_nodes"] == []
    assert len(second.data["report"]["reused_nodes"]) > 0
    versions = [
        item["version"]
        for item in second.data["report"]["reused_nodes"]
        if item["version"] is not None
    ]
    assert all(version >= 1 for version in versions)
    assert len(versions) == len(set(versions))
    assert second.data["report"]["affected_frames"] == {
        dialog["dialog_id"]: [] for dialog in second.data["dialogs"]
    }


def test_concurrent_cache_key_write_returns_busy(tmp_path: Path) -> None:
    store = CacheStore(tmp_path / "cache")
    components = {"upstream_hash": "u"}
    key = hash_value({"schema": KEY_SCHEMA, **components})
    payload = {"value": 1}
    with store.write_lock(key):
        with pytest.raises(BusinessError) as raised:
            store.put(
                cache_key=key,
                node="test",
                key_components=components,
                context_versions={},
                payload=payload,
            )
        assert raised.value.code == E_CACHE_BUSY
    first = store.put(
        cache_key=key,
        node="test",
        key_components=components,
        context_versions={},
        payload=payload,
    )
    second = store.put(
        cache_key=key,
        node="test",
        key_components=components,
        context_versions={},
        payload=payload,
    )
    assert first is not None and second is not None
    assert first.version == second.version
    assert store.manifest.entry_count() == 1


def test_cache_corruption_is_detected(demo_dataset, tmp_path: Path) -> None:
    pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    run = pipeline.analyze_dataset(demo_dataset)
    cache_key = run.data["dialogs"][0]["feature"]["cache_key"]
    path = pipeline.cache.entry_path(cache_key)
    body = read_json(path)
    body["payload"]["frame_count"] = int(body["payload"]["frame_count"]) + 1
    write_json_atomic(path, body)
    with pytest.raises(BusinessError) as raised:
        pipeline.cache.get(cache_key)
    assert raised.value.code == E_CACHE_CORRUPT


def test_config_snapshot_whole_replacement_and_corruption(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "cache")
    first = store.put({"threshold": 1, "nested": {"mode": "a"}})
    second = store.put({"threshold": 2, "nested": {"mode": "b"}})
    assert second["version"] > first["version"]
    assert store.load(first["version"]) == first
    assert store.load() == second
    assert list(first["body"]) == ["nested", "threshold"]
    path = store.current_path
    body = read_json(path)
    body["body"]["threshold"] = 999
    write_json_atomic(path, body)
    with pytest.raises(BusinessError) as raised:
        store.current_optional()
    assert raised.value.code == E_CACHE_CORRUPT
    store.current_path.write_text("{broken-json", encoding="utf-8")
    with pytest.raises(BusinessError) as raised:
        store.current_optional()
    assert raised.value.code == E_CACHE_CORRUPT


def test_historical_scores_replay_after_truth_correction(
    demo_dataset,
    tmp_path: Path,
) -> None:
    pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    initial = pipeline.analyze_dataset(demo_dataset)
    old_dialog = initial.data["dialogs"][0]
    old_target_segment = old_dialog["segments"][1]
    old_dialog_score = copy.deepcopy(old_dialog["dialog"]["scores"])
    old_segment_score = copy.deepcopy(old_target_segment)

    rows = read_jsonl(demo_dataset.dialogs[0].truth_path)
    for frame in range(
        old_target_segment["start_frame"], old_target_segment["end_frame"]
    ):
        rows[frame]["valence"] = round(min(1.0, rows[frame]["valence"] + 0.2), 6)
    write_jsonl(demo_dataset.dialogs[0].truth_path, rows)
    updated_dataset = load_dataset(
        demo_dataset.manifest_path,
        demo_dataset.enrollment_path,
    )
    updated = pipeline.analyze_dataset(updated_dataset, previous_run=initial)
    assert (
        updated.result_fingerprint != initial.result_fingerprint
    )
    replay_dialog = pipeline.trace_score(old_dialog["lineage"]["dialog_record_id"])
    replay_segment = pipeline.trace_score(
        old_dialog["lineage"]["segment_record_ids"][old_target_segment["segment_id"]]
    )
    assert replay_dialog["replayed"] == old_dialog_score
    assert replay_segment["replayed"] == old_segment_score
    assert replay_dialog["input_versions"]["truth_version"] != updated.data["dialogs"][0][
        "truth_version"
    ]
    assert replay_dialog["node_path"][-1]["cache_key"] == old_dialog["dialog"]["cache_key"]


def test_lineage_break_reports_node_and_version(demo_dataset, tmp_path: Path) -> None:
    pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    initial = pipeline.analyze_dataset(demo_dataset)
    dialog = initial.data["dialogs"][0]
    segment_id = dialog["segments"][0]["segment_id"]
    record_id = dialog["lineage"]["segment_record_ids"][segment_id]
    record = pipeline.lineage.get(record_id)
    terminal = record["node_path"][-1]
    pipeline.cache.entry_path(terminal["cache_key"]).unlink()
    with pytest.raises(BusinessError) as raised:
        pipeline.trace_score(record_id)
    assert raised.value.code == E_LINEAGE_BROKEN
    assert raised.value.details["broken_node"] == "segment"
    assert raised.value.details["version"] == terminal["version"]
    assert raised.value.details["cache_key"] == terminal["cache_key"]


def test_historical_replay_uses_old_config_snapshot_not_current(
    demo_dataset,
    tmp_path: Path,
) -> None:
    pipeline = VoiceEmotionPipeline(tmp_path / "cache")
    initial = pipeline.analyze_dataset(demo_dataset)
    old_dialog = initial.data["dialogs"][0]
    old_scores = copy.deepcopy(old_dialog["dialog"]["scores"])
    changed_pipeline = VoiceEmotionPipeline(
        tmp_path / "cache",
        PipelineConfig(feature=FeatureConfig(fft_size=1024)),
    )
    changed = changed_pipeline.analyze_dataset(demo_dataset, previous_run=initial)
    assert changed.data["config_version"] > initial.data["config_version"]
    replay = changed_pipeline.trace_score(old_dialog["lineage"]["dialog_record_id"])
    assert replay["config_version"] == initial.data["config_version"]
    assert replay["replayed"] == old_scores
    assert (
        replay["input_versions"]["feature_params_hash"]
        != changed_pipeline.config.feature.params_hash()
    )
