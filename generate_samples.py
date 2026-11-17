"""Generate deterministic local frame data and task descriptions.

No network or external media is used.  Run:
    python generate_samples.py --output samples
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
from typing import Any


TASK_SCHEMA = "hdr-color-task-v1"
FRAMES_SCHEMA = "hdr-color-frames-v1"


def generate_frames(seed: int, frame_count: int = 17, width: int = 32) -> list[list[list[float]]]:
    rng = random.Random(seed)
    frames: list[list[list[float]]] = []
    for frame_index in range(frame_count):
        if frame_index == 0:
            pixels = [[0.0, 0.0, 0.0] for _ in range(width)]
        elif frame_index == 1:
            pixels = [[1.0, 1.0, 1.0] for _ in range(width)]
        elif frame_index == 2:
            pixels = [[4.0, 0.0, 0.0] for _ in range(width)]
        elif frame_index == 3:
            pixels = [[40.0, 35.0, 30.0] for _ in range(width)]
        elif frame_index == 4:
            pixels = [[0.0, 6.0, 0.0] for _ in range(width)]
        else:
            pixels = []
            for pixel_index in range(width):
                ramp = (pixel_index + 1) / width
                if pixel_index % 11 == 0 and frame_index % 5 == 0:
                    pixel = [12.0 + ramp, 10.0 + ramp, 9.0 + ramp]
                elif pixel_index < 4:
                    tiny = 1e-5 * (pixel_index + 1)
                    pixel = [tiny, tiny * 0.6, tiny * 0.3]
                else:
                    pixel = [
                        rng.random() * (0.5 + 8.5 * ramp),
                        rng.random() * (0.3 + 6.0 * ramp),
                        rng.random() * (0.2 + 4.0 * ramp),
                    ]
                pixels.append([float(channel) for channel in pixel])
        frames.append(pixels)
    return frames


def base_task(task_id: str, frames_path: str, output_dir: str) -> dict[str, Any]:
    return {
        "schema_version": TASK_SCHEMA,
        "task_id": task_id,
        "frames_path": frames_path,
        "output_dir": output_dir,
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
            "window_frames": 4,
            "tail_mode": "include",
        },
        "processing": {
            "chunk_frames": 3,
            "concurrency": 2,
            "memory_budget_bytes": 67108864,
            "curve_lut_size": 65537,
        },
        "injections": [],
    }


def build_tasks(frames_file: str) -> list[dict[str, Any]]:
    pq_to_sdr = base_task(
        "pq-bt2020-to-sdr-bt709",
        frames_file,
        "out/pq_to_sdr",
    )
    pq_to_sdr["source"] = {
        "transfer": "PQ",
        "gamut": "BT2020",
        "white_point": "D65",
    }
    pq_to_sdr["target"] = {
        "transfer": "SDR_GAMMA",
        "gamut": "BT709",
        "white_point": "D65",
    }

    hlg_to_pq = base_task(
        "hlg-bt2020-to-pq-p3",
        frames_file,
        "out/hlg_to_pq",
    )
    hlg_to_pq["source"] = {
        "transfer": "HLG",
        "gamut": "BT2020",
        "white_point": "D65",
    }
    hlg_to_pq["target"] = {
        "transfer": "PQ",
        "gamut": "DISPLAY_P3",
        "white_point": "D65",
    }
    hlg_to_pq["luminance"] = {
        "reference_white_nits": 203.0,
        "source_peak_nits": 1000.0,
        "target_peak_nits": 1000.0,
    }
    hlg_to_pq["tone_mapping"] = {
        "curve": "reinhard_extended",
        "white_nits": 4000.0,
        "exposure": 1.0,
    }
    hlg_to_pq["statistics"] = {
        "window_frames": 5,
        "tail_mode": "drop",
    }
    hlg_to_pq["processing"] = {
        "chunk_frames": 4,
        "concurrency": 3,
        "memory_budget_bytes": 67108864,
        "curve_lut_size": 65537,
    }

    sdr_to_hlg = base_task(
        "sdr-bt709-to-hlg-bt2020",
        frames_file,
        "out/sdr_to_hlg",
    )
    sdr_to_hlg["source"] = {
        "transfer": "SDR_GAMMA",
        "gamut": "BT709",
        "white_point": "D65",
    }
    sdr_to_hlg["target"] = {
        "transfer": "HLG",
        "gamut": "BT2020",
        "white_point": "D65",
    }
    sdr_to_hlg["luminance"] = {
        "reference_white_nits": 100.0,
        "source_peak_nits": 100.0,
        "target_peak_nits": 1000.0,
    }
    sdr_to_hlg["tone_mapping"] = {
        "curve": "reinhard_extended",
        "white_nits": 4000.0,
        "exposure": 1.0,
    }
    sdr_to_hlg["statistics"] = {
        "window_frames": 6,
        "tail_mode": "include",
    }
    sdr_to_hlg["processing"] = {
        "chunk_frames": 5,
        "concurrency": 2,
        "memory_budget_bytes": 67108864,
        "curve_lut_size": 65537,
    }

    injections = base_task(
        "pq-injection-chain",
        frames_file,
        "out/injections",
    )
    injections["source"] = {
        "transfer": "PQ",
        "gamut": "BT2020",
        "white_point": "D65",
    }
    injections["target"] = {
        "transfer": "SDR_GAMMA",
        "gamut": "BT709",
        "white_point": "D65",
    }
    injections["statistics"] = {
        "window_frames": 3,
        "tail_mode": "include",
    }
    injections["processing"] = {
        "chunk_frames": 2,
        "concurrency": 3,
        "memory_budget_bytes": 67108864,
        "curve_lut_size": 65537,
    }
    injections["injections"] = [
        {"frame_index": 2, "kind": "NAN", "channel": 1},
        {"frame_index": 4, "kind": "NEGATIVE", "channel": 0},
        {"frame_index": 6, "kind": "PEAK_OVERFLOW"},
        {
            "frame_index": 8,
            "kind": "BAD_TRANSFER",
            "value": 1.0,
        },
    ]
    return [pq_to_sdr, hlg_to_pq, sdr_to_hlg, injections]


def write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )
    serialized = text + "\n"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="samples")
    parser.add_argument("--seed", type=int, default=20261118)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)

    frames = generate_frames(args.seed)
    frames_payload = {
        "schema_version": FRAMES_SCHEMA,
        "description": "deterministic local linear-light RGB, 1.0 = task reference white",
        "frames": frames,
    }
    frames_path = output / "frames_wide_linear.json"
    frames_digest = write_json(frames_path, frames_payload)

    tasks = build_tasks(frames_path.name)
    task_files: list[dict[str, Any]] = []
    for task in tasks:
        path = output / f"task_{task['task_id']}.json"
        digest = write_json(path, task)
        task_files.append(
            {
                "task_id": task["task_id"],
                "file": path.name,
                "sha256": digest,
                "output_dir": task["output_dir"],
                "injections": task["injections"],
            }
        )

    manifest = {
        "schema_version": "hdr-color-samples-v1",
        "seed": args.seed,
        "frame_count": len(frames),
        "pixels_per_frame": len(frames[0]),
        "frames_file": frames_path.name,
        "frames_sha256": frames_digest,
        "tasks": task_files,
    }
    write_json(output / "manifest.json", manifest)
    print(json.dumps({"status": "generated", "output": str(output), **manifest}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
