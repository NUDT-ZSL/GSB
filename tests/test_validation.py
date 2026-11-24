from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from voice_repair.task import load_task

from .helpers import recording_entry, write_task, write_test_wav


def _tone(count: int = 1024) -> np.ndarray:
    return 0.2 * np.sin(np.linspace(0.0, 20.0, count, dtype=np.float64))


def test_invalid_entries_are_isolated_and_keep_valid_recording(tmp_path: Path) -> None:
    write_test_wav(tmp_path / "valid.wav", _tone(), 16000)
    write_test_wav(tmp_path / "bad_rate.wav", _tone(), 12345)
    write_test_wav(tmp_path / "bad_depth.wav", _tone(), 16000, sample_width=1)
    write_test_wav(tmp_path / "bad_channels.wav", _tone(), 16000, channels=2)

    task_path = write_task(
        tmp_path / "task.json",
        [
            recording_entry("valid", "valid.wav"),
            recording_entry("bad_rate", "bad_rate.wav"),
            recording_entry("bad_depth", "bad_depth.wav"),
            recording_entry("bad_channels", "bad_channels.wav"),
            recording_entry("missing", "missing.wav"),
        ],
    )
    result = load_task(task_path)
    assert result.task is not None
    assert [record.recording_id for record in result.task.recordings] == ["valid"]
    codes = {(issue.recording_id, issue.code) for issue in result.issues}
    assert ("bad_rate", "E_SAMPLE_RATE") in codes
    assert ("bad_depth", "E_BIT_DEPTH") in codes
    assert ("bad_channels", "E_CHANNELS") in codes
    assert ("missing", "E_FILE_MISSING") in codes


def test_nonfinite_and_out_of_domain_parameters_have_distinct_codes(tmp_path: Path) -> None:
    write_test_wav(tmp_path / "valid.wav", _tone(), 16000)
    recording = recording_entry("bad_param", "valid.wav")
    recording["stages"]["declick"]["threshold"] = float("nan")
    missing_stage = recording_entry("missing_stage", "valid.wav")
    del missing_stage["stages"]["normalize"]
    task_path = write_task(
        tmp_path / "task.json",
        [
            recording_entry("valid", "valid.wav"),
            recording,
            missing_stage,
        ],
    )
    result = load_task(task_path)
    assert result.task is not None
    assert len(result.task.recordings) == 1
    codes = {(issue.recording_id, issue.code) for issue in result.issues}
    assert ("bad_param", "E_PARAM_FINITE") in codes
    assert ("missing_stage", "E_STAGE_MISSING") in codes


def test_duplicate_ids_are_reported_and_excluded(tmp_path: Path) -> None:
    write_test_wav(tmp_path / "a.wav", _tone(), 16000)
    write_test_wav(tmp_path / "b.wav", _tone(), 16000)
    task_path = write_task(
        tmp_path / "task.json",
        [
            recording_entry("dup", "a.wav"),
            recording_entry("dup", "b.wav"),
        ],
    )
    result = load_task(task_path)
    assert result.task is not None
    assert result.task.recordings == ()
    assert any(issue.code == "E_ID_DUPLICATE" for issue in result.issues)


def test_json_nan_is_rejected_as_business_code(tmp_path: Path) -> None:
    write_test_wav(tmp_path / "a.wav", _tone(), 16000)
    task = {
        "schema_version": 1,
        "task_id": "nan_task",
        "seed": 1,
        "parallelism": 1,
        "round_time_budget_ns": 100000,
        "memory_budget_bytes": 100000,
        "recordings": [recording_entry("a", "a.wav")],
    }
    task["recordings"][0]["stages"]["normalize"]["target_peak"] = float("nan")
    path = tmp_path / "nan.json"
    path.write_text(json.dumps(task), encoding="utf-8")
    result = load_task(path)
    assert result.task is not None
    assert any(issue.code == "E_PARAM_FINITE" for issue in result.issues)
