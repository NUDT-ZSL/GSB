from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .engine import PipelineEngine, PipelineResult
from .state import StateStore
from .util import atomic_write_json, read_json


def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(target)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _tile_entry(index: dict[str, Any], row: int, col: int) -> dict[str, Any]:
    for tile in index.get("tiles", []):
        if tile.get("row") == row and tile.get("col") == col:
            return tile
    raise KeyError(f"tile r{row}_c{col} not found")


def _make_patch_from_deltas(
    workspace: Path,
    index: dict[str, Any],
    operation: dict[str, Any],
) -> dict[str, Any]:
    row, col = operation["tile"]
    x0, y0, x1, y1 = operation["bbox"]
    tile = _tile_entry(index, row, col)
    with Image.open(workspace / tile["path"]) as image:
        source = np.asarray(image, dtype=np.uint8).copy()
    if source.shape[0] < y1 or source.shape[1] < x1:
        raise ValueError("sample patch bbox is outside source tile")
    band_order = tile.get("band_order", index.get("band_order", ["red", "green", "nir", "elevation"]))
    deltas = operation.get("band_deltas", [0.0, 0.0, 0.0, 0.0])
    if len(deltas) != 4:
        raise ValueError("band_deltas must have four values")
    crop = source[y0:y1, x0:x1]
    patch_bands: list[list[list[int]]] = []
    for band_index in range(4):
        changed = crop[:, :, band_index].astype(np.float64) + float(deltas[band_index]) * 255.0
        changed = np.clip(np.rint(changed), 0, 255).astype(np.uint8)
        patch_bands.append(changed.tolist())
    return {
        "id": str(operation.get("id", f"patch-r{row}-c{col}")),
        "tile": [row, col],
        "bbox": [x0, y0, x1, y1],
        "band_order": band_order,
        "bands": patch_bands,
    }


def apply_edit(workspace: str | Path, step: dict[str, Any]) -> dict[str, Any]:
    workspace = Path(workspace)
    config_path = workspace / "config.json"
    index_path = workspace / "tiles.json"
    overlay_path = workspace / "overlay.json"
    config = read_json(config_path)
    index = read_json(index_path)
    overlay = read_json(overlay_path)

    if isinstance(step.get("config_patch"), dict):
        config = _deep_merge(config, step["config_patch"])
    if isinstance(step.get("index_patch"), dict):
        patch = copy.deepcopy(step["index_patch"])
        add_neighbors = patch.pop("add_neighbors", [])
        propagate_band_version = patch.get("band_version")
        propagate_band_order = patch.get("band_order")
        index = _deep_merge(index, patch)
        if propagate_band_version is not None or propagate_band_order is not None:
            for tile in index.get("tiles", []):
                if propagate_band_version is not None:
                    tile["band_version"] = propagate_band_version
                if propagate_band_order is not None:
                    tile["band_order"] = copy.deepcopy(propagate_band_order)
        for source_row, source_col, target_row, target_col in add_neighbors:
            source = _tile_entry(index, source_row, source_col)
            target = _tile_entry(index, target_row, target_col)
            pair = [target_row, target_col]
            if pair not in source.setdefault("neighbors", []):
                source["neighbors"].append(pair)
                source["neighbors"].sort()
            reverse = [source_row, source_col]
            if reverse not in target.setdefault("neighbors", []):
                target["neighbors"].append(reverse)
                target["neighbors"].sort()

    if "overlay_ops" in step or "invalid_overlay_ops" in step:
        patches: list[dict[str, Any]] = []
        mode = step.get("overlay_mode", "replace")
        if mode == "merge":
            patches.extend(copy.deepcopy(overlay.get("patches", [])))
        for operation in step.get("overlay_ops", []):
            patches.append(_make_patch_from_deltas(workspace, index, operation))
        for operation in step.get("invalid_overlay_ops", []):
            patches.append(copy.deepcopy(operation))
        overlay = {"schema_version": 1, "patches": patches}

    atomic_write_json(config_path, config)
    atomic_write_json(index_path, index)
    atomic_write_json(overlay_path, overlay)
    return {
        "name": step.get("name", "edit"),
        "config_hash_file": str(config_path),
        "index_version": index.get("index_version"),
        "band_version": index.get("band_version"),
        "overlay_patches": len(overlay.get("patches", [])),
    }


def _compare_results(incremental: PipelineResult, full: PipelineResult) -> dict[str, Any]:
    category_equal = bool(
        incremental.global_classes.shape == full.global_classes.shape
        and np.array_equal(incremental.global_classes, full.global_classes)
    )
    common = sorted(set(incremental.tile_results) & set(full.tile_results))
    score_mismatches: list[str] = []
    class_mismatches: list[str] = []
    key_mismatches: list[str] = []
    for tile_id in common:
        inc = incremental.tile_results[tile_id]
        ref = full.tile_results[tile_id]
        if not np.array_equal(inc.classes, ref.classes):
            class_mismatches.append(tile_id)
        if not np.array_equal(inc.scores, ref.scores):
            score_mismatches.append(tile_id)
        if inc.cache_key != ref.cache_key:
            key_mismatches.append(tile_id)
    missing = sorted(set(incremental.tile_results) ^ set(full.tile_results))
    return {
        "category_equal": category_equal,
        "tile_class_equal": not class_mismatches and not missing,
        "tile_score_equal": not score_mismatches and not missing,
        "tile_key_equal": not key_mismatches and not missing,
        "class_mismatches": class_mismatches,
        "score_mismatches": score_mismatches,
        "key_mismatches": key_mismatches,
        "tile_set_mismatch": missing,
    }


def run_edit_sequence(
    workspace: str | Path,
    state_dir: str | Path,
    edits_path: str | Path,
    *,
    report_path: str | Path,
    max_batches_per_edit: int = 100,
    reset_state: bool = True,
) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    state_dir = Path(state_dir).resolve()
    edits = read_json(edits_path)
    steps = edits.get("steps", edits if isinstance(edits, list) else [])
    if not isinstance(steps, list) or not steps:
        raise ValueError("edit sequence contains no steps")
    if reset_state and state_dir.exists():
        shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "schema_version": 1,
        "workspace": str(workspace),
        "state_dir": str(state_dir),
        "steps": [],
        "all_equal": True,
    }
    for index, step in enumerate(steps):
        applied = apply_edit(workspace, step)
        incremental_runs: list[PipelineResult] = []
        for _ in range(max_batches_per_edit):
            run = PipelineEngine(
                workspace,
                state_dir,
                mode="incremental",
                max_batches=1,
            ).run()
            incremental_runs.append(run)
            if run.converged:
                break
        else:
            raise RuntimeError(f"incremental state did not converge after step {step.get('name')}")
        incremental = incremental_runs[-1]
        full_state = state_dir.parent / f".full-{state_dir.name}-step-{index + 1}"
        if full_state.exists():
            shutil.rmtree(full_state)
        full = PipelineEngine(workspace, full_state, mode="full", max_batches=1).run()
        shutil.rmtree(full_state, ignore_errors=True)
        comparison = _compare_results(incremental, full)
        equal = all(
            comparison[key]
            for key in ("category_equal", "tile_class_equal", "tile_score_equal", "tile_key_equal")
        )
        report["all_equal"] = report["all_equal"] and equal

        snapshot = StateStore(state_dir).load_snapshot()
        if snapshot is None:
            raise RuntimeError("incremental snapshot failed checksum/manifest validation")
        step_report = {
            "step_index": index + 1,
            "name": step.get("name", applied["name"]),
            "applied": applied,
            "incremental_calls": len(incremental_runs),
            "batch_snapshot_versions": [run.snapshot_version for run in incremental_runs],
            "batch_codes": [run.business_codes for run in incremental_runs],
            "pending_observed": any(
                "E_RECOMPUTE_BUDGET_PENDING" in run.business_codes for run in incremental_runs
            ),
            "final_snapshot_version": incremental.snapshot_version,
            "snapshot_manifest_sha256": snapshot.manifest_digest,
            "snapshot_manifest_valid": True,
            "area_stats": incremental.report["area_stats"],
            "comparison": comparison,
            "incremental_equal_to_full": equal,
        }
        report["steps"].append(step_report)
        print(
            f"step {index + 1}/{len(steps)} {step_report['name']}: "
            f"calls={len(incremental_runs)} equal={equal} "
            f"pending_observed={step_report['pending_observed']}"
        )
    atomic_write_json(report_path, report)
    return report
