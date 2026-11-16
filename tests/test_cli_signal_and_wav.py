from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from asr_editor.samples import generate_samples

ROOT = Path(__file__).parents[1]


def _copy_sample(sample_dir: Path, destination: Path) -> tuple[Path, Path, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    for suffix in (".v1.json", ".v2.srt", ".tokens.meta.json"):
        shutil.copy2(
            sample_dir / f"sample01{suffix}", destination / f"sample01{suffix}"
        )
    return (
        destination / "sample01.v1.json",
        destination / "sample01.v2.srt",
        destination / "sample01.tokens.meta.json",
    )


def _run(
    args: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "asr_editor", *args]
    merged_env = os.environ.copy()
    merged_env["PYTHONPATH"] = str(ROOT)
    if env:
        merged_env.update(env)
    return subprocess.run(
        command,
        cwd=ROOT,
        env=merged_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_generated_wav_is_deterministic_16k_mono_pcm(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_samples(first)
    generate_samples(second)
    for name in ("sample01.wav", "sample02.wav", "sample03.wav"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    import soundfile as sf

    info = sf.info(first / "sample01.wav")
    assert info.samplerate == 16000
    assert info.channels == 1
    assert info.subtype == "PCM_16"


def test_cli_wav_verification(sample_dir: Path) -> None:
    result = _run(["verify-wav", "--wav", str(sample_dir / "sample01.wav")])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["valid_16k_mono_pcm"] is True


def test_cli_continues_after_operation_failure(
    sample_dir: Path, tmp_path: Path
) -> None:
    v1, v2, sidecar = _copy_sample(sample_dir, tmp_path / "work")
    ops_path = tmp_path / "ops.json"
    ops_path.write_text(
        json.dumps(
            [
                {"op": "shift", "start_ms": 0, "end_ms": 1000, "delta_ms": -10},
                {
                    "op": "replace",
                    "utterance_id": "utt-003",
                    "token_index": 1,
                    "text": "总结",
                },
            ]
        ),
        encoding="utf-8",
    )
    result = _run(
        [
            "edit",
            "--v1",
            str(v1),
            "--v2",
            str(v2),
            "--sidecar",
            str(sidecar),
            "--state-dir",
            str(tmp_path / "state"),
            "--ops",
            str(ops_path),
        ]
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["snapshot_version"] == 1
    assert payload["last_completed_operation"] == 2
    assert any(item["code"] == "E_TIME_OVERFLOW" for item in payload["failure_list"])
    assert "总结" in v1.read_text(encoding="utf-8")


def test_resume_rejects_a_different_operation_plan(
    sample_dir: Path, tmp_path: Path
) -> None:
    v1, v2, sidecar = _copy_sample(sample_dir, tmp_path / "work")
    state_dir = tmp_path / "state"
    first_ops = tmp_path / "first.json"
    first_ops.write_text(
        json.dumps(
            [
                {
                    "op": "replace",
                    "utterance_id": "utt-003",
                    "token_index": 1,
                    "text": "总结",
                }
            ]
        ),
        encoding="utf-8",
    )
    first = _run(
        [
            "edit",
            "--v1",
            str(v1),
            "--v2",
            str(v2),
            "--sidecar",
            str(sidecar),
            "--state-dir",
            str(state_dir),
            "--ops",
            str(first_ops),
        ]
    )
    assert first.returncode == 0
    before = v1.read_text(encoding="utf-8")
    second_ops = tmp_path / "second.json"
    second_ops.write_text(
        json.dumps(
            [
                {
                    "op": "replace",
                    "utterance_id": "utt-003",
                    "token_index": 2,
                    "text": "错误",
                }
            ]
        ),
        encoding="utf-8",
    )
    second = _run(
        [
            "edit",
            "--v1",
            str(v1),
            "--v2",
            str(v2),
            "--sidecar",
            str(sidecar),
            "--state-dir",
            str(state_dir),
            "--ops",
            str(second_ops),
        ]
    )
    assert second.returncode == 1
    payload = json.loads(second.stdout)
    assert any(item["code"] == "E_PLAN_MISMATCH" for item in payload["failure_list"])
    assert payload["operations_applied_this_run"] == 0
    assert v1.read_text(encoding="utf-8") == before


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal delivery test")
def test_sigint_completes_snapshot_and_resume_skips_completed(
    sample_dir: Path, tmp_path: Path
) -> None:
    v1, v2, sidecar = _copy_sample(sample_dir, tmp_path / "work")
    state_dir = tmp_path / "state"
    init = _run(
        [
            "init",
            "--v1",
            str(v1),
            "--v2",
            str(v2),
            "--sidecar",
            str(sidecar),
            "--state-dir",
            str(state_dir),
        ]
    )
    assert init.returncode == 0, init.stderr
    operations = [
        {
            "op": "replace",
            "utterance_id": "utt-003",
            "token_index": 1,
            "text": f"文本{index}",
        }
        for index in range(1, 13)
    ]
    ops_path = tmp_path / "ops.json"
    ops_path.write_text(json.dumps(operations), encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["ASR_EDITOR_TEST_OP_DELAY_MS"] = "250"
    command = [
        sys.executable,
        "-m",
        "asr_editor",
        "edit",
        "--v1",
        str(v1),
        "--v2",
        str(v2),
        "--sidecar",
        str(sidecar),
        "--state-dir",
        str(state_dir),
        "--ops",
        str(ops_path),
    ]
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    progress_path = state_dir / "progress.json"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if progress_path.exists():
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            if progress.get("snapshot_version", 0) >= 1:
                break
        time.sleep(0.03)
    process.send_signal(signal.SIGINT)
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 0, stderr
    payload = json.loads(stdout)
    assert payload["graceful_signal"] in {"SIGINT", "SIGTERM"}
    completed = payload["last_completed_operation"]
    assert 1 <= completed < len(operations)
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["last_completed_operation"] == completed
    assert progress["snapshot_version"] == completed

    resumed = _run(
        [
            "edit",
            "--v1",
            str(v1),
            "--v2",
            str(v2),
            "--sidecar",
            str(sidecar),
            "--state-dir",
            str(state_dir),
            "--ops",
            str(ops_path),
        ]
    )
    assert resumed.returncode == 0, resumed.stderr
    resumed_payload = json.loads(resumed.stdout)
    assert resumed_payload["resumed_after_operation"] == completed
    assert resumed_payload["last_completed_operation"] == len(operations)


@pytest.mark.skipif(os.name != "posix", reason="forced-kill recovery test uses SIGKILL")
def test_forced_kill_recovers_only_from_complete_snapshot(
    sample_dir: Path, tmp_path: Path
) -> None:
    v1, v2, sidecar = _copy_sample(sample_dir, tmp_path / "work")
    state_dir = tmp_path / "state"
    init = _run(
        [
            "init",
            "--v1",
            str(v1),
            "--v2",
            str(v2),
            "--sidecar",
            str(sidecar),
            "--state-dir",
            str(state_dir),
        ]
    )
    assert init.returncode == 0
    operations = [
        {
            "op": "replace",
            "utterance_id": "utt-003",
            "token_index": 1,
            "text": f"强杀{index}",
        }
        for index in range(1, 9)
    ]
    ops_path = tmp_path / "ops.json"
    ops_path.write_text(json.dumps(operations), encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["ASR_EDITOR_TEST_OP_DELAY_MS"] = "2000"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "asr_editor",
            "edit",
            "--v1",
            str(v1),
            "--v2",
            str(v2),
            "--sidecar",
            str(sidecar),
            "--state-dir",
            str(state_dir),
            "--ops",
            str(ops_path),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    progress_path = state_dir / "progress.json"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if progress_path.exists():
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            if progress.get("snapshot_version", 0) >= 1:
                break
        time.sleep(0.03)
    process.kill()
    process.wait(timeout=10)
    recovered = _run(
        [
            "recover",
            "--v1",
            str(v1),
            "--v2",
            str(v2),
            "--sidecar",
            str(sidecar),
            "--state-dir",
            str(state_dir),
        ]
    )
    assert recovered.returncode == 0, recovered.stderr
    payload = json.loads(recovered.stdout)
    assert payload["snapshot_version"] >= 1
    assert payload["failure_list"] == []
