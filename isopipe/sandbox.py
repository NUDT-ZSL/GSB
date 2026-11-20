from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PipelineError, error


CHILD_SCRIPT = Path(__file__).with_name("child.py")


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    envelope: dict[str, Any] | None
    timed_out: bool = False


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def execute_in_sandbox(
    *,
    root: Path,
    task: dict[str, Any],
    input_paths: list[Path],
    read_scopes: list[Path],
    write_scopes: list[Path],
    workdir: Path,
    output_path: Path,
    staging_path: Path,
    envelope_path: Path,
    environment: dict[str, str],
    fixed_scale: int,
    kernel_version: str,
    timeout_ms: int,
) -> SandboxResult:
    workdir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in (staging_path, envelope_path):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    payload = {
        "task": task,
        "root": str(root),
        "input_paths": [str(path) for path in input_paths],
        "read_scopes": [str(path) for path in read_scopes],
        "write_scopes": [str(path) for path in write_scopes],
        "workdir": str(workdir),
        "output_path": str(output_path),
        "staging_path": str(staging_path),
        "envelope_path": str(envelope_path),
        "environment": environment,
        "fixed_scale": fixed_scale,
        "kernel_version": kernel_version,
    }
    command = [sys.executable, "-I", str(CHILD_SCRIPT)]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(workdir),
        env=environment,
        start_new_session=(os.name == "posix"),
    )
    timed_out = False
    stdout = b""
    stderr = b""
    try:
        stdout, stderr = process.communicate(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            timeout=max(0.001, timeout_ms / 1000.0),
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        stdout, stderr = process.communicate()
    envelope: dict[str, Any] | None = None
    try:
        with envelope_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            envelope = loaded
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        envelope = None
    return SandboxResult(
        ok=(not timed_out and process.returncode == 0 and bool(envelope and envelope.get("ok"))),
        returncode=process.returncode,
        stdout=_decode(stdout),
        stderr=_decode(stderr),
        envelope=envelope,
        timed_out=timed_out,
    )


def sandbox_failure(result: SandboxResult, *, task_name: str) -> PipelineError:
    if result.timed_out:
        return error(
            "E_TASK_TIMEOUT",
            "task exceeded its execution timeout",
            task=task_name,
            returncode=result.returncode,
        )
    envelope = result.envelope or {}
    code = envelope.get("error_code")
    if code in ("E_SANDBOX_READ_DENIED", "E_SANDBOX_WRITE_DENIED"):
        return error(
            code,
            envelope.get("message", "sandbox denied filesystem access"),
            task=task_name,
            path=envelope.get("path"),
            returncode=result.returncode,
        )
    if code:
        return error(
            code,
            str(envelope.get("message", "sandbox task failed")),
            task=task_name,
            returncode=result.returncode,
        )
    return error(
        "E_INTERNAL",
        "sandbox child failed without a structured error",
        task=task_name,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )

