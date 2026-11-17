from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def base_task(output_dir: str = "out") -> dict[str, Any]:
    return {
        "schema_version": "hdr-color-task-v1",
        "task_id": "unit-test",
        "frames_path": "frames.json",
        "output_dir": output_dir,
        "source": {
            "transfer": "PQ",
            "gamut": "BT2020",
            "white_point": "D65",
        },
        "target": {
            "transfer": "SDR_GAMMA",
            "gamut": "BT709",
            "white_point": "D65",
        },
        "luminance": {
            "reference_white_nits": 100.0,
            "source_peak_nits": 1000.0,
            "target_peak_nits": 100.0,
        },
        "tone_mapping": {
            "curve": "reinhard_extended",
            "white_nits": 1000.0,
            "exposure": 1.0,
        },
        "statistics": {
            "window_frames": 3,
            "tail_mode": "include",
        },
        "processing": {
            "chunk_frames": 2,
            "concurrency": 2,
            "memory_budget_bytes": 16777216,
            "curve_lut_size": 4097,
        },
        "injections": [],
    }


def frame_value(index: int, pixels: int = 4) -> list[list[float]]:
    if index == 0:
        return [[0.0, 0.0, 0.0] for _ in range(pixels)]
    value = 0.25 + index
    return [
        [value * (0.4 + 0.1 * pixel), value, value * 0.7]
        for pixel in range(pixels)
    ]


def write_case(
    directory: Path,
    task: dict[str, Any],
    frames: list[list[list[float]]] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    frames_path = directory / "frames.json"
    if frames is not None:
        payload = {
            "schema_version": "hdr-color-frames-v1",
            "frames": frames,
        }
        frames_path.write_text(
            json.dumps(payload, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
    task_path = directory / "task.json"
    task_path.write_text(
        json.dumps(task, ensure_ascii=False, allow_nan=False, indent=2),
        encoding="utf-8",
    )
    return task_path
