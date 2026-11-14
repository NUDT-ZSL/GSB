import json
from pathlib import Path

import pytest


def scene_dict(
    width,
    height,
    values,
    levels,
    queries=None,
    canvas=(200, 100),
    world=None,
    **extra,
):
    data = {
        "grid": {"width": width, "height": height, "values": values},
        "levels": levels,
        "queries": queries or [],
        "canvas": {"width": canvas[0], "height": canvas[1]},
    }
    if world is not None:
        data["world"] = world
    data.update(extra)
    return data


def write_scene(tmp_path, data, name="scene.json"):
    path = Path(tmp_path) / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def writable_out(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    return out
