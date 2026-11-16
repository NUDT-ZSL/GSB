"""Incremental invalidation, reuse ledger and serial-baseline equivalence."""
from __future__ import annotations

import os

from voiceworkbench.audio import scan_wav_files
from voiceworkbench.baseline import run_serial_baseline
from voiceworkbench.commands import load_commands
from voiceworkbench.engine import WorkbenchEngine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_incremental(config_factory, audio_dir, commands_path, snapshot_dir,
                     **config_overrides):
    cfg = config_factory(**config_overrides)
    commands = load_commands(commands_path)
    engine = WorkbenchEngine(
        cfg, scan_wav_files(audio_dir), snapshot_dir,
    )
    engine.initialize()
    for command in commands:
        engine.handle_command(command)
    return cfg, commands, engine


def test_final_state_matches_serial_baseline(tmp_path, config_factory,
                                             audio_dir, commands_path):
    snapshot_dir = str(tmp_path / "snap")
    cfg, commands, engine = _run_incremental(
        config_factory, audio_dir, commands_path, snapshot_dir,
    )
    baseline, rejected = run_serial_baseline(
        cfg, scan_wav_files(audio_dir), commands,
    )
    result = engine.latest_result
    assert rejected == engine.rejected_commands
    assert result.stage_keys == baseline.stage_keys
    assert [c.to_dict() for c in result.clusters] == [
        c.to_dict() for c in baseline.clusters
    ]
    assert [a.to_dict() for a in result.assignments] == [
        a.to_dict() for a in baseline.assignments
    ]
    assert [s.to_dict() for s in result.segments] == [
        s.to_dict() for s in baseline.segments
    ]


def test_quant4_equivalence(tmp_path, config_factory, audio_dir, commands_path):
    snapshot_dir = str(tmp_path / "snap-q4")
    cfg, commands, engine = _run_incremental(
        config_factory, audio_dir, commands_path, snapshot_dir, quant_levels=4,
    )
    baseline, _ = run_serial_baseline(cfg, scan_wav_files(audio_dir), commands)
    assert engine.latest_result.stage_keys == baseline.stage_keys
    assert [c.member_ids for c in engine.latest_result.clusters] == [
        c.member_ids for c in baseline.clusters
    ]


def test_loose_threshold_equivalence(tmp_path, config_factory, audio_dir,
                                     commands_path):
    snapshot_dir = str(tmp_path / "snap-loose")
    cfg, commands, engine = _run_incremental(
        config_factory, audio_dir, commands_path, snapshot_dir,
        cluster_distance_threshold=2.5,
    )
    baseline, _ = run_serial_baseline(cfg, scan_wav_files(audio_dir), commands)
    assert engine.latest_result.stage_keys == baseline.stage_keys
    assert [c.member_ids for c in engine.latest_result.clusters] == [
        c.member_ids for c in baseline.clusters
    ]


def test_threshold_change_only_invalidates_downstream(tmp_path, config_factory,
                                                      audio_dir, commands_path):
    cfg = config_factory()
    commands = load_commands(commands_path)
    engine = WorkbenchEngine(cfg, scan_wav_files(audio_dir),
                             str(tmp_path / "snap"))
    engine.initialize()
    threshold_command = next(
        c for c in commands if c.command_type == "set_cluster_threshold"
    )
    response = engine.handle_command(threshold_command)
    ledger = response.ledger
    reused_stages = {entry["stage"] for entry in ledger["reused"]}
    recomputed_stages = {entry["stage"] for entry in ledger["recomputed"]}
    # features and VAD are untouched; clustering + assignment recompute
    assert "features" in reused_stages
    assert "vad" in reused_stages
    assert "clustering" in recomputed_stages
    assert "assignment" in recomputed_stages
    assert all(
        entry["stage"] in ("clustering", "assignment")
        for entry in ledger["invalidated"]
    )


def test_audio_edit_invalidates_all_stages(tmp_path, config_factory,
                                           audio_dir, commands_path):
    cfg = config_factory()
    commands = load_commands(commands_path)
    engine = WorkbenchEngine(cfg, scan_wav_files(audio_dir),
                             str(tmp_path / "snap"))
    engine.initialize()
    cut_command = next(c for c in commands if c.command_type == "cut_interval")
    response = engine.handle_command(cut_command)
    invalidated_stages = {entry["stage"] for entry in response.ledger["invalidated"]}
    assert invalidated_stages == {"features", "vad", "clustering", "assignment"}


def test_unchanged_stage_is_reused_not_recomputed(tmp_path, config_factory,
                                                  audio_dir, commands_path):
    cfg = config_factory()
    commands = load_commands(commands_path)
    engine = WorkbenchEngine(cfg, scan_wav_files(audio_dir),
                             str(tmp_path / "snap"))
    engine.initialize()
    # a merge only affects clustering/assignment; features must be reused
    merge_command = next(c for c in commands if c.command_type == "merge_speakers")
    response = engine.handle_command(merge_command)
    reused = {entry["stage"] for entry in response.ledger["reused"]}
    assert "features" in reused and "vad" in reused
    recomputed = {entry["stage"] for entry in response.ledger["recomputed"]}
    assert "features" not in recomputed


def test_member_order_is_deterministic(tmp_path, config_factory, audio_dir,
                                       commands_path):
    cfg, commands, engine = _run_incremental(
        config_factory, audio_dir, commands_path,
        str(tmp_path / "snap-order"),
    )
    for cluster in engine.latest_result.clusters:
        assert cluster.member_ids == sorted(cluster.member_ids)
