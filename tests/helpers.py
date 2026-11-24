from __future__ import annotations

import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

from voice_repair.audio import float64_to_pcm16


ROOT = Path(__file__).resolve().parents[1]


def stage_config(block_frames: int = 4096) -> dict:
    return {
        "dc": {"block_frames": block_frames, "mode": "mean"},
        "declick": {
            "block_frames": block_frames,
            "threshold": 0.19,
            "radius": 2,
        },
        "denoise": {
            "block_frames": block_frames,
            "frame_size": 256,
            "hop_size": 64,
            "noise_frames": 6,
            "oversubtraction": 1.25,
            "floor_ratio": 0.03,
        },
        "normalize": {"block_frames": block_frames, "target_peak": 0.9},
    }


def write_test_wav(
    path: Path,
    values: np.ndarray,
    sample_rate: int = 16000,
    *,
    channels: int = 1,
    sample_width: int = 2,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        if sample_width == 2:
            pcm = float64_to_pcm16(values)
            if channels > 1:
                frame = np.frombuffer(pcm, dtype="<i2")
                pcm = np.repeat(frame, channels).astype("<i2").tobytes()
        elif sample_width == 1:
            pcm = np.clip((values + 1.0) * 127.5, 0, 255).astype(np.uint8).tobytes()
            if channels > 1:
                pcm = bytes(byte for byte in pcm for _ in range(channels))
        else:
            raise ValueError("test helper supports 8-bit and 16-bit WAVs")
        wav.writeframes(pcm)


def write_task(path: Path, recordings: list[dict], **overrides: object) -> Path:
    payload = {
        "schema_version": 1,
        "task_id": "test_task",
        "seed": 7,
        "parallelism": 3,
        "round_time_budget_ns": 150_000,
        "memory_budget_bytes": 400_000,
        "recordings": recordings,
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def recording_entry(recording_id: str, wav_name: str, *, block_frames: int = 4096) -> dict:
    return {
        "id": recording_id,
        "wav": wav_name,
        "stages": stage_config(block_frames),
    }


def cli_env() -> dict[str, str]:
    env = os.environ.copy()
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + current if current else "")
    return env


def run_cli(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "voice_repair", *args],
        cwd=ROOT,
        env=cli_env(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
