from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from meeting_vad.errors import BusinessError
from meeting_vad.session import ALL_NODES, AnalysisSession


def test_initial_run_then_full_cache_reuse(session: AnalysisSession) -> None:
    first = session.analyze()
    assert first.snapshot_version == 1
    assert first.snapshot_replaced is True
    assert first.recomputed_nodes == list(ALL_NODES)
    assert first.reused_nodes == []
    assert first.self_check["ok"] is True

    second = session.analyze()
    assert second.snapshot_version == first.snapshot_version
    assert second.snapshot_replaced is False
    assert second.recomputed_nodes == []
    assert second.reused_nodes == list(ALL_NODES)
    assert second.interval_recompute_count == 0
    assert second.export_jsonl == first.export_jsonl


def test_annotation_change_recomputes_only_downstream_suffix(
    session: AnalysisSession,
) -> None:
    initial = session.analyze()
    result = session.correct_annotation(
        "a-002", speaker_label="S01", label_revision=2
    )
    assert result.recomputed_nodes == ["speaker", "export"]
    assert result.reused_nodes == ["envelope", "vad", "refine"]
    assert result.affected_nodes == ["speaker", "export"]
    assert result.affected_ranges[0]["clip_id"] == "meeting-a"
    assert result.interval_recompute_count == 1
    assert result.interval_reuse_count == 4
    assert result.snapshot_version > initial.snapshot_version
    assert result.self_check["ok"] is True
    items = [json.loads(line) for line in result.export_jsonl.splitlines()]
    changed = next(item for item in items if item["segment_id"] == "a-002")
    assert changed["speaker_label"] == "S01"
    assert changed["label_revision"] == 2
    replay = session.replay_export(result.export_cache_key)
    assert replay.items == items


def test_quantile_change_invalidates_vad_to_export(
    session: AnalysisSession,
) -> None:
    initial = session.analyze()
    old_config_version = session.config.config_version
    old_threshold_version = session.config.vad.threshold_version
    result = session.set_noise_quantile(0.25)
    assert result.reused_nodes == ["envelope"]
    assert result.recomputed_nodes == ["vad", "refine", "speaker", "export"]
    assert result.affected_nodes == ["vad", "refine", "speaker", "export"]
    assert result.snapshot_version == initial.snapshot_version + 1
    assert session.config.config_version == old_config_version + 1
    assert session.config.vad.threshold_version != old_threshold_version
    assert result.self_check["ok"] is True


def test_frame_change_invalidates_all_nodes(session: AnalysisSession) -> None:
    session.analyze()
    result = session.set_frame(20, 5)
    assert result.recomputed_nodes == list(ALL_NODES)
    assert result.reused_nodes == []
    assert result.self_check["ok"] is True
    for clip_id in ("meeting-a", "meeting-b"):
        status = result.node_statuses["envelope"]
        assert status["summary"]["frame_counts"][clip_id] > 0


def test_export_key_details_are_readable(session: AnalysisSession) -> None:
    session.analyze()
    details = session.key_details("export")
    material = details["key_details"]
    assert details["cache_key"]
    assert material["audio_content_hash"]
    assert material["frame_hop_ms"] == session.config.frame_hop_ms
    assert material["threshold_version"] == session.config.vad.threshold_version
    assert material["annotation_revision"] >= 1
    assert material["upstream_hash"]


def test_corrupt_snapshot_entry_returns_cache_corrupt(
    session: AnalysisSession,
) -> None:
    result = session.analyze()
    manifest = session.snapshots.current_manifest()
    export_entry = next(
        entry for entry in manifest["entries"] if entry["node"] == "export"
    )
    path = (
        session.snapshots.root
        / f"v{int(manifest['version']):06d}"
        / export_entry["path"]
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["item_count"] += 1
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(BusinessError) as exc:
        session.replay_export(result.export_cache_key)
    assert exc.value.code == "E_CACHE_CORRUPT"
    assert exc.value.details["version"] == result.snapshot_version


def test_missing_snapshot_entry_returns_lineage_broken(
    session: AnalysisSession,
) -> None:
    result = session.analyze()
    manifest = session.snapshots.current_manifest()
    entry = next(item for item in manifest["entries"] if item["node"] == "refine")
    path = (
        session.snapshots.root
        / f"v{int(manifest['version']):06d}"
        / entry["path"]
    )
    path.unlink()
    with pytest.raises(BusinessError) as exc:
        session.replay_export(result.export_cache_key)
    assert exc.value.code == "E_LINEAGE_BROKEN"


def test_snapshot_rollback_rejected(session: AnalysisSession) -> None:
    result = session.analyze()
    with pytest.raises(BusinessError) as exc:
        session.snapshots.commit_generation(
            session.config.snapshot_identity(),
            session.artifacts.values(),
            requested_version=result.snapshot_version,
        )
    assert exc.value.code == "E_SNAPSHOT_ROLLBACK"


def test_versions_are_monotonic_and_generations_are_whole(
    session: AnalysisSession,
) -> None:
    first = session.analyze()
    old_key = first.export_cache_key
    second = session.set_noise_quantile(0.25)
    assert second.snapshot_version == first.snapshot_version + 1
    assert session.snapshots.history_versions() == [
        first.snapshot_version,
        second.snapshot_version,
    ]
    current_manifest = session.snapshots.current_manifest()
    old_manifest = session.snapshots._load_manifest_file(first.snapshot_version)
    assert len(current_manifest["entries"]) == len(ALL_NODES)
    assert len(old_manifest["entries"]) == len(ALL_NODES)
    assert all(
        entry["cache_key"] != old_key
        for entry in current_manifest["entries"]
        if entry["node"] == "export"
    )
    old_replay = session.replay_export(old_key)
    assert old_replay.items
    with pytest.raises(BusinessError) as exc:
        session.snapshots.commit_generation(
            session.config.snapshot_identity(),
            session.artifacts.values(),
            requested_version=first.snapshot_version,
        )
    assert exc.value.code == "E_SNAPSHOT_ROLLBACK"


def test_snapshot_entry_resource_limit(app_config, sample_dir) -> None:
    config = replace(
        app_config,
        resources=replace(app_config.resources, max_snapshot_entries=2),
    )
    limited = AnalysisSession(config, use_runtime_annotations=False)
    with pytest.raises(BusinessError) as exc:
        limited.analyze()
    assert exc.value.code == "E_RESOURCE_LIMIT"


def test_replay_reproduces_every_interval_field(session: AnalysisSession) -> None:
    result = session.analyze()
    replay = session.replay_export(result.export_cache_key)
    lines = [
        json.loads(line) for line in result.export_jsonl.splitlines() if line
    ]
    assert replay.items == lines
    for item in replay.items:
        assert {
            "start_ms",
            "end_ms",
            "speaker_label",
            "confidence_lower",
            "confidence_upper",
        } <= set(item)
        assert item["confidence_lower"] <= item["confidence_upper"]


def test_missing_replay_record_is_lineage_broken(session: AnalysisSession) -> None:
    session.analyze()
    with pytest.raises(BusinessError) as exc:
        session.replay_export("0" * 64)
    assert exc.value.code == "E_LINEAGE_BROKEN"


def test_incremental_full_recompute_mismatch_is_reported(
    session: AnalysisSession, monkeypatch
) -> None:
    session.analyze()
    original = session._compute_fresh_export

    def tampered(config, annotations):
        payload = original(config, annotations)
        changed = [dict(item) for item in payload["items"]]
        changed[0]["speaker_label"] = "S02"
        lines = [
            json.dumps(item, sort_keys=True, separators=(",", ":"))
            for item in changed
        ]
        payload = dict(payload)
        payload["items"] = changed
        payload["jsonl"] = "\n".join(lines) + "\n"
        return payload

    monkeypatch.setattr(session, "_compute_fresh_export", tampered)
    with pytest.raises(BusinessError) as exc:
        session.analyze()
    assert exc.value.code == "E_INCONSISTENT"
    assert "first_difference_index" in exc.value.details


def test_annotation_edit_persists_for_restart(app_config, session: AnalysisSession) -> None:
    session.analyze()
    session.correct_annotation("a-002", speaker_label="S01")
    restarted = AnalysisSession(app_config, use_runtime_annotations=True)
    annotation = next(
        item for item in restarted.annotations if item.segment_id == "a-002"
    )
    assert annotation.speaker_label == "S01"
    result = restarted.analyze()
    assert result.self_check["ok"] is True


def test_runtime_config_persists_for_restart(app_config, session: AnalysisSession) -> None:
    session.analyze()
    session.set_noise_quantile(0.25)
    session.set_frame(20.0, 10.0)
    restarted = AnalysisSession(app_config, use_runtime_annotations=False)
    assert restarted.config.vad.noise_quantile == 0.25
    assert restarted.config.frame_hop_ms == 10.0
    assert restarted.config.config_version == session.config.config_version
    result = restarted.analyze()
    assert result.snapshot_version == session.last_result.snapshot_version
    assert result.recomputed_nodes == []


def test_unknown_annotation_label_does_not_commit_edit(session: AnalysisSession) -> None:
    session.analyze()
    original = next(item for item in session.annotations if item.segment_id == "a-002")
    with pytest.raises(BusinessError) as exc:
        session.correct_annotation("a-002", speaker_label="S99")
    assert exc.value.code == "E_SPEAKER_UNKNOWN"
    assert session.last_error["code"] == "E_SPEAKER_UNKNOWN"
    current = next(item for item in session.annotations if item.segment_id == "a-002")
    assert current == original
