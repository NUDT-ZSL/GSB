from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSCODER = REPO_ROOT / "transcoder.py"


def tool_paths() -> tuple[Path, Path]:
    ffmpeg_value = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    ffprobe_value = os.environ.get("FFPROBE") or shutil.which("ffprobe")
    ffmpeg = Path(ffmpeg_value) if ffmpeg_value else REPO_ROOT / ".tools" / "ffmpeg"
    ffprobe = Path(ffprobe_value) if ffprobe_value else REPO_ROOT / ".tools" / "ffprobe"
    if not ffmpeg.is_file():
        raise unittest_skip("ffmpeg unavailable")
    if not ffprobe.is_file():
        raise unittest_skip("ffprobe unavailable")
    return ffmpeg, ffprobe


def unittest_skip(reason: str) -> None:
    import unittest

    raise unittest.SkipTest(reason)


def write_wav(
    path: Path,
    samples: np.ndarray,
    sample_rate: int,
    bit_depth: int = 16,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = np.asarray(samples, dtype=np.float64)
    if values.ndim == 1:
        values = values.reshape(-1, 1)
    if bit_depth == 16:
        payload = np.rint(np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        width = 2
    elif bit_depth == 24:
        whole = np.rint(np.clip(values, -1.0, 1.0) * 8388607.0).astype("<i4")
        payload = whole.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
        width = 3
    elif bit_depth == 32:
        payload = np.rint(np.clip(values, -1.0, 1.0) * 2147483647.0).astype("<i4").tobytes()
        width = 4
    else:
        raise ValueError(bit_depth)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(values.shape[1])
        handle.setsampwidth(width)
        handle.setframerate(sample_rate)
        handle.writeframes(payload)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        frames = handle.getnframes()
        payload = handle.readframes(frames)
    if width == 2:
        values = np.frombuffer(payload, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 3:
        raw = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 3)
        whole = (
            raw[:, 0].astype(np.int32)
            | (raw[:, 1].astype(np.int32) << 8)
            | (raw[:, 2].astype(np.int32) << 16)
        )
        whole = np.where(whole & 0x800000, whole - 0x1000000, whole)
        values = whole.astype(np.float64) / 8388608.0
    elif width == 4:
        values = np.frombuffer(payload, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise ValueError(width)
    return values.reshape(-1, channels), sample_rate


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def run_cli(
    manifest: Path,
    *extra: str,
    timeout: float = 90.0,
) -> subprocess.CompletedProcess[str]:
    ffmpeg, ffprobe = tool_paths()
    command = [
        sys.executable,
        str(TRANSCODER),
        str(manifest),
        "--ffmpeg",
        str(ffmpeg),
        "--ffprobe",
        str(ffprobe),
        *extra,
    ]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PATH"] = f"{ffmpeg.parent}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def cli_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(f"no JSON stdout; stderr={result.stderr!r}")
    return json.loads(lines[-1])


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 15.0,
    interval: float = 0.02,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition was not reached before timeout")
