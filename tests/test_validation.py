from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from surface_stitch.errors import PipelineError
from surface_stitch.hashing import sha256_file
from surface_stitch.loader import load_project


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_generated_sample_is_valid(sample_dir: Path) -> None:
    sample = load_project(sample_dir)
    assert len(sample.tiles) == 4
    assert sample.version_id


def test_layout_coverage_error(sample_dir: Path) -> None:
    project_path = sample_dir / "project.json"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project["tiles"][0]["width"] = 40
    write_json(project_path, project)
    with pytest.raises(PipelineError) as exc:
        load_project(sample_dir)
    assert exc.value.code == "E_LAYOUT_COVERAGE"


def test_overlap_declaration_error(sample_dir: Path) -> None:
    project_path = sample_dir / "project.json"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project["overlaps"][0]["bbox"]["width"] += 4
    write_json(project_path, project)
    with pytest.raises(PipelineError) as exc:
        load_project(sample_dir)
    assert exc.value.code == "E_OVERLAP"


def test_truth_offset_finite_error(sample_dir: Path) -> None:
    truth_path = sample_dir / "truth_offsets.json"
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    truth["tiles"][1]["offset_xy"][0] = "not-a-number"
    write_json(truth_path, truth)
    with pytest.raises(PipelineError) as exc:
        load_project(sample_dir)
    assert exc.value.code == "E_OFFSET_FINITE"


def test_image_dimension_error(sample_dir: Path) -> None:
    Image.new("L", (32, 32), 128).save(sample_dir / "tiles" / "tile_00.png")
    with pytest.raises(PipelineError) as exc:
        load_project(sample_dir)
    assert exc.value.code == "E_IMAGE_SIZE"


def test_v2_soft_weight_domain_error(sample_dir: Path) -> None:
    manifest_path = sample_dir / "descriptors" / "v2" / "v2_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shard_path = sample_dir / "descriptors" / "v2" / "shard_01.json"
    shard = json.loads(shard_path.read_text(encoding="utf-8"))
    shard["records"][0]["soft_mask"]["weights"][0] = 1.5
    write_json(shard_path, shard)
    for shard_descriptor in manifest["shards"]:
        if shard_descriptor["path"] == "shard_01.json":
            shard_descriptor["sha256"] = sha256_file(shard_path)
    write_json(manifest_path, manifest)
    with pytest.raises(PipelineError) as exc:
        load_project(sample_dir)
    assert exc.value.code == "E_MASK_DOMAIN"
