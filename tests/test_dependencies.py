from dataclasses import replace

from seg_diag.config_io import read_v1_config, read_v2_config
from seg_diag.dependencies import plan_revision
from seg_diag.hashing import content_key
from seg_diag.ledger import build_ledger
from seg_diag.sample_generator import generate_sample_directory, load_sample_arrays


def _revised(v2, **changes):
    config = replace(v2, **changes)
    config.checksum = content_key(config.canonical_payload())
    return config


def test_threshold_revision_recomputes_only_dependent_rows(tmp_path):
    sample_dir = generate_sample_directory(tmp_path / "sample", seed=801, force=True)
    arrays = load_sample_arrays(sample_dir)
    v1 = read_v1_config(sample_dir / "config_v1.json")
    v2 = read_v2_config(sample_dir / "config_v2")
    old_ledger = build_ledger(arrays, v1, v2, seed=801)

    mappings = tuple(
        replace(m, confidence_threshold=0.93) if m.instance_id == 201 else m
        for m in v2.mappings
    )
    rev2 = _revised(v2, revision=2, mappings=mappings)
    plan = plan_revision(arrays, v1, v2, rev2)

    assert plan["threshold_changes"]
    assert 0 < plan["affected_pixel_count"] < arrays["gt_instance"].size
    coverage = plan["coverage_check"]
    assert coverage["disjoint"]
    assert coverage["union_all_rows"]
    assert not coverage["full_image_recompute"]

    incremental = build_ledger(
        arrays,
        v1,
        rev2,
        seed=801,
        existing_rows={row["row_id"]: row for row in old_ledger["rows"]},
        ledger_version=2,
    )
    assert set(incremental["_computed_rows"]) == set(plan["recompute_set"])
    assert set(incremental["_reused_rows"]) == set(plan["reuse_set"])


def test_mapping_revision_suffix_and_sets(tmp_path):
    sample_dir = generate_sample_directory(tmp_path / "sample", seed=802, force=True)
    arrays = load_sample_arrays(sample_dir)
    v1 = read_v1_config(sample_dir / "config_v1.json")
    v2 = read_v2_config(sample_dir / "config_v2")

    mappings = tuple(
        replace(m, category_id=2) if m.instance_id == 301 else m for m in v2.mappings
    )
    rev3 = _revised(v2, revision=3, mappings=mappings)
    plan = plan_revision(arrays, v1, v2, rev3)

    assert plan["mapping_changes"]
    assert plan["invalidated_stage_suffix"][0] == "normalize_v2"
    assert plan["coverage_check"]["covered"]
    assert plan["recompute_set"]
    assert plan["reuse_set"]


def test_metadata_only_revision_has_no_pixel_recompute(tmp_path):
    sample_dir = generate_sample_directory(tmp_path / "sample", seed=803, force=True)
    arrays = load_sample_arrays(sample_dir)
    v1 = read_v1_config(sample_dir / "config_v1.json")
    v2 = read_v2_config(sample_dir / "config_v2")
    rev = _revised(v2, revision=2, effective_at_ms=v2.effective_at_ms + 5000)
    plan = plan_revision(arrays, v1, v2, rev)
    assert plan["pixel_relevant"] is False
    assert plan["affected_pixel_count"] == 0
    assert plan["coverage_check"]["recompute_count"] == 0
