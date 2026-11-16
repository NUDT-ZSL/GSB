#!/usr/bin/env python3
"""Run an end-to-end local acceptance workflow and write evidence artifacts."""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr_editor.codes import (
    E_CROSS_FORMAT_MISMATCH,
    E_FORMAT_INVALID,
    E_TIME_OVERFLOW,
    E_TOKEN_TIME_INFERRED,
)
from asr_editor.diagnostics import build_cross_format_report, write_report
from asr_editor.formats import atomic_write_json, transcript_fingerprint
from asr_editor.interop import upgrade_v2
from asr_editor.operations import OperationError, apply_operation
from asr_editor.parsers import parse_v1_file, parse_v2_file
from asr_editor.samples import generate_samples
from asr_editor.workspace import Workspace

OUT = ROOT / "artifacts" / "acceptance"
SAMPLES = ROOT / "samples"
OPS = [
    {"op": "merge", "utterance_id": "utt-001", "other_utterance_id": "utt-002"},
    {"op": "split", "utterance_id": "utt-001", "token_index": 3},
    {"op": "shift", "start_ms": 1500, "end_ms": 2900, "delta_ms": 250},
    {"op": "replace", "utterance_id": "utt-003", "token_index": 1, "text": "总结"},
]


def run_cli(
    args: list[str], *, env_extra: dict[str, str] | None = None, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "asr_editor", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def copy_sample(name: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for suffix in (".wav", ".v1.json", ".v2.srt", ".tokens.meta.json"):
        shutil.copy2(SAMPLES / f"{name}{suffix}", destination / f"{name}{suffix}")


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, Any]] = []

    def check(name: str, condition: bool, details: Any = None) -> None:
        checks.append({"name": name, "passed": bool(condition), "details": details})
        if not condition:
            raise AssertionError(f"acceptance check failed: {name}: {details}")

    manifest = generate_samples(SAMPLES)
    check(
        "deterministic sample corpus generated",
        len(manifest["samples"]) == 3,
        {"samples": len(manifest["samples"])},
    )

    import soundfile as sf

    wav_info = {}
    for name in ("sample01", "sample02", "sample03"):
        info = sf.info(SAMPLES / f"{name}.wav")
        wav_info[name] = {
            "samplerate": info.samplerate,
            "channels": info.channels,
            "subtype": info.subtype,
            "frames": info.frames,
        }
        check(
            f"{name} is 16kHz mono PCM_16",
            info.samplerate == 16000
            and info.channels == 1
            and info.subtype == "PCM_16",
            wav_info[name],
        )

    initial_v1 = parse_v1_file(SAMPLES / "sample01.v1.json")
    initial_v2 = parse_v2_file(SAMPLES / "sample01.v2.srt")
    check(
        "v1/v2 parse without failures",
        not initial_v1.failures and not initial_v2.failures,
    )
    clean_report = build_cross_format_report(
        initial_v1.transcript,
        initial_v2.transcript,
        precision_v1=initial_v1.precision,
        precision_v2=initial_v2.precision,
    )
    check(
        "baseline cross-format consistency",
        clean_report["summary"]["mismatch_utterance_count"] == 0,
    )

    work = OUT / "work" / "sample01"
    copy_sample("sample01", work)
    v1 = work / "sample01.v1.json"
    v2 = work / "sample01.v2.srt"
    sidecar = work / "sample01.tokens.meta.json"
    state_dir = OUT / "state"
    workspace = Workspace.open(v1, v2, sidecar, state_dir, create_initial_snapshot=True)
    for index, operation in enumerate(OPS, start=1):
        workspace.apply_operation(operation, index)
    check(
        "four edit operations completed",
        workspace.version == 4,
        {"snapshot_version": workspace.version},
    )

    try:
        apply_operation(
            workspace.transcript,
            {"op": "shift", "start_ms": 0, "end_ms": 1000, "delta_ms": -10},
        )
        overflow_code = None
    except OperationError as exc:
        overflow_code = exc.code
    check(
        "negative timeline shift emits E_TIME_OVERFLOW",
        overflow_code == E_TIME_OVERFLOW,
        {"code": overflow_code},
    )

    expected = initial_v1.transcript.normalized_copy()
    for operation in OPS:
        expected = apply_operation(expected, operation).transcript
    check(
        "snapshot IR equals sequential replay",
        transcript_fingerprint(expected)
        == transcript_fingerprint(workspace.transcript),
        {
            "snapshot_fingerprint": transcript_fingerprint(workspace.transcript),
            "sequential_fingerprint": transcript_fingerprint(expected),
        },
    )

    edited_v1 = parse_v1_file(v1)
    edited_v2 = parse_v2_file(v2)
    edited_report = build_cross_format_report(
        edited_v1.transcript,
        edited_v2.transcript,
        snapshot_version=workspace.version,
        precision_v1=edited_v1.precision,
        precision_v2=edited_v2.precision,
    )
    write_report(OUT / "diagnostics-sample01-edited.json", edited_report)
    check(
        "edited v1/v2 remain equivalent",
        edited_report["summary"]["mismatch_utterance_count"] == 0
        and not edited_v1.failures
        and not edited_v2.failures,
        edited_report["summary"],
    )

    upgraded = upgrade_v2(edited_v2.transcript, sidecar)
    check(
        "sidecar upgrade restores word timestamps",
        upgraded.sidecar_used and upgraded.inferred_time_count == 0,
        {"reason": upgraded.reason, "inferred": upgraded.inferred_time_count},
    )
    expected_tokens = [
        token.to_dict()
        for utterance in workspace.transcript.utterances
        for token in utterance.tokens
    ]
    upgraded_tokens = [
        token.to_dict()
        for utterance in upgraded.transcript.utterances
        for token in utterance.tokens
    ]
    token_signature = lambda items: [
        (item["text"], item["start_ms"], item["end_ms"], item["inferred"])
        for item in items
    ]
    check(
        "sidecar upgrade token times exact",
        token_signature(expected_tokens) == token_signature(upgraded_tokens),
    )

    inferred = upgrade_v2(edited_v2.transcript, None)
    check(
        "no-sidecar upgrade marks inferred timing",
        not inferred.sidecar_used
        and inferred.inferred_time_count == edited_v2.transcript.token_count
        and E_TOKEN_TIME_INFERRED in inferred.diagnostic_codes,
        {
            "inferred_time_count": inferred.inferred_time_count,
            "codes": inferred.diagnostic_codes,
        },
    )

    expected_snapshot = workspace.transcript.to_dict()
    state_path = state_dir / "workspace.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["workspace_version"] += 100
    state_path.write_text(json.dumps(state), encoding="utf-8")
    recovered = Workspace.open(v1, v2, sidecar, state_dir)
    check(
        "startup rollback restores latest complete snapshot",
        recovered.recovery.restored
        and recovered.transcript.to_dict() == expected_snapshot,
        recovered.recovery.to_dict(),
    )

    mismatch_v1 = parse_v1_file(SAMPLES / "sample02.v1.json")
    mismatch_v2 = parse_v2_file(SAMPLES / "sample02.v2.srt")
    mismatch_report = build_cross_format_report(
        mismatch_v1.transcript,
        mismatch_v2.transcript,
        precision_v1=mismatch_v1.precision,
        precision_v2=mismatch_v2.precision,
    )
    write_report(OUT / "diagnostics-sample02-mismatch.json", mismatch_report)
    check(
        "cross-format mismatch report is produced",
        mismatch_report["summary"]["mismatch_utterance_count"] == 2
        and E_CROSS_FORMAT_MISMATCH in mismatch_report["business_codes"],
        mismatch_report["summary"],
    )

    fixtures = OUT / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    bad_v1 = fixtures / "invalid.v1.json"
    bad_v1.write_text(
        "{\n"
        '  "version": 1,\n'
        '  "utterances": [\n'
        '    {"id":"ok1","start":0,"end":1,"text":"ok","tokens":[{"text":"ok","start":0,"end":1}]},\n'
        '    {"id":"bad","start":1,"end":2,"text":"bad","tokens":[{"text":"bad","start":2,"end":1}]},\n'
        '    {"id":"ok2","start":2,"end":3,"text":"ok2","tokens":[{"text":"ok2","start":2,"end":3}]}\n'
        "  ]\n"
        "}\n",
        encoding="utf-8",
    )
    bad_srt = fixtures / "invalid.v2.srt"
    bad_srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nok\n\n"
        "2\n00:00:02,000 --> 00:00:01,000\nreversed\n\n"
        "3\n00:00:03,000 --> 00:00:04,000\nok2\n",
        encoding="utf-8",
    )
    bad_v1_result = parse_v1_file(bad_v1)
    bad_v2_result = parse_v2_file(bad_srt)
    failure_list = [
        failure.to_dict() for failure in bad_v1_result.failures + bad_v2_result.failures
    ]
    atomic_write_json(OUT / "failure-list.json", failure_list)
    check(
        "invalid entries fail without stopping valid neighbors",
        len(bad_v1_result.transcript.utterances) == 2
        and len(bad_v2_result.transcript.utterances) == 2
        and all(item["code"] == E_FORMAT_INVALID for item in failure_list),
        failure_list,
    )
    inspect_cli = run_cli(["inspect", "--v1", str(bad_v1), "--v2", str(bad_srt)])
    check(
        "failure list causes integer exit code 1",
        inspect_cli.returncode == 1,
        {"exit_code": inspect_cli.returncode},
    )

    signal_result: dict[str, Any] | None = None
    forced_kill_result: dict[str, Any] | None = None
    if os.name == "posix":
        signal_work = OUT / "signal"
        copy_sample("sample01", signal_work)
        signal_state = OUT / "signal-state"
        init = run_cli(
            [
                "init",
                "--v1",
                str(signal_work / "sample01.v1.json"),
                "--v2",
                str(signal_work / "sample01.v2.srt"),
                "--sidecar",
                str(signal_work / "sample01.tokens.meta.json"),
                "--state-dir",
                str(signal_state),
            ]
        )
        check("signal fixture init succeeds", init.returncode == 0, init.stdout[-500:])
        signal_ops = [
            {
                "op": "replace",
                "utterance_id": "utt-003",
                "token_index": 1,
                "text": f"信号{index}",
            }
            for index in range(1, 9)
        ]
        ops_path = signal_work / "ops.json"
        atomic_write_json(ops_path, signal_ops)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        env["ASR_EDITOR_TEST_OP_DELAY_MS"] = "220"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "asr_editor",
                "edit",
                "--v1",
                str(signal_work / "sample01.v1.json"),
                "--v2",
                str(signal_work / "sample01.v2.srt"),
                "--sidecar",
                str(signal_work / "sample01.tokens.meta.json"),
                "--state-dir",
                str(signal_state),
                "--ops",
                str(ops_path),
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        progress_path = signal_state / "progress.json"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if progress_path.exists():
                progress = json.loads(progress_path.read_text(encoding="utf-8"))
                if progress.get("snapshot_version", 0) >= 1:
                    break
            time.sleep(0.03)
        process.send_signal(signal.SIGINT)
        stdout, _stderr = process.communicate(timeout=20)
        signal_payload = json.loads(stdout)
        signal_result = {
            "exit_code": process.returncode,
            "graceful_signal": signal_payload.get("graceful_signal"),
            "last_completed_operation": signal_payload.get("last_completed_operation"),
            "snapshot_version": signal_payload.get("snapshot_version"),
        }
        check(
            "SIGINT completes snapshot/progress and exits 0",
            process.returncode == 0
            and signal_payload.get("graceful_signal") == "SIGINT"
            and signal_payload.get("snapshot_version")
            == signal_payload.get("last_completed_operation"),
            signal_result,
        )
        resumed = run_cli(
            [
                "edit",
                "--v1",
                str(signal_work / "sample01.v1.json"),
                "--v2",
                str(signal_work / "sample01.v2.srt"),
                "--sidecar",
                str(signal_work / "sample01.tokens.meta.json"),
                "--state-dir",
                str(signal_state),
                "--ops",
                str(ops_path),
            ]
        )
        resumed_payload = json.loads(resumed.stdout)
        check(
            "resume starts after last completed operation",
            resumed.returncode == 0
            and resumed_payload.get("resumed_after_operation")
            == signal_result["last_completed_operation"]
            and resumed_payload.get("last_completed_operation") == len(signal_ops),
            {
                "exit_code": resumed.returncode,
                "resumed_after": resumed_payload.get("resumed_after_operation"),
                "last_completed": resumed_payload.get("last_completed_operation"),
            },
        )

        kill_work = OUT / "forced-kill"
        copy_sample("sample01", kill_work)
        kill_state = OUT / "forced-kill-state"
        init = run_cli(
            [
                "init",
                "--v1",
                str(kill_work / "sample01.v1.json"),
                "--v2",
                str(kill_work / "sample01.v2.srt"),
                "--sidecar",
                str(kill_work / "sample01.tokens.meta.json"),
                "--state-dir",
                str(kill_state),
            ]
        )
        check("forced-kill fixture init succeeds", init.returncode == 0)
        kill_ops = [
            {
                "op": "replace",
                "utterance_id": "utt-003",
                "token_index": 1,
                "text": f"强杀{index}",
            }
            for index in range(1, 7)
        ]
        kill_ops_path = kill_work / "ops.json"
        atomic_write_json(kill_ops_path, kill_ops)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        env["ASR_EDITOR_TEST_OP_DELAY_MS"] = "2000"
        kill_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "asr_editor",
                "edit",
                "--v1",
                str(kill_work / "sample01.v1.json"),
                "--v2",
                str(kill_work / "sample01.v2.srt"),
                "--sidecar",
                str(kill_work / "sample01.tokens.meta.json"),
                "--state-dir",
                str(kill_state),
                "--ops",
                str(kill_ops_path),
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        kill_progress_path = kill_state / "progress.json"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if kill_progress_path.exists():
                progress = json.loads(kill_progress_path.read_text(encoding="utf-8"))
                if progress.get("snapshot_version", 0) >= 1:
                    break
            time.sleep(0.03)
        kill_process.kill()
        kill_process.wait(timeout=10)
        recover = run_cli(
            [
                "recover",
                "--v1",
                str(kill_work / "sample01.v1.json"),
                "--v2",
                str(kill_work / "sample01.v2.srt"),
                "--sidecar",
                str(kill_work / "sample01.tokens.meta.json"),
                "--state-dir",
                str(kill_state),
            ]
        )
        recover_payload = json.loads(recover.stdout)
        forced_kill_result = {
            "exit_code": recover.returncode,
            "recovered_snapshot_version": recover_payload.get("snapshot_version"),
            "failure_count": len(recover_payload.get("failure_list", [])),
        }
        check(
            "forced kill restarts from complete snapshot only",
            recover.returncode == 0
            and recover_payload.get("snapshot_version", 0) >= 1
            and not recover_payload.get("failure_list"),
            forced_kill_result,
        )

    business_codes = list(
        dict.fromkeys(
            clean_report["business_codes"]
            + edited_report["business_codes"]
            + mismatch_report["business_codes"]
            + [item["code"] for item in failure_list]
            + [E_TIME_OVERFLOW]
        )
    )
    summary = {
        "status": "passed" if all(item["passed"] for item in checks) else "failed",
        "python": sys.version,
        "platform": platform.platform(),
        "working_directory": str(ROOT),
        "samples": manifest,
        "wav_checks": wav_info,
        "checks": checks,
        "business_codes_observed": business_codes,
        "snapshot_versions": [
            item["snapshot_version"] for item in workspace.operation_log
        ],
        "signal_result": signal_result,
        "forced_kill_result": forced_kill_result,
        "artifacts": {
            "edited_v1": str(v1),
            "edited_v2": str(v2),
            "sidecar": str(sidecar),
            "state_dir": str(state_dir),
            "diagnostics_clean": str(OUT / "diagnostics-sample01-edited.json"),
            "diagnostics_mismatch": str(OUT / "diagnostics-sample02-mismatch.json"),
            "failure_list": str(OUT / "failure-list.json"),
        },
    }
    atomic_write_json(OUT / "acceptance-summary.json", summary)
    print(
        json.dumps(
            {
                "status": summary["status"],
                "checks": len(checks),
                "business_codes": business_codes,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
