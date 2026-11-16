"""Subprocess execution of stage scripts and deterministic output summaries."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

from .snapshot import normalize_stderr


@dataclass
class AttemptOutcome:
    exit_code: Optional[int]
    timed_out: bool
    duration_ms: int
    start_mono_ms: int
    end_mono_ms: int
    stderr_norm: str
    reason: str


@dataclass
class OutputSummary:
    output_path: str
    sha256: str
    byte_size: int
    record_count: int


def hash_output_file(path: str) -> OutputSummary:
    """Byte-stable SHA-256 plus JSONL record count of a stage output."""
    digest = hashlib.sha256()
    record_count = 0
    with open(path, "rb") as handle:
        for line in handle:
            digest.update(line)
            if line.strip():
                record_count += 1
    byte_size = os.path.getsize(path)
    return OutputSummary(
        output_path=os.path.abspath(path),
        sha256=digest.hexdigest(),
        byte_size=byte_size,
        record_count=record_count,
    )


def build_stage_env(
    stage_id: str,
    output_path: str,
    input_map: Dict[str, str],
    missing_upstreams: List[str],
    attempt_no: int,
    failure_config: Optional[Dict[str, int]] = None,
    script_dir: Optional[str] = None,
) -> Dict[str, str]:
    env = dict(os.environ)
    env["PIPE_STAGE_ID"] = stage_id
    env["PIPE_OUTPUT_PATH"] = os.path.abspath(output_path)
    env["PIPE_INPUT_MAP"] = json.dumps(input_map, sort_keys=True)
    env["PIPE_MISSING_UPSTREAMS"] = json.dumps(sorted(missing_upstreams))
    env["PIPE_ATTEMPT_NO"] = str(attempt_no)
    env["PIPE_WORKSPACE_ROOT"] = os.getcwd()
    env["PYTHONIOENCODING"] = "utf-8"
    if script_dir:
        script_dir = os.path.abspath(script_dir)
        env["PYTHONPATH"] = script_dir + os.pathsep + env.get("PYTHONPATH", "")
    if failure_config:
        env["PIPE_FAIL_FIRST_N"] = json.dumps(failure_config, sort_keys=True)
    return env


class SubprocessStageRunner:
    def __init__(self, python_executable: Optional[str] = None):
        self.python_executable = python_executable or sys.executable

    def run_attempt(
        self,
        script_abs: str,
        env: Dict[str, str],
        timeout_ms: Optional[int],
        clock,
    ) -> AttemptOutcome:
        start = clock.mono_ms()
        try:
            completed = subprocess.run(
                [self.python_executable, script_abs],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=(timeout_ms / 1000.0) if timeout_ms else None,
                check=False,
            )
            end = clock.mono_ms()
            stderr_norm = normalize_stderr(completed.stderr or "")
            if completed.returncode == 0:
                reason = "exit_zero"
            else:
                reason = f"non_zero_exit_{completed.returncode}"
            return AttemptOutcome(
                exit_code=completed.returncode,
                timed_out=False,
                duration_ms=max(0, end - start),
                start_mono_ms=start,
                end_mono_ms=end,
                stderr_norm=stderr_norm,
                reason=reason,
            )
        except subprocess.TimeoutExpired as exc:
            end = clock.mono_ms()
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return AttemptOutcome(
                exit_code=None,
                timed_out=True,
                duration_ms=max(0, end - start),
                start_mono_ms=start,
                end_mono_ms=end,
                stderr_norm=normalize_stderr(stderr),
                reason=f"timeout_after_{timeout_ms}_ms",
            )
        except OSError as exc:
            end = clock.mono_ms()
            return AttemptOutcome(
                exit_code=None,
                timed_out=False,
                duration_ms=max(0, end - start),
                start_mono_ms=start,
                end_mono_ms=end,
                stderr_norm=normalize_stderr(str(exc)),
                reason="spawn_error",
            )
