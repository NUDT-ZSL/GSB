"""Business rule 1: parsing and strict validation."""

from __future__ import annotations

import json

import pytest

from terrain_engine import errors
from terrain_engine.validate import load_task


def write_task(sample_dir, mutate):
    data = json.loads((sample_dir / "task.json").read_text(encoding="utf-8"))
    mutate(data)
    path = sample_dir / "bad_task.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_terrain(sample_dir, mutate):
    data = json.loads((sample_dir / "terrain.json").read_text(encoding="utf-8"))
    mutate(data)
    path = sample_dir / "bad_terrain.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_valid_sample_loads(sample_dir):
    task = load_task(sample_dir / "task.json")
    assert task.width_px == 96 and task.height_px == 72
    assert len(task.tiles) == 9


def test_bad_grid_dims(sample_dir):
    path = write_terrain(sample_dir, lambda data: data.update(rows_nx=0))
    with pytest.raises(errors.EngineError) as info:
        load_task(sample_dir / "task.json", terrain_override=path)
    assert info.value.code == errors.E_GRID_DIMS


def test_bad_scale(sample_dir):
    path = write_terrain(sample_dir, lambda data: data.update(scale_z=-1.0))
    with pytest.raises(errors.EngineError) as info:
        load_task(sample_dir / "task.json", terrain_override=path)
    assert info.value.code == errors.E_SCALE


def test_height_count_mismatch(sample_dir):
    def drop_row(data):
        data["heights"].pop()
    path = write_terrain(sample_dir, drop_row)
    with pytest.raises(errors.EngineError) as info:
        load_task(sample_dir / "task.json", terrain_override=path)
    assert info.value.code == errors.E_HEIGHT_COUNT


def test_non_integer_height(sample_dir):
    def break_height(data):
        data["heights"][0][0] = 1.5
    path = write_terrain(sample_dir, break_height)
    with pytest.raises(errors.EngineError) as info:
        load_task(sample_dir / "task.json", terrain_override=path)
    assert info.value.code == errors.E_HEIGHT_COUNT


def test_tile_coverage_divisibility(sample_dir):
    path = write_task(
        sample_dir, lambda data: data["tiles"].update(tile_width=30))
    with pytest.raises(errors.EngineError) as info:
        load_task(path)
    assert info.value.code == errors.E_TILE_DIVISIBILITY


def test_bad_samples(sample_dir):
    path = write_task(sample_dir, lambda data: data.update(samples=0))
    with pytest.raises(errors.EngineError) as info:
        load_task(path)
    assert info.value.code == errors.E_SAMPLES


def test_parallel_camera_rejected(sample_dir):
    def make_parallel(data):
        data["camera"]["up"] = [0.0, 0.0, 1.0]
    path = write_task(sample_dir, make_parallel)
    with pytest.raises(errors.EngineError) as info:
        load_task(path)
    assert info.value.code == errors.E_CAMERA_AXIS


def test_invalid_json_returns_parse_error(sample_dir):
    path = sample_dir / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(errors.EngineError) as info:
        load_task(path)
    assert info.value.code == errors.E_PARSE


def test_validation_creates_no_outputs(sample_dir, tmp_path):
    path = write_task(sample_dir, lambda data: data.update(samples=-1))
    before = set(p.name for p in sample_dir.iterdir())
    with pytest.raises(errors.EngineError):
        load_task(path)
    after = set(p.name for p in sample_dir.iterdir())
    assert before == after

