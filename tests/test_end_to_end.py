from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

from voice_repair.audio import read_wav_interval, wav_bytes, wav_mean
from voice_repair.kernels import (
    apply_normalization,
    declick_full,
    denoise_full,
    normalization_gain,
    remove_dc,
)
from voice_repair.ledger import Ledger
from voice_repair.samples import generate_demo_dataset
from voice_repair.task import load_task
from voice_repair.verify import run_verification

from .helpers import (
    recording_entry,
    run_cli,
    stage_config,
    write_task,
    write_test_wav,
)


def _fresh_task(tmp_path: Path) -> Path:
    return generate_demo_dataset(tmp_path / "data", force=True)


def _run_batch(task_path: Path, state: Path, workers: int = 3):
    return run_cli(
        "run",
        "--task",
        str(task_path),
        "--ledger",
        str(state / "ledger.sqlite"),
        "--out-dir",
        str(state / "output"),
        "--report",
        str(state / "report.json"),
        "--state-dir",
        str(state),
        "--workers",
        str(workers),
    )


def test_end_to_end_matches_serial_reference_and_obeys_budgets(tmp_path: Path) -> None:
    task_path = _fresh_task(tmp_path)
    state = tmp_path / "state"
    completed = _run_batch(task_path, state)
    assert completed.returncode == 0, completed.stderr
    report = json.loads((state / "report.json").read_text())
    assert report["summary"]["completed_blocks"] == report["summary"]["total_blocks"]
    assert report["summary"]["deferred_blocks"] > 0

    reasons = {
        deferred["reason"]
        for round_record in report["scheduling"]["rounds"]
        for deferred in round_record["deferred"]
    }
    assert "memory_budget" in reasons
    assert "round_time_budget" in reasons
    for round_record in report["scheduling"]["rounds"]:
        assert len(round_record["admitted"]) <= report["run"]["parallelism"]
        assert round_record["logical_cost_ns"] <= report["budgets"]["round_time_budget_ns"]
        assert (
            sum(item["memory_bytes"] for item in round_record["admitted"])
            <= report["budgets"]["memory_budget_bytes"]
        )

    validation = load_task(task_path)
    assert validation.task is not None
    ledger = Ledger(state / "ledger.sqlite")
    for recording in validation.task.recordings:
        raw = read_wav_interval(recording.wav_path, 0, recording.sample_count)
        mean = wav_mean(recording.wav_path)
        dc_values = remove_dc(raw, recording.dc, mean)
        declicked = declick_full(dc_values, recording.declick)
        denoised = denoise_full(declicked, recording.denoise)
        gain = normalization_gain(denoised, recording.normalize)
        expected_stages = [
            dc_values,
            declicked,
            denoised,
            apply_normalization(denoised, gain),
        ]
        for stage, expected in enumerate(expected_stages):
            actual = np.concatenate(
                [
                    ledger.fetch_block(recording.recording_id, stage, block_index)
                    for block_index in range(len(recording.block_ranges(stage)))
                ]
            )
            assert np.array_equal(actual, expected)

        output_path = state / "output" / f"{recording.recording_id}.wav"
        expected_pcm = wav_bytes(apply_normalization(denoised, gain), recording.sample_rate)
        assert output_path.read_bytes() == expected_pcm
    ledger.close()

    rerun = _run_batch(task_path, state)
    assert rerun.returncode == 0, rerun.stderr
    rerun_report = json.loads((state / "report.json").read_text())
    assert all(item["output_changed"] is False for item in rerun_report["recordings"])
    assert rerun_report["summary"]["completed_blocks"] == report["summary"]["total_blocks"]
    assert all(len(item["stages"]) == 4 for item in rerun_report["recordings"])


def test_serial_and_parallel_verification_is_exact(tmp_path: Path) -> None:
    task_path = _fresh_task(tmp_path)
    validation = load_task(task_path)
    assert validation.task is not None
    result = run_verification(
        validation=validation,
        work_dir=tmp_path / "verification",
        workers=3,
    )
    assert result["ok"] is True
    assert all(result["checks"].values())
    assert result["max_abs_sample_delta"] == 0.0
    assert result["compared_outputs"] == len(validation.task.recordings)


def test_different_stage_block_sizes_still_match_serial_reference(tmp_path: Path) -> None:
    rng = np.random.default_rng(99)
    values = rng.normal(0.0, 0.15, 9_003)
    values[[700, 4_500, 8_200]] += np.array([0.7, -0.8, 0.6])
    write_test_wav(tmp_path / "mixed.wav", values, 16000)
    stages = stage_config()
    stages["dc"]["block_frames"] = 1_000
    stages["declick"]["block_frames"] = 777
    stages["denoise"]["block_frames"] = 2_048
    stages["normalize"]["block_frames"] = 1_500
    task_path = write_task(
        tmp_path / "mixed.json",
        [{"id": "mixed", "wav": "mixed.wav", "stages": stages}],
        parallelism=2,
        round_time_budget_ns=2_000_000,
        memory_budget_bytes=4_000_000,
    )
    state = tmp_path / "mixed_state"
    result = _run_batch(task_path, state, workers=2)
    assert result.returncode == 0, result.stderr

    validation = load_task(task_path)
    assert validation.task is not None
    recording = validation.task.recordings[0]
    raw = read_wav_interval(recording.wav_path, 0, recording.sample_count)
    dc_values = remove_dc(raw, recording.dc, wav_mean(recording.wav_path))
    declicked = declick_full(dc_values, recording.declick)
    denoised = denoise_full(declicked, recording.denoise)
    gain = normalization_gain(denoised, recording.normalize)
    expected = wav_bytes(apply_normalization(denoised, gain), recording.sample_rate)
    assert (state / "output" / "mixed.wav").read_bytes() == expected

    report = json.loads((state / "report.json").read_text())
    block_counts = [stage["total_blocks"] for stage in report["recordings"][0]["stages"]]
    assert block_counts == [10, 12, 5, 7]


def test_invalid_entry_does_not_block_valid_recording(tmp_path: Path) -> None:
    write_test_wav(tmp_path / "ok.wav", np.linspace(-0.2, 0.2, 4_000), 16000)
    task_path = write_task(
        tmp_path / "partial.json",
        [
            recording_entry("ok", "ok.wav", block_frames=1_024),
            recording_entry("missing", "missing.wav"),
        ],
        round_time_budget_ns=2_000_000,
        memory_budget_bytes=2_000_000,
    )
    state = tmp_path / "partial_state"
    result = _run_batch(task_path, state, workers=2)
    assert result.returncode == 2
    report = json.loads((state / "report.json").read_text())
    assert report["status"] == "completed_with_errors"
    assert any(
        issue["recording_id"] == "missing" and issue["code"] == "E_FILE_MISSING"
        for issue in report["business_codes"]
    )
    assert (state / "output" / "ok.wav").is_file()
    assert not (state / "output" / "missing.wav").exists()


def test_unsatisfiable_memory_budget_is_reported(tmp_path: Path) -> None:
    write_test_wav(tmp_path / "tiny.wav", np.linspace(-0.1, 0.1, 2_000), 16000)
    task_path = write_task(
        tmp_path / "tiny.json",
        [recording_entry("tiny", "tiny.wav", block_frames=512)],
        memory_budget_bytes=1,
    )
    state = tmp_path / "tiny_state"
    result = _run_batch(task_path, state, workers=1)
    assert result.returncode == 3
    report = json.loads((state / "report.json").read_text())
    assert report["status"] == "unschedulable"
    assert any(
        issue["code"] == "E_MEMORY_BUDGET_UNSATISFIABLE"
        for issue in report["business_codes"]
    )


def test_forced_termination_resumes_only_missing_blocks(tmp_path: Path) -> None:
    task_path = _fresh_task(tmp_path)
    baseline_state = tmp_path / "baseline"
    baseline = _run_batch(task_path, baseline_state)
    assert baseline.returncode == 0, baseline.stderr
    baseline_report = json.loads((baseline_state / "report.json").read_text())
    baseline_outputs = {
        item["recording_id"]: item["output_sha256"]
        for item in baseline_report["recordings"]
    }

    crash_state = tmp_path / "crash"
    crashed = run_cli(
        "run",
        "--task",
        str(task_path),
        "--ledger",
        str(crash_state / "ledger.sqlite"),
        "--out-dir",
        str(crash_state / "output"),
        "--report",
        str(crash_state / "report.json"),
        "--state-dir",
        str(crash_state),
        "--workers",
        "3",
        "--crash-after-commits",
        "10",
    )
    assert crashed.returncode == 137, crashed.stderr
    assert not (crash_state / "report.json").exists()

    connection = sqlite3.connect(crash_state / "ledger.sqlite")
    rows = connection.execute(
        """
        SELECT recording_id, stage, block_index, start_sample, end_sample,
               sample_count, content_sha256, payload
        FROM block_ledger
        ORDER BY recording_id, stage, block_index
        """
    ).fetchall()
    assert 10 <= len(rows) <= 13
    for row in rows:
        payload = bytes(row[7])
        assert hashlib_sha256(payload) == row[6]
        assert len(payload) == row[5] * 8
        assert row[4] - row[3] == row[5]
    prior = {
        (row[0], row[1], row[2]): row[6]
        for row in rows
    }
    connection.close()

    resumed = _run_batch(task_path, crash_state)
    assert resumed.returncode == 0, resumed.stderr
    resumed_report = json.loads((crash_state / "report.json").read_text())
    assert resumed_report["run"]["is_resume"] is True
    assert sum(
        stage["resume_repaired_blocks"]
        for recording in resumed_report["recordings"]
        for stage in recording["stages"]
    ) > 0
    assert {
        item["recording_id"]: item["output_sha256"]
        for item in resumed_report["recordings"]
    } == baseline_outputs

    connection = sqlite3.connect(crash_state / "ledger.sqlite")
    after = {
        (row[0], row[1], row[2]): row[3]
        for row in connection.execute(
            "SELECT recording_id, stage, block_index, content_sha256 FROM block_ledger"
        ).fetchall()
    }
    connection.close()
    assert all(after[key] == digest for key, digest in prior.items())
    assert len(after) == resumed_report["summary"]["total_blocks"]


def hashlib_sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()
