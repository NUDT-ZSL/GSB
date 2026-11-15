#!/usr/bin/env python3
"""Test driver that pauses after a real state transition for signal tests."""

from __future__ import annotations

import sys
import time
import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from offline_audio import engine
from offline_audio.cli import main
from offline_audio.state import StateStore


def main_driver() -> int:
    if len(sys.argv) < 4:
        raise SystemExit("usage: interrupt_driver.py MANIFEST MODE PAUSE_SECONDS")
    manifest = str(Path(sys.argv[1]).resolve())
    mode = sys.argv[2]
    pause_seconds = float(sys.argv[3])
    slept = {"value": False}

    original_set_stage = StateStore.set_stage
    original_add_completed = StateStore.add_completed_track

    def paused_set_stage(self: StateStore, stage: str, **kwargs: object) -> None:
        original_set_stage(self, stage, **kwargs)
        if not slept["value"] and mode == "loading" and stage == "loading":
            slept["value"] = True
            time.sleep(pause_seconds)

    def paused_add_completed(self: StateStore, entry: dict[str, object]) -> None:
        original_add_completed(self, entry)
        if not slept["value"] and mode == "completed":
            slept["value"] = True
            time.sleep(pause_seconds)

    StateStore.set_stage = paused_set_stage
    StateStore.add_completed_track = paused_add_completed
    ffmpeg = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or str(
        REPO_ROOT / ".tools" / "ffmpeg"
    )
    ffprobe = os.environ.get("FFPROBE") or shutil.which("ffprobe") or str(
        REPO_ROOT / ".tools" / "ffprobe"
    )
    return main(
        [
            manifest,
            "--ffmpeg",
            ffmpeg,
            "--ffprobe",
            ffprobe,
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main_driver())
