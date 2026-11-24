from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent.parent


def base_payload(width: int = 8, height: int = 6, **render_overrides: Any) -> dict[str, Any]:
    render = {
        "width": width,
        "height": height,
        "samples_per_pixel": 1,
        "step_size": 0.5,
        "max_steps": 64,
        "transmittance_cutoff": 0.08,
        "fixed_scale": 256,
        "transmittance_tolerance": 0.0002,
        "ray_epsilon": 1e-7,
        "exposure": 3.0,
        "tile": {"width": 4, "height": 3},
        "max_workers": 1,
        "inflight_memory_limit_bytes": 10 * 1024 * 1024,
        "state_dir": "../state",
        "output_dir": "../output",
    }
    render.update(render_overrides)
    return {
        "schema_version": 1,
        "name": "unit-test-scene",
        "seed": 7,
        "bounds": {"min": [-5.0, -5.0, -5.0], "max": [5.0, 5.0, 5.0]},
        "data": {"media": "media.json", "lights": "lights.json", "jitter": "jitter.json"},
        "camera": {
            "position": [0.0, 0.0, -7.0],
            "forward": [0.0, 0.0, 1.0],
            "up": [0.0, 1.0, 0.0],
            "vertical_fov_deg": 60.0,
            "aspect_ratio": width / height,
        },
        "render": render,
        "media": [
            {
                "id": "medium_a",
                "kind": "fog",
                "center": [0.0, 0.0, 0.0],
                "size": [5.0, 5.0, 5.0],
                "extinction": [0.7, 0.7, 0.7],
                "albedo": [0.5, 0.5, 0.5],
                "ior": 1.0,
            }
        ],
        "lights": [
            {
                "id": "light_a",
                "position": [1.0, 1.0, -2.0],
                "intensity": 18.0,
            }
        ],
        "jitter": {
            "sample_offsets": [[0.5, 0.5]],
            "step_scales": [1.0, 1.5, 2.0],
            "cutoff_scales": [1.0, 1.5, 2.0],
        },
    }


def write_scene(
    directory: Path,
    *,
    width: int = 8,
    height: int = 6,
    mutate: Callable[[dict[str, Any]], None] | None = None,
    **render_overrides: Any,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    payload = base_payload(width, height, **render_overrides)
    if mutate is not None:
        mutate(payload)
    media = payload.pop("media")
    lights = payload.pop("lights")
    jitter = payload.pop("jitter")
    (directory / "media.json").write_text(json.dumps({"media": media}), encoding="utf-8")
    (directory / "lights.json").write_text(json.dumps({"lights": lights}), encoding="utf-8")
    (directory / "jitter.json").write_text(json.dumps(jitter), encoding="utf-8")
    scene_path = directory / "scene.json"
    scene_path.write_text(json.dumps(payload), encoding="utf-8")
    return scene_path


def clone_payload(**overrides: Any) -> dict[str, Any]:
    payload = copy.deepcopy(base_payload())
    payload["render"].update(overrides)
    return payload


def run_cli(
    *args: str,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    if env:
        environment.update(env)
    return subprocess.run(
        [sys.executable, "-m", "volumetric_engine", *args],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
    )

