from __future__ import annotations

from pathlib import Path

from prefworkbench.engine import run_pipeline
from prefworkbench.generator import generate_workspace

from conftest import write_config_variant


def test_generation_is_byte_deterministic(tmp_path: Path) -> None:
    first = generate_workspace(tmp_path / "a", seed=12345, count=20)
    second = generate_workspace(tmp_path / "b", seed=12345, count=20)
    assert Path(first["samples_path"]).read_bytes() == Path(second["samples_path"]).read_bytes()
    assert Path(first["config_path"]).read_bytes() == Path(second["config_path"]).read_bytes()
    assert first["samples_sha256"] == second["samples_sha256"]


def test_initial_full_then_identical_run_reuses_everything(
    tmp_path: Path,
    generated: dict,
) -> None:
    stage = tmp_path / "stage"
    first = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=generated["config_path"],
        stage_dir=stage,
        mode="full",
    )
    assert first["version"] == 1
    assert set(first["recomputed_nodes"]) == {
        "quality_clean",
        "near_duplicate",
        "pair_consistency",
        "allocation_sampling",
    }
    first_content = (stage / first["dataset_records_path"]).read_bytes()

    second = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=generated["config_path"],
        stage_dir=stage,
        mode="incremental",
    )
    assert second["version"] == 2
    assert set(second["reused_nodes"]) == set(second["topology"]["nodes"])
    assert second["recomputed_nodes"] == []
    assert second["dataset_checksum"] == first["dataset_checksum"]
    assert (stage / second["dataset_records_path"]).read_bytes() == first_content
    assert (stage / "manifests" / "v000001.json").exists()
    assert (stage / "manifests" / "v000002.json").exists()


def test_cache_key_contains_params_and_upstream_hashes(
    tmp_path: Path,
    generated: dict,
    config: dict,
) -> None:
    stage = tmp_path / "stage"
    first = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=generated["config_path"],
        stage_dir=stage,
        mode="full",
    )
    quality_key = first["nodes"]["quality_clean"]["cache_key"]
    near_key = first["nodes"]["near_duplicate"]["cache_key"]
    consistency_key = first["nodes"]["pair_consistency"]["cache_key"]
    sampling_key = first["nodes"]["allocation_sampling"]["cache_key"]

    near_config = write_config_variant(
        tmp_path / "near.json",
        config,
        **{"near_duplicate.similarity_threshold": 0.68},
    )
    near_run = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=near_config,
        stage_dir=stage,
        mode="incremental",
    )
    assert near_run["nodes"]["quality_clean"]["cache_key"] == quality_key
    assert near_run["nodes"]["near_duplicate"]["cache_key"] != near_key
    assert near_run["nodes"]["pair_consistency"]["cache_key"] != consistency_key
    assert near_run["nodes"]["allocation_sampling"]["cache_key"] != sampling_key
    assert "quality_clean" in near_run["reused_nodes"]

    sampling_config = write_config_variant(
        tmp_path / "sampling.json",
        config_json(near_config),
        **{"sampling.seed": 999001},
    )
    sampling_run = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=sampling_config,
        stage_dir=stage,
        mode="incremental",
    )
    assert sampling_run["nodes"]["quality_clean"]["cache_key"] == quality_key
    assert sampling_run["nodes"]["near_duplicate"]["cache_key"] != near_key
    assert sampling_run["nodes"]["allocation_sampling"]["cache_key"] != sampling_key
    assert sampling_run["recomputed_nodes"] == ["allocation_sampling"]
    assert set(sampling_run["reused_nodes"]) == {
        "quality_clean",
        "near_duplicate",
        "pair_consistency",
    }


def test_quality_weight_change_changes_all_keys(
    tmp_path: Path,
    generated: dict,
    config: dict,
) -> None:
    stage = tmp_path / "stage"
    first = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=generated["config_path"],
        stage_dir=stage,
        mode="full",
    )
    updated = write_config_variant(
        tmp_path / "weight.json",
        config,
        **{"quality_clean.weights.length": 0.58},
    )
    changed = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=updated,
        stage_dir=stage,
        mode="incremental",
    )
    for node in first["nodes"]:
        assert changed["nodes"][node]["cache_key"] != first["nodes"][node]["cache_key"]
    assert set(changed["recomputed_nodes"]) == set(changed["topology"]["nodes"])


def test_key_components_expand_and_reconstruct(tmp_path: Path, generated: dict) -> None:
    first = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=generated["config_path"],
        stage_dir=tmp_path / "stage",
        mode="full",
    )
    from prefworkbench.cache import cache_key_from_components

    for node, diag in first["nodes"].items():
        components = diag["key_components"]
        assert any(item["kind"] == "parameters" for item in components)
        if node == "quality_clean":
            assert any(item["kind"] == "input_digest" for item in components)
        else:
            assert any(item["kind"] == "upstream_payload_hash" for item in components)
        assert cache_key_from_components(components) == diag["cache_key"]


def config_json(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))
