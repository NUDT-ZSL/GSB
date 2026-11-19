from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, hash_value, read_json
from .config import validate_config
from .diagnostics import ensure_selfcheck, write_dashboard
from .engine import run_pipeline
from .errors import WorkbenchError
from .generator import (
    generate_workspace,
    mutate_samples,
    read_samples,
    write_config,
    write_jsonl,
)
from .store import Stage


def _check(name: str, ok: bool, **details: Any) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "details": details}


def _fresh(path: Path) -> Path:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": manifest["version"],
        "dataset_checksum": manifest["dataset_checksum"],
        "reused_nodes": manifest["reused_nodes"],
        "recomputed_nodes": manifest["recomputed_nodes"],
        "invalidated_nodes": manifest["invalidated_nodes"],
        "affected_nodes": manifest["affected_nodes"],
        "node_payload_hashes": {
            node: diag["payload_hash"] for node, diag in manifest["nodes"].items()
        },
        "node_cache_keys": {
            node: diag["cache_key"] for node, diag in manifest["nodes"].items()
        },
    }


def _compare_incremental_with_full(
    incremental: dict[str, Any],
    full: dict[str, Any],
    incremental_stage: Stage,
    full_stage: Stage,
) -> dict[str, Any]:
    incremental_content = (
        incremental_stage.root / incremental["dataset_records_path"]
    ).read_bytes()
    full_content = (full_stage.root / full["dataset_records_path"]).read_bytes()
    payload_hashes_match = {
        node: incremental["nodes"][node]["payload_hash"]
        == full["nodes"][node]["payload_hash"]
        for node in incremental["nodes"]
    }
    cache_keys_match = {
        node: incremental["nodes"][node]["cache_key"] == full["nodes"][node]["cache_key"]
        for node in incremental["nodes"]
    }
    return {
        "payload_hashes_match": payload_hashes_match,
        "cache_keys_match": cache_keys_match,
        "dataset_checksum_match": incremental["dataset_checksum"] == full["dataset_checksum"],
        "dataset_records_bytes_match": incremental_content == full_content,
        "incremental_dataset_bytes_checksum": hash_value(
            list(incremental_content.decode("utf-8").splitlines())
        ),
        "full_dataset_bytes_checksum": hash_value(
            list(full_content.decode("utf-8").splitlines())
        ),
    }


def _set_config_value(config: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    result = copy.deepcopy(config)
    cursor = result
    parts = path.split(".")
    for part in parts[:-1]:
        cursor = cursor[part]
    cursor[parts[-1]] = value
    return validate_config(result)


def run_demo(workspace_root: Path | str) -> dict[str, Any]:
    workspace = Path(workspace_root)
    demo_root = _fresh(workspace / "artifacts" / "demo")
    data_a = _fresh(demo_root / "data-a")
    data_b = _fresh(demo_root / "data-b")
    generated_a = generate_workspace(data_a, seed=20261120, count=36)
    generated_b = generate_workspace(data_b, seed=20261120, count=36)
    samples_path = Path(generated_a["samples_path"])
    config_path = Path(generated_a["config_path"])
    config = validate_config(read_json(config_path))
    samples, _ = read_samples(samples_path)

    checks: list[dict[str, Any]] = []
    checks.append(
        _check(
            "deterministic_generation",
            Path(generated_a["samples_path"]).read_bytes()
            == Path(generated_b["samples_path"]).read_bytes()
            and Path(generated_a["config_path"]).read_bytes()
            == Path(generated_b["config_path"]).read_bytes(),
            samples_sha256=generated_a["samples_sha256"],
            config_sha256=generated_a["config_sha256"],
        )
    )

    stage_a = Stage(demo_root / "stage-a")
    initial = run_pipeline(
        samples_path=samples_path,
        config_path=config_path,
        stage_dir=stage_a.root,
        mode="full",
    )
    checks.append(
        _check(
            "initial_full_recompute",
            set(initial["recomputed_nodes"]) == set(initial["topology"]["nodes"])
            and not initial["reused_nodes"],
            manifest=_manifest_summary(initial),
        )
    )

    identical = run_pipeline(
        samples_path=samples_path,
        config_path=config_path,
        stage_dir=stage_a.root,
        mode="incremental",
    )
    checks.append(
        _check(
            "identical_input_all_cache_hit",
            set(identical["reused_nodes"]) == set(identical["topology"]["nodes"])
            and not identical["recomputed_nodes"]
            and identical["dataset_checksum"] == initial["dataset_checksum"],
            manifest=_manifest_summary(identical),
        )
    )

    mutated_samples = mutate_samples(
        samples,
        pair_id="p0001",
        chosen_suffix=" [annotator correction]",
        bump_version=True,
    )
    mutated_path = demo_root / "samples-mutated.jsonl"
    write_jsonl(mutated_path, mutated_samples)
    mutated_incremental = run_pipeline(
        samples_path=mutated_path,
        config_path=config_path,
        stage_dir=stage_a.root,
        mode="incremental",
    )
    full_stage_mutated = _fresh(demo_root / "stage-full-mutated")
    mutated_full = run_pipeline(
        samples_path=mutated_path,
        config_path=config_path,
        stage_dir=full_stage_mutated,
        mode="full",
    )
    comparison = _compare_incremental_with_full(
        mutated_incremental,
        mutated_full,
        stage_a,
        Stage(full_stage_mutated),
    )
    checks.append(
        _check(
            "annotator_edit_incremental_equals_full",
            all(comparison["payload_hashes_match"].values())
            and all(comparison["cache_keys_match"].values())
            and comparison["dataset_checksum_match"]
            and comparison["dataset_records_bytes_match"],
            incremental=_manifest_summary(mutated_incremental),
            full=_manifest_summary(mutated_full),
            comparison=comparison,
        )
    )

    near_config = _set_config_value(config, "near_duplicate.similarity_threshold", 0.68)
    near_config_path = demo_root / "config-near-068.json"
    write_config(near_config_path, near_config)
    near_incremental = run_pipeline(
        samples_path=mutated_path,
        config_path=near_config_path,
        stage_dir=stage_a.root,
        mode="incremental",
    )
    near_full_stage = _fresh(demo_root / "stage-full-near")
    near_full = run_pipeline(
        samples_path=mutated_path,
        config_path=near_config_path,
        stage_dir=near_full_stage,
        mode="full",
    )
    near_comparison = _compare_incremental_with_full(
        near_incremental,
        near_full,
        stage_a,
        Stage(near_full_stage),
    )
    checks.append(
        _check(
            "near_threshold_prefix_reuse",
            "quality_clean" in near_incremental["reused_nodes"]
            and {"near_duplicate", "pair_consistency", "allocation_sampling"}
            <= set(near_incremental["recomputed_nodes"])
            and all(near_comparison["payload_hashes_match"].values())
            and near_comparison["dataset_records_bytes_match"],
            manifest=_manifest_summary(near_incremental),
            comparison=near_comparison,
        )
    )

    sampling_config = _set_config_value(near_config, "sampling.seed", 424242)
    sampling_config_path = demo_root / "config-sampling-seed.json"
    write_config(sampling_config_path, sampling_config)
    sampling_incremental = run_pipeline(
        samples_path=mutated_path,
        config_path=sampling_config_path,
        stage_dir=stage_a.root,
        mode="incremental",
    )
    sampling_full_stage = _fresh(demo_root / "stage-full-sampling")
    sampling_full = run_pipeline(
        samples_path=mutated_path,
        config_path=sampling_config_path,
        stage_dir=sampling_full_stage,
        mode="full",
    )
    sampling_comparison = _compare_incremental_with_full(
        sampling_incremental,
        sampling_full,
        stage_a,
        Stage(sampling_full_stage),
    )
    checks.append(
        _check(
            "sampling_seed_only_recomputes_sampling",
            set(sampling_incremental["reused_nodes"])
            == {"quality_clean", "near_duplicate", "pair_consistency"}
            and sampling_incremental["recomputed_nodes"] == ["allocation_sampling"]
            and sampling_comparison["dataset_checksum_match"],
            manifest=_manifest_summary(sampling_incremental),
            comparison=sampling_comparison,
        )
    )

    quality_config = _set_config_value(sampling_config, "quality_clean.weights.length", 0.61)
    quality_config_path = demo_root / "config-quality-weight.json"
    write_config(quality_config_path, quality_config)
    quality_incremental = run_pipeline(
        samples_path=mutated_path,
        config_path=quality_config_path,
        stage_dir=stage_a.root,
        mode="incremental",
    )
    quality_full_stage = _fresh(demo_root / "stage-full-quality")
    quality_full = run_pipeline(
        samples_path=mutated_path,
        config_path=quality_config_path,
        stage_dir=quality_full_stage,
        mode="full",
    )
    quality_comparison = _compare_incremental_with_full(
        quality_incremental,
        quality_full,
        stage_a,
        Stage(quality_full_stage),
    )
    checks.append(
        _check(
            "quality_weight_invalidates_downstream",
            set(quality_incremental["recomputed_nodes"]) == set(quality_incremental["topology"]["nodes"])
            and all(quality_comparison["payload_hashes_match"].values())
            and quality_comparison["dataset_records_bytes_match"],
            manifest=_manifest_summary(quality_incremental),
            comparison=quality_comparison,
        )
    )

    conflict_data = _fresh(demo_root / "conflict-data")
    conflict_generated = generate_workspace(
        conflict_data,
        seed=17,
        count=12,
        with_conflict=True,
        conflict_policy="error",
    )
    strict_stage = demo_root / "stage-conflict-strict"
    conflict_error: WorkbenchError | None = None
    try:
        run_pipeline(
            samples_path=conflict_generated["samples_path"],
            config_path=conflict_generated["config_path"],
            stage_dir=strict_stage,
            mode="full",
        )
    except WorkbenchError as exc:
        conflict_error = exc
    checks.append(
        _check(
            "strict_conflict_returns_E_PAIR_CONFLICT",
            conflict_error is not None
            and conflict_error.code == "E_PAIR_CONFLICT"
            and bool(conflict_error.details.get("pair_ids"))
            and bool(conflict_error.details.get("conflict_group_ids")),
            error=conflict_error.to_dict() if conflict_error else None,
        )
    )

    conflict_config = validate_config(read_json(conflict_generated["config_path"]))
    conflict_config["pair_consistency"]["conflict_policy"] = "keep_and_flag"
    conflict_config_path = conflict_data / "config-keep.json"
    write_config(conflict_config_path, conflict_config)
    kept_stage = Stage(demo_root / "stage-conflict-kept")
    kept = run_pipeline(
        samples_path=conflict_generated["samples_path"],
        config_path=conflict_config_path,
        stage_dir=kept_stage.root,
        mode="full",
    )
    kept_check = ensure_selfcheck(kept_stage.root)
    checks.append(
        _check(
            "explicit_conflict_retention_is_recorded",
            len(kept["conflicts"]) == 1
            and bool(kept["conflict_pair_ids"])
            and kept_check["ok"],
            conflicts=kept["conflicts"],
            selfcheck=kept_check,
        )
    )

    degrade_config = copy.deepcopy(config)
    degrade_config["near_duplicate"]["max_comparisons"] = 1
    degrade_config["near_duplicate"]["on_limit"] = "degrade"
    degrade_config_path = demo_root / "config-near-degrade.json"
    write_config(degrade_config_path, degrade_config)
    degraded_stage = Stage(demo_root / "stage-resource-degrade")
    degraded = run_pipeline(
        samples_path=mutated_path,
        config_path=degrade_config_path,
        stage_dir=degraded_stage.root,
        mode="full",
    )
    degraded_check = ensure_selfcheck(degraded_stage.root)
    checks.append(
        _check(
            "near_limit_degrade_is_marked",
            any(
                item["category"] == "NEAR_DUPLICATE_COMPARISON_TRUNCATED"
                for item in degraded["degradations"]
            )
            and degraded_check["ok"],
            degradations=degraded["degradations"],
            selfcheck=degraded_check,
        )
    )

    error_config = copy.deepcopy(degrade_config)
    error_config["near_duplicate"]["on_limit"] = "error"
    error_config_path = demo_root / "config-near-error.json"
    write_config(error_config_path, error_config)
    resource_error: WorkbenchError | None = None
    try:
        run_pipeline(
            samples_path=mutated_path,
            config_path=error_config_path,
            stage_dir=demo_root / "stage-resource-error",
            mode="full",
        )
    except WorkbenchError as exc:
        resource_error = exc
    checks.append(
        _check(
            "near_limit_error_returns_E_RESOURCE_LIMIT",
            resource_error is not None and resource_error.code == "E_RESOURCE_LIMIT",
            error=resource_error.to_dict() if resource_error else None,
        )
    )

    corruption_stage = Stage(demo_root / "stage-corrupt")
    corrupt_manifest = run_pipeline(
        samples_path=mutated_path,
        config_path=quality_config_path,
        stage_dir=corruption_stage.root,
        mode="full",
    )
    quality_diag = corrupt_manifest["nodes"]["quality_clean"]
    corrupt_path = corruption_stage.root / quality_diag["cache_object_path"]
    corrupt_bytes = corrupt_path.read_bytes()
    corrupt_path.write_bytes(corrupt_bytes[:-8] + b"BROKEN!!")
    corruption_error: WorkbenchError | None = None
    try:
        run_pipeline(
            samples_path=mutated_path,
            config_path=quality_config_path,
            stage_dir=corruption_stage.root,
            mode="incremental",
        )
    except WorkbenchError as exc:
        corruption_error = exc
    checks.append(
        _check(
            "corrupt_cache_returns_E_CACHE_CORRUPT",
            corruption_error is not None
            and corruption_error.code == "E_CACHE_CORRUPT"
            and corruption_error.node == "quality_clean"
            and corruption_error.version is not None,
            error=corruption_error.to_dict() if corruption_error else None,
        )
    )

    stale_error: WorkbenchError | None = None
    try:
        run_pipeline(
            samples_path=mutated_path,
            config_path=quality_config_path,
            stage_dir=stage_a.root,
            mode="incremental",
            expected_base_version=1,
        )
    except WorkbenchError as exc:
        stale_error = exc
    current_after_stale = Stage(stage_a.root).current_manifest()
    checks.append(
        _check(
            "stale_version_cannot_roll_back",
            stale_error is not None
            and stale_error.code == "E_STALE_VERSION"
            and current_after_stale is not None
            and current_after_stale["version"] == quality_incremental["version"],
            error=stale_error.to_dict() if stale_error else None,
            current_version=current_after_stale["version"] if current_after_stale else None,
        )
    )

    final_selfcheck = ensure_selfcheck(stage_a.root)
    checks.append(
        _check(
            "final_cross_module_selfcheck",
            final_selfcheck["ok"],
            selfcheck=final_selfcheck,
        )
    )

    dashboard_path = write_dashboard(stage_a.root, workspace / "artifacts" / "dashboard.html")
    verification = {
        "schema_version": 1,
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "generation": generated_a,
        "final_stage": str(stage_a.root),
        "final_manifest": _manifest_summary(quality_incremental),
        "dashboard": str(dashboard_path),
        "recent_errors": Stage(stage_a.root).recent_errors(),
    }
    report_path = workspace / "artifacts" / "verification.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(canonical_bytes(verification))
    return verification
