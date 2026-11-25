from __future__ import annotations

from typing import Any

import numpy as np

from .errors import WorkbenchError
from .geometry import build_mask_model
from .hashing import array_digest, canonical_hash
from .imaging import generate_image, image_hash, text_condition_vector
from .numeric import (
    PrecisionContext,
    error_statistics,
    first_difference,
    recompute_stage_diagnostics,
)
from .operators import (
    build_region_plan,
    completion_kernel,
    fusion_kernel,
    style_kernel,
    subset_plan,
)
from .tasks import validate_task


def prepare_bundle(task: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    validation = validate_task(task, params)
    image = generate_image(int(task["image_seed"]), task["image_shape"])
    mask_model = build_mask_model(task["mask"], task["image_shape"], image)
    text = text_condition_vector(
        str(task["prompt"]),
        params.get("text_vocab", []),
        int(params.get("text_dim", 24)),
    )
    return {
        "task": task,
        "params": params,
        "image": image,
        "image_hash": image_hash(image),
        "mask_model": mask_model,
        "text": text,
        "matrix": validation["color_matrix"],
    }


def _profile_for(task: dict[str, Any], params: dict[str, Any], profile_name: str | None = None) -> PrecisionContext:
    if profile_name is not None:
        return PrecisionContext(
            profile_name,
            float(task.get("quant_scale", params.get("quantization", {}).get("default_quant_scale", 128))),
            str(task.get("rounding_mode", params.get("quantization", {}).get("rounding_mode", "nearest"))),
        )
    profile = task.get("dtype_profile", "float64")
    if isinstance(profile, dict):
        return PrecisionContext(
            str(profile.get("kind", "fixed")),
            float(profile.get("quant_scale", task.get("quant_scale", 128))),
            str(profile.get("rounding_mode", task.get("rounding_mode", "nearest"))),
        )
    return PrecisionContext(
        str(profile),
        float(task.get("quant_scale", params.get("quantization", {}).get("default_quant_scale", 128))),
        str(task.get("rounding_mode", params.get("quantization", {}).get("rounding_mode", "nearest"))),
    )


def execute_full(bundle: dict[str, Any], pc: PrecisionContext) -> dict[str, Any]:
    task = bundle["task"]
    params = bundle["params"]
    image = bundle["image"]
    matrix = bundle["matrix"]
    mask_model = bundle["mask_model"]
    iterations = int(task["iterations"])
    strength = float(task["style_strength"])
    neighbors = int(params.get("boundary_neighbors", 8))
    feather = float(params.get("fusion_feather", 2.0))
    epsilon = float(params.get("numeric_epsilon", 1e-12))

    output = image.astype(np.float64).copy()
    region_results: list[dict[str, Any]] = []
    concat: dict[str, list[np.ndarray]] = {
        "sampled": [], "completed": [], "styled": [], "quantized": [], "fused": []
    }

    for region in mask_model["regions"]:
        plan = build_region_plan(region, neighbors, feather, epsilon, pc)
        completed, source_contrib = completion_kernel(plan, iterations, pc)
        styled, color_contrib = style_kernel(completed, plan, matrix, strength, pc)
        quantized = pc.finalize(styled)
        fused = fusion_kernel(image, plan["coords"], quantized, plan["alpha"], pc)
        output[plan["coords"][:, 0], plan["coords"][:, 1]] = fused

        concat["sampled"].append(np.asarray(plan["sampled"], dtype=np.float64))
        concat["completed"].append(np.asarray(completed, dtype=np.float64))
        concat["styled"].append(np.asarray(styled, dtype=np.float64))
        concat["quantized"].append(quantized)
        concat["fused"].append(np.asarray(fused, dtype=np.float64))
        region_results.append({
            "region_id": plan["region_id"],
            "plan": plan,
            "completed": completed,
            "source_contrib": source_contrib,
            "styled": styled,
            "color_contrib": color_contrib,
            "quantized": quantized,
            "fused": fused,
        })

    arrays = {key: np.concatenate(parts, axis=0) if parts else np.empty((0, 3))
              for key, parts in concat.items()}
    return {"output": output, "regions": region_results, "arrays": arrays, "pc": pc}


def _block_keys(coords: np.ndarray, block_size: int) -> np.ndarray:
    by = coords[:, 0] // int(block_size)
    bx = coords[:, 1] // int(block_size)
    return np.stack([by, bx], axis=1)


def execute_blocks(
    bundle: dict[str, Any],
    pc: PrecisionContext,
    block_size: int,
    full_execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task = bundle["task"]
    params = bundle["params"]
    image = bundle["image"]
    matrix = bundle["matrix"]
    mask_model = bundle["mask_model"]
    iterations = int(task["iterations"])
    strength = float(task["style_strength"])
    neighbors = int(params.get("boundary_neighbors", 8))
    feather = float(params.get("fusion_feather", 2.0))
    epsilon = float(params.get("numeric_epsilon", 1e-12))

    output = image.astype(np.float64).copy()
    block_records: list[dict[str, Any]] = []
    region_arrays: dict[str, dict[str, np.ndarray]] = {}

    for region_index, region in enumerate(mask_model["regions"]):
        plan = build_region_plan(region, neighbors, feather, epsilon, pc)
        pixel_count = plan["pixel_count"]
        completed_all = np.empty((pixel_count, 3), dtype=np.float64)
        styled_all = np.empty((pixel_count, 3), dtype=np.float64)
        quantized_all = np.empty((pixel_count, 3), dtype=np.float64)
        fused_all = np.empty((pixel_count, 3), dtype=np.float64)
        keys = _block_keys(plan["coords"], block_size)
        unique_keys = np.unique(keys, axis=0)

        for key_row in unique_keys:
            block_y, block_x = int(key_row[0]), int(key_row[1])
            selected = np.where((keys[:, 0] == block_y) & (keys[:, 1] == block_x))[0]
            subplan = subset_plan(plan, selected)
            completed, _ = completion_kernel(subplan, iterations, pc)
            styled, _ = style_kernel(completed, subplan, matrix, strength, pc)
            quantized = pc.finalize(styled)
            fused = fusion_kernel(image, subplan["coords"], quantized, subplan["alpha"], pc)

            completed_all[selected] = np.asarray(completed, dtype=np.float64)
            styled_all[selected] = np.asarray(styled, dtype=np.float64)
            quantized_all[selected] = quantized
            fused_all[selected] = fused
            output[subplan["coords"][:, 0], subplan["coords"][:, 1]] = fused

            full_slice = None
            if full_execution is not None:
                full_region = full_execution["regions"][region_index]
                full_slice = {
                    "completed": full_region["completed"][selected],
                    "styled": full_region["styled"][selected],
                    "fused": full_region["fused"][selected],
                }
            block_diff = None
            if full_slice is not None:
                diffs = []
                for stage, block_value, full_value in (
                    ("completion", completed_all[selected], full_slice["completed"]),
                    ("style", styled_all[selected], full_slice["styled"]),
                    ("fusion", fused_all[selected], full_slice["fused"]),
                ):
                    mismatch = first_difference(block_value, full_value)
                    if mismatch is not None:
                        diffs.append({"stage": stage, **mismatch})
                block_diff = diffs[0] if diffs else None

            y0, x0 = block_y * block_size, block_x * block_size
            block_records.append({
                "block_id": f"B{block_y:04d}_{block_x:04d}_{region['region_id']}",
                "tile_yx": [block_y, block_x],
                "bbox_yx": [y0, x0, y0 + block_size, x0 + block_size],
                "region_id": region["region_id"],
                "pixel_count": int(selected.size),
                "pixel_indices": selected.tolist(),
                "global_stats_source": region["region_id"],
                "color_stats": plan["stats_digest_inputs"],
                "color_stats_hash": canonical_hash(plan["stats_digest_inputs"]),
                "weights_hash": array_digest(plan["weights"]),
                "alpha_hash": array_digest(plan["alpha"]),
                "first_difference": block_diff,
                "exact_match": block_diff is None,
            })

        region_arrays[region["region_id"]] = {
            "completed": completed_all,
            "styled": styled_all,
            "quantized": quantized_all,
            "fused": fused_all,
        }

    return {"output": output, "blocks": block_records, "regions": region_arrays}


def _spectrum(actual: np.ndarray, reference: np.ndarray) -> list[dict[str, Any]]:
    flat = np.abs(np.asarray(actual, dtype=np.float64) - np.asarray(reference, dtype=np.float64)).reshape(-1)
    if flat.size == 0:
        return []
    bands = [
        ("exact_zero", lambda d: d == 0.0),
        ("(0,1e-12]", lambda d: (d > 0.0) & (d <= 1e-12)),
        ("(1e-12,1e-8]", lambda d: (d > 1e-12) & (d <= 1e-8)),
        ("(1e-8,1e-5]", lambda d: (d > 1e-8) & (d <= 1e-5)),
        ("(1e-5,1e-3]", lambda d: (d > 1e-5) & (d <= 1e-3)),
        ("(1e-3,1e-1]", lambda d: (d > 1e-3) & (d <= 1e-1)),
        ("(1e-1,1]", lambda d: (d > 1e-1) & (d <= 1.0)),
        ("(1,inf)", lambda d: d > 1.0),
    ]
    return [{"band": name, "count": int(np.count_nonzero(predicate(flat)))} for name, predicate in bands]


def compare_full_block(
    bundle: dict[str, Any],
    full: dict[str, Any],
    blocked: dict[str, Any],
) -> dict[str, Any]:
    task = bundle["task"]
    mask_model = bundle["mask_model"]
    first_mismatch: dict[str, Any] | None = None
    stage_checks: dict[str, Any] = {}

    for index, region in enumerate(mask_model["regions"]):
        rid = region["region_id"]
        full_region = full["regions"][index]
        block_region = blocked["regions"][rid]
        for stage in ("completed", "styled", "fused"):
            mismatch = first_difference(block_region[stage], full_region[stage], region["pixel_coords"])
            exact = mismatch is None
            stage_checks[f"{rid}:{stage}"] = exact
            if not exact and first_mismatch is None:
                first_mismatch = {"region_id": rid, "stage": stage, **(mismatch or {})}

    output_mismatch = first_difference(blocked["output"], full["output"])
    output_exact = output_mismatch is None
    if not output_exact and first_mismatch is None:
        first_mismatch = {"region_id": None, "stage": "full_output", **(output_mismatch or {})}

    byte_identical = (
        array_digest(blocked["output"]) == array_digest(full["output"])
        and all(stage_checks.values())
        and output_exact
    )
    code = "OK" if byte_identical else "E_BLOCK_MISMATCH"
    completion_spectrum = _spectrum(
        np.concatenate([blocked["regions"][r["region_id"]]["completed"] for r in mask_model["regions"]]),
        full["arrays"]["completed"],
    )
    return {
        "code": code,
        "ok": byte_identical,
        "byte_identical": byte_identical,
        "stage_checks": stage_checks,
        "first_mismatch": first_mismatch,
        "output_hash_full": array_digest(full["output"]),
        "output_hash_block": array_digest(blocked["output"]),
        "output_uint8_hash_full": array_digest(
            np.clip(np.rint(full["output"]), 0, 255).astype(np.uint8)
        ),
        "output_uint8_hash_block": array_digest(
            np.clip(np.rint(blocked["output"]), 0, 255).astype(np.uint8)
        ),
        "block_deviation_spectrum": completion_spectrum,
        "block_count": len(blocked["blocks"]),
        "blocks": blocked["blocks"],
    }


def _profile_diagnostics(
    bundle: dict[str, Any],
    reference: dict[str, Any],
    profile_result: dict[str, Any],
    pc: PrecisionContext,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    ref_arrays = reference["arrays"]
    act_arrays = profile_result["arrays"]
    details = {
        "actual_boundary_sampling": act_arrays["sampled"],
        "ref_boundary_sampling": ref_arrays["sampled"],
        "actual_block_completion": act_arrays["completed"],
        "ref_block_completion": ref_arrays["completed"],
        "actual_style_shading": act_arrays["styled"],
        "ref_style_shading": ref_arrays["styled"],
        "actual_fixed_quantization": pc.finalize(act_arrays["styled"]),
        "ref_fixed_quantization": ref_arrays["styled"],
        "actual_boundary_fusion": act_arrays["fused"],
        "ref_boundary_fusion": ref_arrays["fused"],
    }
    diagnostic = recompute_stage_diagnostics(details)
    diagnostic["profile"] = pc.profile
    diagnostic["quant_scale"] = pc.quant_scale if pc.profile == "fixed" else None
    diagnostic["rounding_mode"] = pc.rounding_mode if pc.profile == "fixed" else None
    diagnostic["output_rmse_vs_float64"] = error_statistics(
        profile_result["output"], reference["output"], mask=bundle["mask_model"]["union_mask"]
    )
    return diagnostic, details


def aggregate_metrics(bundle: dict[str, Any], full: dict[str, Any], block_match: dict[str, Any]) -> dict[str, Any]:
    image = bundle["image"].astype(np.float64)
    output = full["output"]
    union = bundle["mask_model"]["union_mask"]
    edit_stats = error_statistics(output, image, mask=union)
    output_uint8 = np.clip(np.rint(output), 0, 255).astype(np.uint8)
    changed = int(np.count_nonzero(np.any(output_uint8 != bundle["image"], axis=2) & union))

    seam_values = []
    seam_coords = bundle["mask_model"]["regions"][0]["boundary_coords"] if bundle["mask_model"]["regions"] else np.empty((0, 2))
    for y, x in seam_coords:
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ny, nx = int(y) + dy, int(x) + dx
            if 0 <= ny < image.shape[0] and 0 <= nx < image.shape[1] and not union[ny, nx]:
                seam_values.append(np.mean(np.abs(output[int(y), int(x)] - output[ny, nx])))
    seam_cost = float(np.mean(seam_values)) if seam_values else 0.0
    return {
        "edit_mae": edit_stats["mean_abs"],
        "edit_rmse": edit_stats["rmse"],
        "edit_relative_rmse": edit_stats["relative_rmse"],
        "changed_mask_pixels_uint8": changed,
        "mask_pixel_count": int(np.count_nonzero(union)),
        "mean_boundary_seam_cost": seam_cost,
        "block_match_ok": bool(block_match["ok"]),
        "business_code": block_match["code"],
        "text_digest": bundle["text"]["digest"],
        "text_terms": bundle["text"]["terms"],
        "unknown_prompt_terms": bundle["text"]["unknown_terms"],
    }


def run_edit(
    task: dict[str, Any],
    params: dict[str, Any],
    *,
    block_size_override: int | None = None,
    profiles: list[str] | tuple[str, ...] = ("float64", "float32", "fixed"),
) -> dict[str, Any]:
    """Run validation, canonical full path, block path and precision diagnostics."""
    edit_id = str(task.get("edit_id", "<unknown>"))
    try:
        bundle = prepare_bundle(task, params)
    except WorkbenchError as exc:
        exc.details.setdefault("edit_id", edit_id)
        return {"edit_id": edit_id, "ok": False, **exc.to_dict()}

    block_size = int(block_size_override or task["block_size"])
    reference_pc = PrecisionContext("float64")
    reference = execute_full(bundle, reference_pc)
    blocked = execute_blocks(bundle, reference_pc, block_size, reference)
    block_match = compare_full_block(bundle, reference, blocked)
    metrics = aggregate_metrics(bundle, reference, block_match)

    precision_reports: dict[str, Any] = {}
    detail_arrays: dict[str, dict[str, np.ndarray]] = {}
    for profile_name in profiles:
        task_pc = _profile_for(task, params)
        if profile_name == "task":
            pc = task_pc
            profile_key = task_pc.profile
        else:
            pc = PrecisionContext(
                profile_name,
                float(task.get("quant_scale", params.get("quantization", {}).get("default_quant_scale", 128))),
                str(task.get("rounding_mode", params.get("quantization", {}).get("rounding_mode", "nearest"))),
            )
            profile_key = profile_name
        profile_full = execute_full(bundle, pc)
        diagnostic, details = _profile_diagnostics(bundle, reference, profile_full, pc)
        precision_reports[profile_key] = diagnostic
        detail_arrays[profile_key] = details

    output_uint8 = np.clip(np.rint(reference["output"]), 0, 255).astype(np.uint8)
    summary = {
        "edit_id": edit_id,
        "ok": block_match["ok"],
        "code": block_match["code"],
        "revision": int(task["revision"]),
        "image_seed": int(task["image_seed"]),
        "image_hash": bundle["image_hash"],
        "mask_digest": bundle["mask_model"]["mask_digest"],
        "region_count": bundle["mask_model"]["region_count"],
        "block_size": block_size,
        "requested_block_size": int(task["block_size"]),
        "block_count": block_match["block_count"],
        "selected_dtype_profile": _profile_for(task, params).profile,
        "metrics": metrics,
        "block_match": {
            key: value for key, value in block_match.items() if key not in {"blocks"}
        },
        "blocks": block_match["blocks"],
        "precision": precision_reports,
        "hashes": {
            "float64_output": block_match["output_hash_full"],
            "block_output": block_match["output_hash_block"],
            "uint8_output": array_digest(output_uint8),
            "mask": bundle["mask_model"]["mask_digest"],
            "text": bundle["text"]["digest"],
        },
        "cache_manifest_inputs": {
            "image_hash": bundle["image_hash"],
            "mask_digest": bundle["mask_model"]["mask_digest"],
            "style_strength": float(task["style_strength"]),
            "iterations": int(task["iterations"]),
            "block_size": int(task["block_size"]),
            "dtype_profile": str(task["dtype_profile"]),
            "revision": int(task["revision"]),
            "color_matrix_hash": canonical_hash(params.get("color_matrix")),
        },
    }
    if not block_match["ok"]:
        summary["code"] = "E_BLOCK_MISMATCH"
        summary["ok"] = False
    return {
        "summary": summary,
        "bundle": bundle,
        "reference": reference,
        "blocked": blocked,
        "block_match": block_match,
        "detail_arrays": detail_arrays,
        "output_uint8": output_uint8,
    }
