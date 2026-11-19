from __future__ import annotations

from pathlib import Path

from prefworkbench.canonical import read_json
from prefworkbench.engine import run_pipeline
from prefworkbench.generator import mutate_samples, read_samples, write_jsonl

from conftest import write_config_variant


def test_annotator_edit_incremental_matches_full(
    tmp_path: Path,
    generated: dict,
    config: dict,
) -> None:
    stage = tmp_path / "incremental"
    initial = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=generated["config_path"],
        stage_dir=stage,
        mode="full",
    )
    samples, _ = read_samples(generated["samples_path"])
    mutated = mutate_samples(
        samples,
        pair_id="p0001",
        chosen_suffix=" [corrected by alpha]",
        bump_version=True,
    )
    mutated_path = tmp_path / "mutated.jsonl"
    write_jsonl(mutated_path, mutated)

    incremental = run_pipeline(
        samples_path=mutated_path,
        config_path=generated["config_path"],
        stage_dir=stage,
        mode="incremental",
    )
    full_stage = tmp_path / "full"
    full = run_pipeline(
        samples_path=mutated_path,
        config_path=generated["config_path"],
        stage_dir=full_stage,
        mode="full",
    )
    assert incremental["dataset_checksum"] == full["dataset_checksum"]
    assert (
        (stage / incremental["dataset_records_path"]).read_bytes()
        == (full_stage / full["dataset_records_path"]).read_bytes()
    )
    for node in initial["nodes"]:
        assert incremental["nodes"][node]["payload_hash"] == full["nodes"][node]["payload_hash"]
        assert incremental["nodes"][node]["cache_key"] == full["nodes"][node]["cache_key"]
    assert set(incremental["recomputed_nodes"]) == set(incremental["topology"]["nodes"])
    assert "p0001" in incremental["affected_pair_ids"]


def test_near_threshold_reuses_quality_prefix(
    tmp_path: Path,
    generated: dict,
    config: dict,
) -> None:
    stage = tmp_path / "stage"
    run_pipeline(
        samples_path=generated["samples_path"],
        config_path=generated["config_path"],
        stage_dir=stage,
        mode="full",
    )
    near_path = write_config_variant(
        tmp_path / "near.json",
        config,
        **{"near_duplicate.similarity_threshold": 0.67},
    )
    changed = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=near_path,
        stage_dir=stage,
        mode="incremental",
    )
    assert changed["reused_nodes"] == ["quality_clean"]
    assert set(changed["recomputed_nodes"]) == {
        "near_duplicate",
        "pair_consistency",
        "allocation_sampling",
    }


def test_sampling_underfill_is_explained_and_stable(
    tmp_path: Path,
    generated: dict,
    config: dict,
) -> None:
    config_path = write_config_variant(
        tmp_path / "sampling.json",
        config,
        **{"sampling.total_target": 10000},
    )
    stage = tmp_path / "stage"
    first = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=config_path,
        stage_dir=stage,
        mode="full",
    )
    total_target = next(
        item for item in first["quota_targets"] if item["kind"] == "total"
    )
    assert total_target["status"] == "underfilled"
    assert total_target["reason"] == "INSUFFICIENT_TOTAL_INVENTORY"
    assert first["underfilled_targets"]
    assert first["sampling_seed"] == config["sampling"]["seed"]

    second = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=config_path,
        stage_dir=stage,
        mode="incremental",
    )
    assert "allocation_sampling" in second["reused_nodes"]
    assert second["dataset_checksum"] == first["dataset_checksum"]
    payload_path = stage / second["dataset_records_path"]
    assert payload_path.exists()
    assert payload_path.read_text(encoding="utf-8").count("\n") == (
        second["nodes"]["allocation_sampling"]["counts"]["selected"]
    )


def test_dataset_records_are_traceable(tmp_path: Path, generated: dict) -> None:
    stage = tmp_path / "stage"
    manifest = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=generated["config_path"],
        stage_dir=stage,
        mode="full",
    )
    quality = read_json(
        stage / "cache" / "objects" / "quality_clean" / (
            manifest["nodes"]["quality_clean"]["cache_key"].removeprefix("sha256:") + ".json"
        )
    )["payload"]
    quality_by_id = {item["pair_id"]: item for item in quality["pair_metrics"]}
    content_lines = (stage / manifest["dataset_records_path"]).read_text(encoding="utf-8").splitlines()
    for line in content_lines:
        record = read_json_from_text(line)
        assert record["quality_score"] == quality_by_id[record["pair_id"]]["quality_score"]
        assert record["quality_tier"] == quality_by_id[record["pair_id"]]["quality_tier"]
        assert "signature_group_id" in record
        assert "max_signature_similarity" in record


def read_json_from_text(text: str) -> dict:
    import json

    return json.loads(text)
