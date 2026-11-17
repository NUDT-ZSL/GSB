from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from tests.conftest import copy_demo, make_tiny_input, write_jsonl
from tests.helpers import assert_outputs_equal, read_outputs, render


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "tts_workbench", "render", *args],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )


def test_forced_exit_resumes_from_atomic_snapshot(tmp_path: Path) -> None:
    sample = copy_demo(tmp_path / "recovery_input", with_commands=True)
    crashed_work = tmp_path / "crashed_work"
    crashed = _run_cli(
        "--input",
        str(sample),
        "--work",
        str(crashed_work),
        "--crash-after-checkpoints",
        "2",
    )
    assert crashed.returncode == 137, crashed.stderr
    assert (crashed_work / "state.json").exists()
    assert not (crashed_work / "report.json").exists()
    snapshot = json.loads((crashed_work / "state.json").read_text(encoding="utf-8"))
    assert snapshot["stage"] == "phoneme_features"
    assert snapshot["completed_phonemes"]
    assert snapshot["substate_positions"]

    resumed = _run_cli("--input", str(sample), "--work", str(crashed_work))
    assert resumed.returncode == 0, resumed.stderr
    report = json.loads((crashed_work / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "complete"
    assert report["snapshot_stage"] == "phoneme_features"
    assert report["recovery_point"]["source"] == "snapshot"
    assert report["recovery_point"]["next_substate"] in {"VOICING_DECISION", "FRAME_CONCAT"}

    full_work = tmp_path / "full_work"
    full = render(sample, full_work)
    assert full.status == "complete"
    assert_outputs_equal(read_outputs(crashed_work), read_outputs(full_work))
    final_state = json.loads((crashed_work / "state.json").read_text(encoding="utf-8"))
    assert final_state["stage"] == "complete"
    assert not list(crashed_work.glob("*.tmp*"))
    assert not list((crashed_work / "output").glob("*.tmp*"))


def test_sigterm_finishes_current_phoneme_and_exits_zero(tmp_path: Path) -> None:
    sample = make_tiny_input(
        tmp_path / "signal_input",
        phones=["AH"],
        durations=[1000],
        f0_hz=[130.0],
        energy=[0.7],
        noise_floor=[0.01],
        voicing_score=[0.9],
        config_updates={
            "spectral_bands": 8,
            "concurrency": 1,
            "snapshot_interval_frames": 200,
        },
    )
    work = tmp_path / "signal_work"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tts_workbench",
            "render",
            "--input",
            str(sample),
            "--work",
            str(work),
            "--quiet",
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    state_path = work / "state.json"
    deadline = time.time() + 20
    while time.time() < deadline:
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(0.005)
                continue
            if state.get("stage") == "load_validate":
                break
        time.sleep(0.005)
    else:
        process.kill()
        raise AssertionError("workbench did not create an initial snapshot")

    time.sleep(0.05)
    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=30)
    assert process.returncode == 0, (stdout, stderr)
    report = json.loads((work / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "interrupted"
    assert report["recovery_point"]["interrupted_stage"] in {
        "load_validate",
        "phoneme_features",
    }
    interrupted_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert interrupted_state["stage"] in {"load_validate", "phoneme_features"}

    resumed = render(sample, work)
    assert resumed.status == "complete"
    full = render(sample, tmp_path / "signal_full_work")
    assert full.status == "complete"
    assert_outputs_equal(read_outputs(work), read_outputs(tmp_path / "signal_full_work"))


def test_edit_then_forced_exit_then_edit_matches_fresh_full_render(tmp_path: Path) -> None:
    sample = copy_demo(tmp_path / "combo_input", with_commands=False)
    work = tmp_path / "combo_work"
    assert render(sample, work).status == "complete"

    seq1 = tmp_path / "combo_seq1.jsonl"
    write_jsonl(
        seq1,
        [
            {
                "seq": 1,
                "utterance_id": "utt_alpha",
                "phoneme_index": 0,
                "op": "set_energy_scale",
                "value": 1.15,
            }
        ],
    )
    crashed = _run_cli(
        "--input",
        str(sample),
        "--work",
        str(work),
        "--commands",
        str(seq1),
        "--crash-after-checkpoints",
        "2",
    )
    assert crashed.returncode == 137, crashed.stderr

    resumed = _run_cli(
        "--input",
        str(sample),
        "--work",
        str(work),
        "--commands",
        str(seq1),
    )
    assert resumed.returncode == 0, resumed.stderr

    seq2 = tmp_path / "combo_seq2.jsonl"
    write_jsonl(
        seq2,
        [
            {
                "seq": 2,
                "utterance_id": "utt_beta",
                "phoneme_index": 1,
                "op": "set_duration",
                "duration_frames": 20,
            }
        ],
    )
    final = _run_cli(
        "--input",
        str(sample),
        "--work",
        str(work),
        "--commands",
        str(seq2),
    )
    assert final.returncode == 0, final.stderr

    fresh_input = copy_demo(tmp_path / "combo_fresh_input", with_commands=False)
    write_jsonl(
        fresh_input / "edit_commands.jsonl",
        [
            {
                "seq": 1,
                "utterance_id": "utt_alpha",
                "phoneme_index": 0,
                "op": "set_energy_scale",
                "value": 1.15,
            },
            {
                "seq": 2,
                "utterance_id": "utt_beta",
                "phoneme_index": 1,
                "op": "set_duration",
                "duration_frames": 20,
            },
        ],
    )
    fresh = render(fresh_input, tmp_path / "combo_fresh_work")
    assert fresh.status == "complete"
    assert_outputs_equal(read_outputs(work), read_outputs(tmp_path / "combo_fresh_work"))
