from __future__ import annotations

import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .engine import PipelineEngine
from .errors import BusinessError
from .reports import write_json_report
from .stages import STAGE_NAMES
from .util import sha256_file
from .workspace import Workspace


def _read_worker_lines(
    process: subprocess.Popen[str],
    line_queue: "queue.Queue[str]",
) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        line_queue.put(line.rstrip("\r\n"))


def _wait_for_marker(
    process: subprocess.Popen[str],
    line_queue: "queue.Queue[str]",
    marker_prefix: str,
    timeout: float,
) -> tuple[str, list[str]]:
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            line = line_queue.get(timeout=min(0.25, remaining))
        except queue.Empty:
            if process.poll() is not None:
                while True:
                    try:
                        lines.append(line_queue.get_nowait())
                    except queue.Empty:
                        break
                raise RuntimeError(
                    "worker exited before commit marker: "
                    + " | ".join(lines[-5:])
                )
            continue
        lines.append(line)
        if line.startswith(marker_prefix):
            return line, lines
    process.kill()
    process.wait(timeout=10)
    raise RuntimeError(
        "timed out waiting for worker marker: " + " | ".join(lines[-5:])
    )


def _prepare_demo_workspace(workspace: Workspace) -> None:
    workspace.ensure_layout()
    demo_dir = workspace.root / "_demo"
    for name in ("full", "recovery"):
        target = demo_dir / name
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)


def _copy_inputs(source: Workspace, target: Workspace) -> None:
    target.ensure_layout()
    for path in source.input_paths():
        target_path = target.data_dir / path.name
        target_path.write_bytes(path.read_bytes())


def _export_bytes(workspace: Workspace, run_result: Any) -> bytes:
    if not run_result.final_export:
        raise RuntimeError("pipeline did not produce an export")
    path = workspace.root / run_result.final_export["path"]
    return path.read_bytes()


def _run_worker(
    recovery: Workspace,
    kill_stage: str,
    mode: str,
    project_root: Path,
) -> dict[str, Any]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(project_root)
        if not existing
        else str(project_root) + os.pathsep + existing
    )
    command = [
        sys.executable,
        "-m",
        "sft_pipeline",
        "worker",
        "--root",
        str(recovery.root),
        "--pause-after-stage",
        kill_stage,
    ]
    process = subprocess.Popen(
        command,
        cwd=str(project_root),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    line_queue: "queue.Queue[str]" = queue.Queue()
    reader = threading.Thread(
        target=_read_worker_lines, args=(process, line_queue), daemon=True
    )
    reader.start()
    marker, lines = _wait_for_marker(
        process, line_queue, f"AFTER_COMMIT:{kill_stage}", timeout=45.0
    )
    termination: dict[str, Any] = {
        "mode": mode,
        "marker": marker,
        "lines_before_signal": lines,
    }
    if mode == "kill":
        process.kill()
        return_code = process.wait(timeout=15)
        termination["signal"] = "SIGKILL/TerminateProcess"
    elif mode == "term":
        if os.name == "nt":
            recovery.stop_request_path.write_text("stop\n", encoding="ascii")
            termination["signal"] = "stop.request"
        else:
            process.send_signal(signal.SIGTERM)
            termination["signal"] = "SIGTERM"
        try:
            return_code = process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait(timeout=10)
            termination["forced_after_timeout"] = True
    else:
        process.kill()
        process.wait(timeout=10)
        raise ValueError(f"unknown demo mode: {mode}")
    termination["returncode"] = return_code
    if process.stderr is not None:
        termination["stderr"] = process.stderr.read()
    if reader.is_alive():
        reader.join(timeout=2)
    while True:
        try:
            lines.append(line_queue.get_nowait())
        except queue.Empty:
            break
    termination["lines_after_signal"] = lines
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()
    if mode == "term" and return_code != 0:
        raise RuntimeError(
            f"graceful worker exited with {return_code}: "
            + " | ".join(lines[-8:])
        )
    if mode == "kill" and return_code == 0:
        raise RuntimeError("forced-kill worker unexpectedly exited cleanly")
    return termination


def run_recovery_demo(
    workspace: Workspace,
    *,
    kill_stage: str = "quality",
    mode: str = "kill",
) -> dict[str, Any]:
    if kill_stage not in STAGE_NAMES:
        raise ValueError(f"unknown stage: {kill_stage}")
    if mode not in {"kill", "term"}:
        raise ValueError("mode must be 'kill' or 'term'")
    if not workspace.inputs_exist():
        raise BusinessError(
            "E_INPUT_INVALID",
            "workspace inputs do not exist; run init first",
            field="data",
        )
    _prepare_demo_workspace(workspace)
    project_root = Path(__file__).resolve().parents[1]
    demo_dir = workspace.root / "_demo"
    full = Workspace(demo_dir / "full")
    recovery = Workspace(demo_dir / "recovery")
    _copy_inputs(workspace, full)
    _copy_inputs(workspace, recovery)

    full_engine = PipelineEngine(full)
    full_run = full_engine.run()
    full_check = full_engine.self_check()
    full_bytes = _export_bytes(full, full_run)
    full_checksum = sha256_file(full.root / full_run.final_export["path"])

    termination = _run_worker(recovery, kill_stage, mode, project_root)
    recovery_engine = PipelineEngine(recovery)
    resume_run = recovery_engine.run()
    recovery_bytes = _export_bytes(recovery, resume_run)
    recovery_checksum = sha256_file(
        recovery.root / resume_run.final_export["path"]
    )
    recovery_check = recovery_engine.self_check()

    full_versions = {
        stage: full_engine.store.load_manifest()["stages"][stage]["version"]
        for stage in STAGE_NAMES
    }
    recovery_versions = {
        stage: recovery_engine.store.load_manifest()["stages"][stage]["version"]
        for stage in STAGE_NAMES
    }
    match = full_bytes == recovery_bytes
    report = {
        "ok": match and full_check["ok"] and recovery_check["ok"],
        "mode": mode,
        "kill_stage": kill_stage,
        "full": {
            "export_path": full_run.final_export["path"],
            "export_bytes_checksum": full_checksum,
            "content_checksum": full_run.final_export["content_checksum"],
            "record_count": full_run.final_export["record_count"],
            "versions": full_versions,
            "self_check_ok": full_check["ok"],
        },
        "forced_termination": termination,
        "recovery": {
            "export_path": resume_run.final_export["path"],
            "export_bytes_checksum": recovery_checksum,
            "content_checksum": resume_run.final_export["content_checksum"],
            "record_count": resume_run.final_export["record_count"],
            "versions": recovery_versions,
            "run": {
                "mode": resume_run.mode,
                "started_with_stages": resume_run.started_with_stages,
                "recomputed_stages": resume_run.recomputed_stages,
                "reused_snapshot_stages": resume_run.reused_snapshot_stages,
                "completed": resume_run.completed,
            },
            "self_check_ok": recovery_check["ok"],
        },
        "byte_identical": match,
        "version_sequences_identical": full_versions == recovery_versions,
    }
    if not match:
        differing_at = next(
            (
                index
                for index, (left, right) in enumerate(zip(full_bytes, recovery_bytes))
                if left != right
            ),
            min(len(full_bytes), len(recovery_bytes)),
        )
        report["first_difference_offset"] = differing_at
    write_json_report(workspace.demo_report_path, report)
    return report
