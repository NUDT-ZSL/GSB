from __future__ import annotations

import json
import subprocess
import sys

from acoustic_alignment.engine import MeasurementEngine, StopController, compute_input_digest
from acoustic_alignment.ledger import LedgerStore
from acoustic_alignment.task_model import load_task


def test_stop_at_band_boundary_leaves_no_complete_track_then_resumes_identically(
    sample_task_path, tmp_path
):
    task = load_task(sample_task_path)
    ledger_path = tmp_path / "alignment_ledger.sqlite"
    digest = compute_input_digest(task)

    stop = StopController()

    def stop_after_first_reference_band(track_id, band_id, band_index):
        if track_id == task.reference_track_id and band_index == 0:
            stop.request("test_band_boundary")

    interrupted_ledger = LedgerStore(ledger_path)
    engine = MeasurementEngine(
        task=task,
        ledger=interrupted_ledger,
        stop=stop,
        band_callback=stop_after_first_reference_band,
    )
    outcome = engine.run(digest)
    assert outcome.interrupted is not None
    assert outcome.interrupted.stage == "band"
    assert interrupted_ledger.has_complete_track(digest, task.reference_track_id) is False

    resumed_ledger = LedgerStore(ledger_path)
    resumed = MeasurementEngine(task=task, ledger=resumed_ledger)
    resumed_outcome = resumed.run(digest)
    assert resumed_outcome.interrupted is None
    assert resumed_outcome.first_error is None
    assert len(resumed_ledger.current_input_results(digest)) == len(task.tracks)

    oneshot_ledger = LedgerStore(tmp_path / "oneshot.sqlite")
    MeasurementEngine(task=task, ledger=oneshot_ledger).run(digest)
    assert resumed_ledger.ledger_digest == oneshot_ledger.ledger_digest
    assert resumed_ledger.canonical_bytes() == oneshot_ledger.canonical_bytes()


def test_stop_before_run_is_track_boundary(sample_task_path, tmp_path):
    task = load_task(sample_task_path)
    digest = compute_input_digest(task)
    ledger = LedgerStore(tmp_path / "ledger.sqlite")
    stop = StopController()
    stop.request("test_track_boundary")
    outcome = MeasurementEngine(task, ledger, stop=stop).run(digest)
    assert outcome.interrupted is not None
    assert outcome.interrupted.stage == "track"
    assert ledger.current_input_results(digest) == []


def test_hard_exit_after_commit_is_recovered_by_cli(tmp_path):
    from acoustic_alignment.sample_generator import generate_sample_set

    sample_dir = tmp_path / "samples"
    _root, task_path = generate_sample_set(sample_dir, seed=381)
    interrupted_output = tmp_path / "interrupted"
    oneshot_output = tmp_path / "oneshot"

    fault = subprocess.run(
        [
            sys.executable,
            "-m",
            "acoustic_alignment.cli",
            "run",
            "-t",
            str(task_path),
            "-o",
            str(interrupted_output),
            "--fault-exit-after-commit",
            "2",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert fault.returncode == 9
    interrupted_ledger = LedgerStore(interrupted_output / "alignment_ledger.sqlite")
    assert len(interrupted_ledger.results) >= 1

    resume = subprocess.run(
        [
            sys.executable,
            "-m",
            "acoustic_alignment.cli",
            "run",
            "-t",
            str(task_path),
            "-o",
            str(interrupted_output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert resume.returncode == 0, resume.stdout + resume.stderr

    oneshot = subprocess.run(
        [
            sys.executable,
            "-m",
            "acoustic_alignment.cli",
            "run",
            "-t",
            str(task_path),
            "-o",
            str(oneshot_output),
            "--force",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert oneshot.returncode == 0, oneshot.stdout + oneshot.stderr

    recovered = LedgerStore(interrupted_output / "alignment_ledger.sqlite")
    expected = LedgerStore(oneshot_output / "alignment_ledger.sqlite")
    assert recovered.ledger_digest == expected.ledger_digest
    assert recovered.canonical_bytes() == expected.canonical_bytes()


def test_memory_budget_serializes_track_admission_in_track_id_order(
    sample_task_path, tmp_path, monkeypatch
):
    import acoustic_alignment.engine as engine_module

    task = load_task(sample_task_path)
    task.concurrency = 4
    task.memory_budget_bytes = 9_000_000
    digest = compute_input_digest(task)
    ledger = LedgerStore(tmp_path / "ledger.sqlite")
    started: list[str] = []

    original_measure = engine_module.measure_prepared

    def recording_measure(prepared, index, task_spec, reference, stop, band_callback):
        started.append(prepared.track.track_id)
        return original_measure(prepared, index, task_spec, reference, stop, band_callback)

    monkeypatch.setattr(engine_module, "estimate_for_track", lambda track: 8_000_000)
    monkeypatch.setattr(engine_module, "measure_prepared", recording_measure)
    engine = MeasurementEngine(task=task, ledger=ledger)
    outcome = engine.run(digest)
    assert outcome.first_error is None
    assert started[0] == task.reference_track_id
    candidate_started = started[1:]
    assert candidate_started == sorted(candidate_started)
    assert engine.accountant.peak_reserved <= task.memory_budget_bytes


def test_unexpected_track_exception_returns_first_error_and_keeps_reference(
    sample_task_path, tmp_path, monkeypatch
):
    import acoustic_alignment.engine as engine_module

    task = load_task(sample_task_path)
    task.concurrency = 2
    digest = compute_input_digest(task)
    ledger = LedgerStore(tmp_path / "ledger.sqlite")
    original = engine_module.measure_prepared

    def flaky(prepared, index, task_spec, reference, stop, callback):
        if prepared.track.track_id == "cand_delay3":
            raise RuntimeError("synthetic unexpected failure")
        return original(prepared, index, task_spec, reference, stop, callback)

    monkeypatch.setattr(engine_module, "measure_prepared", flaky)
    outcome = MeasurementEngine(task=task, ledger=ledger).run(digest)
    assert outcome.first_error is not None
    assert "synthetic unexpected failure" in outcome.first_error["message"]
    assert ledger.has_complete_track(digest, task.reference_track_id)


def test_digest_tampering_is_recovered_from_valid_backup(sample_task_path, tmp_path):
    task = load_task(sample_task_path)
    digest = compute_input_digest(task)
    ledger_path = tmp_path / "alignment_ledger.sqlite"
    report_path = tmp_path / "alignment_report.json"
    ledger = LedgerStore(ledger_path)
    MeasurementEngine(task=task, ledger=ledger).run(digest)

    from acoustic_alignment.reporting import commit_report, reconcile_existing_report

    commit_report(report_path, ledger, digest, task)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["track_count"] = 1
    report_path.write_text(json.dumps(report), encoding="utf-8")

    import sqlite3

    connection = sqlite3.connect(ledger_path)
    connection.execute("UPDATE meta SET value='tampered' WHERE key='ledger_digest'")
    connection.commit()
    connection.close()

    recovered_ledger = LedgerStore(ledger_path)
    _rebuilt, recovery = reconcile_existing_report(report_path, recovered_ledger, digest, task)
    assert recovery is not None
    assert recovery["code"] == "E_DIGEST"
    assert recovery["restored_valid_ledger_backup"] is True
    rebuilt = json.loads(report_path.read_text(encoding="utf-8"))
    assert rebuilt["track_count"] == len(task.tracks)
    assert rebuilt["digest_recovery"]["code"] == "E_DIGEST"
