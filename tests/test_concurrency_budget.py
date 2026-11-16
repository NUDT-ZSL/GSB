"""Concurrency ordering, exception isolation and interaction budget tests."""
from __future__ import annotations

from voiceworkbench.audio import scan_wav_files
from voiceworkbench.commands import load_commands
from voiceworkbench.config import build_config
from voiceworkbench.concurrency import Task, run_tasks
from voiceworkbench.engine import WorkbenchEngine
from voiceworkbench.features import extract_all, extract_file
from voiceworkbench.clustering import assign_segments, agglomerative
from voiceworkbench.vad import segment_all
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_threadpool_preserves_sort_order():
    tasks = [
        Task(sort_key=[index], identifier=f"id{index}", stage="test",
             payload={"value": index})
        for index in range(50)
    ]

    def worker(task):
        return task.payload["value"] * 2

    outcome = run_tasks(tasks, worker, max_workers=8)
    assert outcome.results == [index * 2 for index in range(50)]
    assert outcome.failures == []


def test_exception_isolation_keeps_other_results():
    tasks = [Task(sort_key=[i], identifier=f"id{i}", stage="test", payload={"i": i})
             for i in range(10)]

    def worker(task):
        if task.payload["i"] == 4:
            raise ValueError("boom")
        return task.payload["i"]

    outcome = run_tasks(tasks, worker, max_workers=4)
    assert outcome.results == [i for i in range(10) if i != 4]
    assert len(outcome.failures) == 1
    failure = outcome.failures[0]
    assert failure.identifier == "id4"
    assert failure.stage == "test"
    assert failure.error_type == "ValueError"
    assert failure.sort_key == [4]


def test_frame_failure_is_recorded_and_order_kept(config_factory, audio_dir):
    cfg = config_factory()
    path = scan_wav_files(audio_dir)[0]
    features, failures = extract_file(path, cfg, {"frame_fail": 10})
    assert len(failures) == 1
    assert failures[0].stage == "features"
    # surviving frames remain in strict index order
    indices = [frame.frame_index for frame in features.frames]
    assert indices == sorted(indices)
    assert 10 not in indices


def test_assignment_failure_is_isolated(config_factory, audio_dir):
    cfg = config_factory()
    features, _ = extract_all(scan_wav_files(audio_dir), cfg)
    segments = segment_all(features, cfg)
    clusters = agglomerative(segments, cfg.cluster_distance_threshold)
    target = segments[0].segment_id
    assignments, failures = assign_segments(
        segments, clusters, cfg, {"assign_fail": target},
    )
    assert len(failures) == 1
    assert failures[0].identifier == f"{target}#assign"
    assert len(assignments) == len(segments) - 1
    ids = [a.segment_id for a in assignments]
    assert ids == sorted(ids)


def test_single_vs_multi_worker_identical(config_factory, audio_dir):
    cfg1 = build_config({**config_factory().to_dict(), "max_workers": 1})
    cfg8 = build_config({**config_factory().to_dict(), "max_workers": 8})
    paths = scan_wav_files(audio_dir)
    features1, fail1 = extract_all(paths, cfg1)
    features8, fail8 = extract_all(paths, cfg8)
    assert [f.to_dict() for f in features1] == [f.to_dict() for f in features8]
    assert fail1 == fail8 == []


def test_budget_pending_marks_but_completes(tmp_path, config_factory,
                                            audio_dir, commands_path):
    cfg = config_factory(interact_budget_ms=0.001)
    commands = load_commands(commands_path)
    engine = WorkbenchEngine(cfg, scan_wav_files(audio_dir),
                             str(tmp_path / "snap-budget"))
    engine.initialize()
    responses = [engine.handle_command(command) for command in commands]
    # with a near-zero budget every substantive command is flagged pending,
    # but all work still completes deterministically
    assert all(response.accepted for response in responses)
    assert any(response.pending for response in responses)
    pending = [r for r in responses if r.pending]
    assert all(r.pending_code == "E_INTERACT_BUDGET_PENDING" for r in pending)
    assert all(r.in_budget is False for r in pending)
    # final result is still fully populated
    assert len(engine.latest_result.clusters) >= 1
    assert len(engine.latest_result.assignments) == len(engine.latest_result.segments)


def test_budget_pending_never_aborts(tmp_path, config_factory, audio_dir,
                                     commands_path):
    # slow_ms guarantees the deadline is crossed mid-pipeline; the engine must
    # still finish rather than dropping work
    cfg = config_factory(interact_budget_ms=1.0)
    commands = load_commands(commands_path)
    engine = WorkbenchEngine(
        cfg, scan_wav_files(audio_dir), str(tmp_path / "snap-slow"),
        slow_ms=2.0,
    )
    engine.initialize()
    response = engine.handle_command(commands[0])
    assert response.pending is True
    assert response.pending_code == "E_INTERACT_BUDGET_PENDING"
    # a follow-up command sees a complete prior state
    follow_up = engine.handle_command(commands[1])
    assert follow_up.accepted is True
    assert engine.command_cursor == 2
