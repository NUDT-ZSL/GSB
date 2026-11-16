"""Snapshot recovery, graceful exit and command validation tests."""
from __future__ import annotations

import json
import os

import pytest

from voiceworkbench.audio import scan_wav_files
from voiceworkbench.baseline import run_serial_baseline
from voiceworkbench.commands import load_commands, parse_command
from voiceworkbench.engine import WorkbenchEngine
from voiceworkbench.errors import CommandError, SnapshotError
from voiceworkbench.snapshot import SnapshotStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_snapshot_files_are_atomically_replaced(tmp_path):
    store = SnapshotStore(str(tmp_path))
    assert store.exists() is False
    from voiceworkbench.snapshot import SnapshotData
    from voiceworkbench.cache import StageCache
    data = SnapshotData(
        schema_version=1, stage_name="assignment", config_hash="cfg",
        command_cursor=1, completed_segments=["a"], product_version=1,
        material_fingerprint="m", stage_cache_path=store.cache_path,
        threshold_override=None, cluster_overrides=[], material={},
    )
    store.save(data, StageCache())
    assert os.path.exists(store.path)
    loaded = store.load()
    assert loaded.command_cursor == 1
    # no leftover temp files after atomic replace
    leftovers = [name for name in os.listdir(str(tmp_path)) if name.startswith(".tmp")]
    assert leftovers == []


def test_recovery_resumes_to_identical_state(tmp_path, config_factory,
                                             audio_dir, commands_path):
    cfg = config_factory()
    commands = load_commands(commands_path)
    snapshot_dir = str(tmp_path / "snap")

    first = WorkbenchEngine(cfg, scan_wav_files(audio_dir), snapshot_dir)
    first.initialize()
    for command in commands:
        first.handle_command(command)
    uninterrupted = first.diagnostics()

    # a brand-new engine restores from the completed snapshot
    restored = WorkbenchEngine(cfg, scan_wav_files(audio_dir), snapshot_dir)
    restored.initialize()
    assert restored.command_cursor == first.command_cursor
    assert restored.product_version == first.product_version
    redone = restored.diagnostics()
    assert redone["stage_keys"] == uninterrupted["stage_keys"]
    assert redone["clusters"] == uninterrupted["clusters"]
    assert redone["assignments"] == uninterrupted["assignments"]
    assert redone["material_fingerprint"] == uninterrupted["material_fingerprint"]


def test_corrupt_snapshot_is_ignored(tmp_path, config_factory, audio_dir):
    cfg = config_factory()
    snapshot_dir = str(tmp_path / "snap")
    engine = WorkbenchEngine(cfg, scan_wav_files(audio_dir), snapshot_dir)
    engine.initialize()
    # tamper with the snapshot after a valid write
    with open(os.path.join(snapshot_dir, "snapshot.json"), "w") as handle:
        handle.write('{"broken": true}')
    rebuilt = WorkbenchEngine(cfg, scan_wav_files(audio_dir), snapshot_dir)
    result = rebuilt.initialize()  # falls back to a clean bootstrap
    assert result is not None
    assert rebuilt.command_cursor == 0


def test_graceful_exit_after_command(tmp_path, config_factory, audio_dir,
                                     commands_path):
    cfg = config_factory()
    commands = load_commands(commands_path)
    snapshot_dir = str(tmp_path / "snap-exit")
    engine = WorkbenchEngine(cfg, scan_wav_files(audio_dir), snapshot_dir)
    engine.initialize()
    engine.handle_command(commands[0])
    # simulate the CLI graceful-exit path: persist then a fresh process resumes
    engine._persist_snapshot("assignment")
    resumed = WorkbenchEngine(cfg, scan_wav_files(audio_dir), snapshot_dir)
    resumed.initialize()
    assert resumed.command_cursor == 1
    for command in commands[1:]:
        resumed.handle_command(command)
    baseline, _ = run_serial_baseline(cfg, scan_wav_files(audio_dir), commands)
    assert resumed.latest_result.stage_keys == baseline.stage_keys


def test_command_validation_rejects_bad_shapes():
    bad_cases = [
        {"seq": 1, "command_type": "set_cluster_threshold",
         "params": {"cluster_distance_threshold": 99.0}},
        {"seq": 2, "command_type": "cut_interval",
         "params": {"file_name": "x.wav", "start_ms": 100, "end_ms": 50}},
        {"seq": 3, "command_type": "merge_speakers",
         "params": {"label_a": {"nth": -1}, "label_b": {"nth": 1}}},
        {"seq": 4, "command_type": "split_speaker",
         "params": {"label": {"nth": 0}, "extra": 1}},
        {"seq": 5, "command_type": "totally_unknown", "params": {}},
    ]
    for case in bad_cases:
        with pytest.raises(CommandError) as exc:
            parse_command(case)
        assert exc.value.code == "E_COMMAND"


def test_anomaly_stream_lines_are_flagged():
    path = os.path.join(ROOT, "commands", "anomalies.jsonl")
    accepted = 0
    rejected = 0
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                parse_command(json.loads(line))
                accepted += 1
            except CommandError:
                rejected += 1
    # six lines fail structural parsing; the remaining four fail at apply time
    assert rejected == 6
    assert accepted + rejected == 10


def test_runtime_rejection_does_not_corrupt_state(tmp_path, config_factory,
                                                  audio_dir):
    cfg = config_factory()
    snapshot_dir = str(tmp_path / "snap-reject")
    engine = WorkbenchEngine(cfg, scan_wav_files(audio_dir), snapshot_dir)
    engine.initialize()
    from voiceworkbench.commands import EditCommand
    # unknown file -> runtime CommandError, state and cursor stay consistent
    bad_cut = EditCommand(
        seq=1, command_type="cut_interval",
        params={"file_name": "nope.wav", "start_ms": 0.0, "end_ms": 100.0},
    )
    response = engine.handle_command(bad_cut)
    assert response.accepted is False
    assert response.error["code"] == "E_COMMAND"
    assert len(engine.rejected_commands) == 1
    assert engine.command_cursor == 0  # no pipeline work was committed
