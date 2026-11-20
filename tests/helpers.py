from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tracker_app.config import load_config
from tracker_app.data_gen import generate_dataset, write_dataset


REPO_ROOT = Path(__file__).resolve().parents[1]


def small_config() -> dict[str, Any]:
    config = copy.deepcopy(load_config(REPO_ROOT / "config" / "default.json"))
    config["generator"].update(
        {
            "sequence_count": 2,
            "frames": 24,
            "targets_per_frame": 3,
            "occlusion_intervals": [[5, 6], [14, 15]],
            "disappearance_intervals": [[9, 10], [18, 19]],
            "crossing_frame": 12,
            "revision": 1,
        }
    )
    config["gap"]["max_gap_frames"] = 3
    config["resources"]["max_detections_per_frame"] = 12
    config["resources"]["max_appearance_cache_entries"] = 32
    config["resources"]["max_parallel_gap_tracks"] = 4
    return config


def write_config(config: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def materialize_dataset(
    base_dir: Path,
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    config = config or small_config()
    dataset = generate_dataset(config)
    data_dir = base_dir / "data"
    write_dataset(dataset, data_dir)
    config_path = write_config(config, base_dir / "config.json")
    return config, dataset, config_path


def run_cli(
    arguments: list[str],
    *,
    cwd: Path = REPO_ROOT,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "tracker_app", *arguments],
        cwd=str(cwd),
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
