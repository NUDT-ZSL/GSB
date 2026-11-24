from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.generate_sample import generate  # noqa: E402


def make_workspace(tmp_path: Path, *, seed: int = 20261125) -> dict:
    data_dir = tmp_path / "data"
    generate(data_dir, seed)
    return {
        "data": data_dir,
        "scene": data_dir / "scene.json",
        "state": tmp_path / "state",
        "out": tmp_path / "out",
    }


def mutate_scene(scene_path: Path, key: str, value) -> None:
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    scene[key] = value
    scene_path.write_text(json.dumps(scene), encoding="utf-8")
