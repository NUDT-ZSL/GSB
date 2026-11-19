from __future__ import annotations

import json
from pathlib import Path

import pytest

from prefworkbench.engine import run_pipeline
from prefworkbench.errors import WorkbenchError
from prefworkbench.generator import generate_workspace, write_config
from prefworkbench.selfcheck import run_selfcheck

from conftest import write_config_variant


def test_conflict_strict_error_and_explicit_retention(tmp_path: Path) -> None:
    generated = generate_workspace(
        tmp_path / "data",
        seed=17,
        count=12,
        with_conflict=True,
        conflict_policy="error",
    )
    with pytest.raises(WorkbenchError) as raised:
        run_pipeline(
            samples_path=generated["samples_path"],
            config_path=generated["config_path"],
            stage_dir=tmp_path / "strict",
            mode="full",
        )
    error = raised.value
    assert error.code == "E_PAIR_CONFLICT"
    assert error.node == "pair_consistency"
    assert {"conflict_a", "conflict_b"} <= set(error.details["pair_ids"])
    assert error.details["conflict_group_ids"]

    config = json.loads(Path(generated["config_path"]).read_text(encoding="utf-8"))
    config["pair_consistency"]["conflict_policy"] = "keep_and_flag"
    keep_path = tmp_path / "keep.json"
    write_config(keep_path, config)
    keep_stage = tmp_path / "keep-stage"
    manifest = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=keep_path,
        stage_dir=keep_stage,
        mode="full",
    )
    assert len(manifest["conflicts"]) == 1
    assert manifest["conflict_pair_ids"] == ["conflict_a", "conflict_b"]
    assert any(
        item["category"] == "PAIR_CONFLICT_RETAINED" for item in manifest["degradations"]
    )
    assert run_selfcheck(keep_stage, persist=False)["ok"]


def test_conflict_event_is_in_recent_error_log(tmp_path: Path) -> None:
    generated = generate_workspace(
        tmp_path / "data",
        seed=23,
        count=10,
        with_conflict=True,
        conflict_policy="error",
    )
    with pytest.raises(WorkbenchError):
        run_pipeline(
            samples_path=generated["samples_path"],
            config_path=generated["config_path"],
            stage_dir=tmp_path / "stage",
            mode="full",
        )
    history = json.loads((tmp_path / "stage" / "errors.json").read_text(encoding="utf-8"))
    assert any(item["code"] == "E_PAIR_CONFLICT" for item in history["errors"])


def test_near_duplicate_limit_error_and_degrade(tmp_path: Path, generated: dict, config: dict) -> None:
    error_config = write_config_variant(
        tmp_path / "near-error.json",
        config,
        **{
            "near_duplicate.max_comparisons": 1,
            "near_duplicate.on_limit": "error",
        },
    )
    with pytest.raises(WorkbenchError) as raised:
        run_pipeline(
            samples_path=generated["samples_path"],
            config_path=error_config,
            stage_dir=tmp_path / "error-stage",
            mode="full",
        )
    assert raised.value.code == "E_RESOURCE_LIMIT"
    assert raised.value.details["category"] == "NEAR_DUPLICATE_COMPARISON_LIMIT"

    degrade_config = write_config_variant(
        tmp_path / "near-degrade.json",
        config,
        **{
            "near_duplicate.max_comparisons": 1,
            "near_duplicate.on_limit": "degrade",
        },
    )
    stage = tmp_path / "degrade-stage"
    manifest = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=degrade_config,
        stage_dir=stage,
        mode="full",
    )
    assert any(
        item["category"] == "NEAR_DUPLICATE_COMPARISON_TRUNCATED"
        for item in manifest["degradations"]
    )
    assert run_selfcheck(stage, persist=False)["ok"]


def test_manifest_memory_limit_returns_resource_error(
    tmp_path: Path,
    generated: dict,
    config: dict,
) -> None:
    config_path = write_config_variant(
        tmp_path / "small.json",
        config,
        **{
            "resources.max_manifest_bytes": 1200,
            "resources.on_limit": "error",
        },
    )
    with pytest.raises(WorkbenchError) as raised:
        run_pipeline(
            samples_path=generated["samples_path"],
            config_path=config_path,
            stage_dir=tmp_path / "stage",
            mode="full",
        )
    assert raised.value.code == "E_RESOURCE_LIMIT"
    assert raised.value.details["category"] == "MANIFEST_MEMORY_LIMIT"
    assert not (tmp_path / "stage" / "current.json").exists()


def test_parallel_limit_error_and_degrade(tmp_path: Path, generated: dict, config: dict) -> None:
    config_path = write_config_variant(
        tmp_path / "parallel.json",
        config,
        **{"resources.max_parallel_nodes": 1, "resources.on_limit": "error"},
    )
    with pytest.raises(WorkbenchError) as raised:
        run_pipeline(
            samples_path=generated["samples_path"],
            config_path=config_path,
            stage_dir=tmp_path / "error-stage",
            mode="full",
            requested_parallel_nodes=2,
        )
    assert raised.value.code == "E_RESOURCE_LIMIT"

    degrade_path = write_config_variant(
        tmp_path / "parallel-degrade.json",
        config,
        **{"resources.max_parallel_nodes": 1, "resources.on_limit": "degrade"},
    )
    manifest = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=degrade_path,
        stage_dir=tmp_path / "degrade-stage",
        mode="full",
        requested_parallel_nodes=2,
    )
    assert manifest["resources"]["effective_parallel_nodes"] == 1
    assert any(item["category"] == "PARALLELISM_CAPPED" for item in manifest["degradations"])
