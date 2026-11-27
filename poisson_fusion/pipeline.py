"""End-to-end full fusion and incremental replacement pipelines."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from .errors import FusionError
from .field_builder import FieldBundle, build_fields, save_fields
from .geometry import (
    bounding_box,
    inner_boundary,
    make_band,
    region_statistics,
    validate_mask,
)
from .io_utils import (
    array_descriptor,
    array_sha256,
    image_to_rgb_or_rgba,
    load_image,
    load_mask,
    save_json,
    save_npy,
    sha256_file,
    write_png,
)
from .logging_utils import log_record
from .metrics import build_metrics, quantize_rgb
from .sample_gen import generate_sample
from .solver import SolveResult, solve_poisson
from .visualize import mask_png, save_review_images


@dataclass(frozen=True)
class FusionParameters:
    tolerance: float = 1e-8
    max_iterations: int = 8000
    band_radius: int = 3
    foreground_mode: str = "auto"
    background_color: tuple[float, float, float] | None = None
    alpha_threshold: float = 1.0
    solid_threshold: float = 8.0

    def __post_init__(self) -> None:
        if self.tolerance <= 0:
            raise ValueError("tolerance must be positive")
        if self.max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        if self.band_radius < 0:
            raise ValueError("band_radius must be non-negative")
        if self.foreground_mode not in {"auto", "alpha", "solid"}:
            raise ValueError("foreground_mode must be auto, alpha or solid")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data["background_color"] is None:
            data["background_color"] = None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FusionParameters":
        allowed = {
            "tolerance",
            "max_iterations",
            "band_radius",
            "foreground_mode",
            "background_color",
            "alpha_threshold",
            "solid_threshold",
        }
        return cls(**{key: value for key, value in data.items() if key in allowed})


def _base_array(array: np.ndarray) -> np.ndarray:
    rgb = image_to_rgb_or_rgba(array, "base")
    if rgb.shape[2] == 4:
        rgb = rgb[:, :, :3]
    return rgb


def _file_input_summary(paths: dict[str, str | Path]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name, value in paths.items():
        path = Path(value).resolve()
        entry: dict[str, Any] = {"path": str(path)}
        if path.exists() and path.is_file():
            entry["file_sha256"] = sha256_file(path)
            entry["bytes"] = path.stat().st_size
        summary[name] = entry
    return summary


def _load_inputs(
    base_path: str | Path,
    foreground_path: str | Path,
    mask_path: str | Path,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    base_raw = load_image(base_path)
    foreground_raw = load_image(foreground_path)
    mask_raw = load_image(mask_path)
    base = _base_array(base_raw)
    foreground = image_to_rgb_or_rgba(foreground_raw, "foreground")
    from .io_utils import mask_from_array

    mask = mask_from_array(mask_raw)
    if foreground.shape[:2] != base.shape[:2] or mask.shape != base.shape[:2]:
        raise FusionError(
            "E_DIMENSIONS",
            "底板、前景图与掩码尺寸不一致",
            {
                "base_shape": list(base.shape),
                "foreground_shape": list(foreground.shape),
                "mask_shape": list(mask.shape),
            },
        )
    input_summary = {
        "base": {
            "path": str(Path(base_path).resolve()),
            "file_sha256": sha256_file(base_path),
            "array": array_descriptor(base),
        },
        "foreground": {
            "path": str(Path(foreground_path).resolve()),
            "file_sha256": sha256_file(foreground_path),
            "array": array_descriptor(foreground),
        },
        "mask": {
            "path": str(Path(mask_path).resolve()),
            "file_sha256": sha256_file(mask_path),
            "array": array_descriptor(mask.astype(np.uint8)),
        },
    }
    return base, foreground, mask, input_summary


def _solve_full(
    base: np.ndarray,
    foreground: np.ndarray,
    mask: np.ndarray,
    params: FusionParameters,
) -> tuple[np.ndarray, FieldBundle, SolveResult]:
    validate_mask(mask, base.shape[:2])
    bundle = build_fields(
        base,
        foreground,
        mask,
        foreground_mode=params.foreground_mode,
        background_color=params.background_color,
        alpha_threshold=params.alpha_threshold,
        solid_threshold=params.solid_threshold,
    )
    solved = solve_poisson(
        base,
        bundle,
        tolerance=params.tolerance,
        max_iterations=params.max_iterations,
    )
    return solved.image, bundle, solved


def _output_entry(path: Path) -> dict[str, Any]:
    return {"path": str(path.resolve()), "file_sha256": sha256_file(path)}


def _write_common_outputs(
    output_dir: Path,
    *,
    fused_u8: np.ndarray,
    fused_float: np.ndarray,
    bundle: FieldBundle,
    metrics: dict[str, Any],
    metric_arrays: dict[str, np.ndarray],
    updated_region: np.ndarray,
    run_type: str,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    save_npy(output_dir / "fused_u8.npy", fused_u8)
    save_npy(output_dir / "fused_float.npy", fused_float.astype(np.float32))
    save_npy(output_dir / "updated_region_mask.npy", updated_region.astype(np.uint8))
    save_npy(output_dir / "target_mask.npy", (bundle.unknown_mask | bundle.boundary_mask).astype(np.uint8))
    save_npy(output_dir / "boundary_mask.npy", bundle.boundary_mask.astype(np.uint8))
    save_npy(output_dir / "boundary_band.npy", metric_arrays["fusion_band"].astype(np.uint8))
    for name, array in metric_arrays.items():
        save_npy(output_dir / f"{name}.npy", array)
    boundary = bundle.boundary_mask
    band = metric_arrays["fusion_band"].astype(bool)
    visual_outputs = save_review_images(
        output_dir,
        fused_u8=fused_u8,
        updated_region=updated_region,
        boundary=boundary,
        band=band,
        seam_error_map=metric_arrays["seam_error_map"],
        gradient_error_map=metric_arrays["gradient_error_map"],
    )
    outputs: dict[str, Any] = {
        "fused_png": str((output_dir / visual_outputs["fused"]).resolve()),
        "fused_u8_npy": str((output_dir / "fused_u8.npy").resolve()),
        "fused_float_npy": str((output_dir / "fused_float.npy").resolve()),
        "updated_region_png": str((output_dir / visual_outputs["updated_region"]).resolve()),
        "updated_region_npy": str((output_dir / "updated_region_mask.npy").resolve()),
        "updated_region_overlay_png": str(
            (output_dir / visual_outputs["updated_region_overlay"]).resolve()
        ),
        "seam_error_map_npy": str((output_dir / "seam_error_map.npy").resolve()),
        "seam_error_heatmap_png": str((output_dir / visual_outputs["seam_error_heatmap"]).resolve()),
        "gradient_error_map_npy": str((output_dir / "gradient_error_map.npy").resolve()),
        "gradient_error_heatmap_png": str(
            (output_dir / visual_outputs["gradient_error_heatmap"]).resolve()
        ),
        "boundary_band_png": str((output_dir / visual_outputs["boundary_band"]).resolve()),
        "boundary_mask_png": str((output_dir / visual_outputs["boundary_mask"]).resolve()),
        "boundary_band_npy": str((output_dir / "boundary_band.npy").resolve()),
        "boundary_mask_npy": str((output_dir / "boundary_mask.npy").resolve()),
        "target_mask_npy": str((output_dir / "target_mask.npy").resolve()),
        "fields_manifest_json": str((output_dir / "fields" / "fields_manifest.json").resolve()),
    }
    return outputs


def _run_id(input_summary: dict[str, Any], params: FusionParameters, run_type: str) -> str:
    mask_entry = input_summary.get("mask") or input_summary.get("new_mask")
    parts = [
        run_type,
        input_summary["base"]["array"]["sha256"],
        input_summary["foreground"]["array"]["sha256"],
        mask_entry["array"]["sha256"],
        str(sorted(params.to_dict().items())),
    ]
    return array_sha256(np.frombuffer("|".join(parts).encode("utf-8"), dtype=np.uint8))


def fuse_image_files(
    base_path: str | Path,
    foreground_path: str | Path,
    mask_path: str | Path,
    output_dir: str | Path,
    *,
    params: FusionParameters | None = None,
    log_path: str | Path | None = None,
    seed_hint: int | None = None,
) -> dict[str, Any]:
    """Run a full Poisson fusion from three input files and persist all artifacts."""

    params = params or FusionParameters()
    destination = Path(output_dir).resolve()
    input_summary: dict[str, Any] | None = _file_input_summary(
        {
            "base": base_path,
            "foreground": foreground_path,
            "mask": mask_path,
        }
    )
    try:
        base, foreground, mask, input_summary = _load_inputs(base_path, foreground_path, mask_path)
        run_id = _run_id(input_summary, params, "full")
        fused_float, bundle, solved = _solve_full(base, foreground, mask, params)
        fused_u8 = quantize_rgb(fused_float)
        base_u8 = quantize_rgb(base)
        metrics, metric_arrays = build_metrics(
            fused_u8, base_u8, bundle, band_radius=params.band_radius
        )
        fields_dir = destination / "fields"
        save_fields(bundle, fields_dir)
        outputs = _write_common_outputs(
            destination,
            fused_u8=fused_u8,
            fused_float=fused_float,
            bundle=bundle,
            metrics=metrics,
            metric_arrays=metric_arrays,
            updated_region=mask,
            run_type="full",
        )
        mask_path_resolved = str(Path(mask_path).resolve())
        report = {
            "schema_version": 1,
            "ok": True,
            "run_id": run_id,
            "run_type": "full_fusion",
            "seed_hint": seed_hint,
            "inputs": input_summary,
            "parameters": params.to_dict(),
            "mask_validation": bundle.extraction["mask_diagnostics"],
            "foreground_extraction": bundle.extraction,
            "solver": solved.solver,
            "metrics": metrics,
            "updated_region": {
                **region_statistics(mask),
                "definition": "full target mask replaced by this run",
            },
            "mask_change": {
                "detected": False,
                "reason": "initial full fusion has no previous mask",
            },
            "outputs": outputs,
        }
        save_json(destination / "report.json", report)
        outputs["report_json"] = str((destination / "report.json").resolve())
        state = {
            "schema_version": 1,
            "run_type": "full",
            "run_id": run_id,
            "base_path": str(Path(base_path).resolve()),
            "base_file_sha256": input_summary["base"]["file_sha256"],
            "foreground_path": str(Path(foreground_path).resolve()),
            "mask_path": mask_path_resolved,
            "mask_sha256": array_sha256(mask.astype(np.uint8)),
            "fused_path": outputs["fused_u8_npy"],
            "fused_file_sha256": sha256_file(outputs["fused_u8_npy"]),
            "report_path": outputs["report_json"],
            "parameters": params.to_dict(),
        }
        save_json(destination / "state.json", state)
        outputs["state_json"] = str((destination / "state.json").resolve())
        result = {
            "ok": True,
            "run_id": run_id,
            "run_type": "full_fusion",
            "output_dir": str(destination),
            "metrics": metrics,
            "solver": solved.solver,
            "outputs": outputs,
        }
        log_record(
            log_path,
            {
                "event": "run",
                "status": "ok",
                "run_type": "full_fusion",
                "run_id": run_id,
                "seed_hint": seed_hint,
                "input_summary": input_summary,
                "parameters": params.to_dict(),
                "key_metrics": {
                    "seam_max": metrics["seam_error"]["max"],
                    "seam_mean": metrics["seam_error"]["mean"],
                    "gradient_p95": metrics["gradient_error"]["p95"],
                    "boundary_max_error": metrics["boundary_match"]["max_error"],
                },
                "output_dir": str(destination),
                "output_files": outputs,
            },
        )
        return result
    except FusionError as exc:
        destination.mkdir(parents=True, exist_ok=True)
        save_json(destination / "error.json", exc.to_dict())
        log_record(
            log_path,
            {
                "event": "run",
                "status": "error",
                "run_type": "full_fusion",
                "seed_hint": seed_hint,
                "input_summary": input_summary,
                "error": exc.to_dict()["error"],
                "output_dir": str(destination),
            },
        )
        raise


def _load_state(state_path: str | Path) -> tuple[dict[str, Any], Path]:
    path = Path(state_path)
    try:
        import json

        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise FusionError(
            "E_STATE",
            "无法读取增量状态文件",
            {"state_path": str(path), "reason": str(exc)},
        ) from exc
    required = ["base_path", "mask_path", "fused_path", "parameters"]
    missing = [key for key in required if key not in state]
    if missing:
        raise FusionError(
            "E_STATE",
            "增量状态文件缺少必要字段",
            {"state_path": str(path), "missing": missing},
        )
    return state, path


def _verify_incremental(
    incremental: np.ndarray,
    full: np.ndarray,
    previous_fused: np.ndarray,
    affected: np.ndarray,
) -> tuple[dict[str, Any], np.ndarray]:
    difference = np.abs(incremental.astype(np.int16) - full.astype(np.int16))
    pixel_difference = np.max(difference, axis=2)
    over = pixel_difference > 1
    if np.any(over):
        first_over = [int(v) for v in np.argwhere(over)[0, :2]]
    else:
        first_over = None
    if np.any(pixel_difference > 0):
        first_any = [int(v) for v in np.argwhere(pixel_difference > 0)[0]]
    else:
        first_any = None
    preserved = np.array_equal(incremental[~affected], previous_fused[~affected])
    verification = {
        "tolerance_levels": 1,
        "within_tolerance": not bool(np.any(over)),
        "max_abs_difference": int(np.max(pixel_difference)) if pixel_difference.size else 0,
        "pixels_compared": int(pixel_difference.size),
        "channel_values_compared": int(difference.size),
        "pixels_differing": int(np.count_nonzero(pixel_difference)),
        "channel_values_differing": int(np.count_nonzero(difference)),
        "pixels_over_tolerance": int(np.count_nonzero(over)),
        "first_difference_pixel": first_any,
        "first_over_tolerance_pixel": first_over,
        "unaffected_pixels_exactly_preserved": preserved,
        "comparison_definition": "incremental uint8 output versus full recomputation uint8 output",
    }
    return verification, difference.astype(np.uint8)


def replace_foreground_files(
    state_path: str | Path,
    foreground_path: str | Path,
    output_dir: str | Path,
    *,
    new_mask_path: str | Path | None = None,
    params_override: FusionParameters | None = None,
    log_path: str | Path | None = None,
    seed_hint: int | None = None,
    verify_full: bool = True,
) -> dict[str, Any]:
    """Replace a foreground, solve only the affected crop, and verify against a full solve."""

    destination = Path(output_dir).resolve()
    initial_paths: dict[str, str | Path] = {
        "previous_state": state_path,
        "foreground": foreground_path,
    }
    if new_mask_path is not None:
        initial_paths["new_mask"] = new_mask_path
    input_summary: dict[str, Any] | None = _file_input_summary(initial_paths)
    try:
        state, state_file = _load_state(state_path)
        base_path = Path(state["base_path"])
        old_mask_path = Path(state["mask_path"])
        old_fused_path = Path(state["fused_path"])
        old_mask = load_mask(old_mask_path)
        previous_fused = load_image(old_fused_path)
        previous_fused = image_to_rgb_or_rgba(previous_fused, "previous fused")
        if previous_fused.shape[2] == 4:
            previous_fused = previous_fused[:, :, :3]
        previous_u8 = quantize_rgb(previous_fused)
        base = _base_array(load_image(base_path))
        foreground = image_to_rgb_or_rgba(load_image(foreground_path), "foreground")
        new_mask = load_mask(new_mask_path) if new_mask_path else old_mask.copy()
        validate_mask(old_mask, base.shape[:2])
        validate_mask(new_mask, base.shape[:2])
        if foreground.shape[:2] != base.shape[:2]:
            raise FusionError(
                "E_DIMENSIONS",
                "新前景图与底板尺寸不一致",
                {"foreground_shape": list(foreground.shape), "base_shape": list(base.shape)},
            )
        if previous_u8.shape != base.shape or old_mask.shape != base.shape[:2]:
            raise FusionError(
                "E_STATE",
                "状态文件中的旧融合图或旧掩码尺寸与底板不一致",
                {
                    "previous_fused_shape": list(previous_u8.shape),
                    "base_shape": list(base.shape),
                    "old_mask_shape": list(old_mask.shape),
                },
            )
        base_state_hash = state.get("base_file_sha256")
        if base_state_hash and sha256_file(base_path) != base_state_hash:
            raise FusionError(
                "E_STATE",
                "底板文件与状态记录不一致",
                {"base_path": str(base_path), "expected_sha256": base_state_hash},
            )

        base_params = FusionParameters.from_dict(state["parameters"])
        params = params_override or base_params
        if params.foreground_mode == "auto":
            effective_mode = base_params.foreground_mode
            if effective_mode != "auto":
                params = replace(params, foreground_mode=effective_mode)

        affected = old_mask | new_mask
        old_only = old_mask & ~new_mask
        new_only = new_mask & ~old_mask
        common = old_mask & new_mask
        changed = not np.array_equal(old_mask, new_mask)
        affected_box = bounding_box(affected)
        if affected_box is None:
            raise FusionError("E_EMPTY_MASK", "替换区域为空", {"pixels": 0})

        # The full field/solve is retained only for explicit verification and
        # review.  The actual incremental solve uses a bounded crop.
        full_bundle = build_fields(
            base,
            foreground,
            new_mask,
            foreground_mode=params.foreground_mode,
            background_color=params.background_color,
            alpha_threshold=params.alpha_threshold,
            solid_threshold=params.solid_threshold,
        )
        full_solve = solve_poisson(
            base,
            full_bundle,
            tolerance=params.tolerance,
            max_iterations=params.max_iterations,
        )
        full_u8 = quantize_rgb(full_solve.image)

        crop_box = bounding_box(affected, margin=2)
        assert crop_box is not None
        y0, y1 = crop_box["y"], crop_box["y2"]
        x0, x1 = crop_box["x"], crop_box["x2"]
        base_crop = base[y0:y1, x0:x1]
        foreground_crop = foreground[y0:y1, x0:x1]
        new_mask_crop = new_mask[y0:y1, x0:x1]
        crop_bundle = build_fields(
            base_crop,
            foreground_crop,
            new_mask_crop,
            foreground_mode=params.foreground_mode,
            background_color=params.background_color,
            alpha_threshold=params.alpha_threshold,
            solid_threshold=params.solid_threshold,
        )
        crop_solve = solve_poisson(
            base_crop,
            crop_bundle,
            tolerance=params.tolerance,
            max_iterations=params.max_iterations,
        )
        crop_u8 = quantize_rgb(crop_solve.image)

        base_u8 = quantize_rgb(base)
        incremental_u8 = previous_u8.copy()
        incremental_u8[old_only] = base_u8[old_only]
        incremental_u8[new_mask] = crop_u8[new_mask_crop]
        incremental_float = previous_u8.astype(np.float64)
        incremental_float[old_only] = base[old_only]
        incremental_float[new_mask] = crop_solve.image[new_mask_crop]

        verification, difference = _verify_incremental(
            incremental_u8, full_u8, previous_u8, affected
        )
        if verify_full and not verification["within_tolerance"]:
            raise FusionError(
                "E_STATE",
                "增量结果与全量重算差异超过 1 个量化级",
                verification,
            )

        metrics, metric_arrays = build_metrics(
            incremental_u8,
            base_u8,
            full_bundle,
            band_radius=params.band_radius,
        )
        fields_dir = destination / "fields"
        save_fields(full_bundle, fields_dir)
        save_npy(destination / "full_recompute_u8.npy", full_u8)
        write_png(destination / "full_recompute.png", full_u8)
        save_npy(destination / "incremental_full_diff.npy", difference)
        difference_visual = np.max(difference, axis=2) * 255
        write_png(
            destination / "incremental_full_diff_heatmap.png",
            np.dstack([difference_visual] * 3),
        )
        outputs = _write_common_outputs(
            destination,
            fused_u8=incremental_u8,
            fused_float=incremental_float,
            bundle=full_bundle,
            metrics=metrics,
            metric_arrays=metric_arrays,
            updated_region=affected,
            run_type="replace",
        )
        outputs["full_recompute_png"] = str((destination / "full_recompute.png").resolve())
        outputs["full_recompute_npy"] = str((destination / "full_recompute_u8.npy").resolve())
        outputs["incremental_full_diff_npy"] = str(
            (destination / "incremental_full_diff.npy").resolve()
        )
        outputs["incremental_full_diff_heatmap_png"] = str(
            (destination / "incremental_full_diff_heatmap.png").resolve()
        )

        mask_change_files: dict[str, Any] = {}
        save_npy(destination / "mask_old.npy", old_mask.astype(np.uint8))
        save_npy(destination / "mask_new.npy", new_mask.astype(np.uint8))
        write_png(destination / "mask_old.png", mask_png(old_mask))
        write_png(destination / "mask_new.png", mask_png(new_mask))
        mask_change_files["mask_old"] = {
            "npy": str((destination / "mask_old.npy").resolve()),
            "png": str((destination / "mask_old.png").resolve()),
        }
        mask_change_files["mask_new"] = {
            "npy": str((destination / "mask_new.npy").resolve()),
            "png": str((destination / "mask_new.png").resolve()),
        }
        if changed:
            change_arrays = {
                "mask_old_only": old_only,
                "mask_new_only": new_only,
                "mask_change_xor": old_only | new_only,
                "mask_common": common,
            }
            for name, array in change_arrays.items():
                save_npy(destination / f"{name}.npy", array.astype(np.uint8))
                write_png(destination / f"{name}.png", mask_png(array))
                mask_change_files[name] = {
                    "npy": str((destination / f"{name}.npy").resolve()),
                    "png": str((destination / f"{name}.png").resolve()),
                }
        mask_change = {
            "detected": changed,
            "old_mask_pixels": int(np.count_nonzero(old_mask)),
            "new_mask_pixels": int(np.count_nonzero(new_mask)),
            "old_only": region_statistics(old_only),
            "new_only": region_statistics(new_only),
            "xor_pixels": int(np.count_nonzero(old_only | new_only)),
            "common_pixels": int(np.count_nonzero(common)),
            "files": mask_change_files,
            "processing_strategy": (
                "Affected region is old_mask union new_mask. old_only pixels revert to the "
                "base image, new_only pixels are solved with new foreground gradients, "
                "common pixels are re-solved because foreground gradients changed, and "
                "all pixels outside the union reuse the previous fused result exactly. "
                "Only the affected bounding box (plus a two-pixel numerical margin) is solved."
            ),
        }

        input_summary = {
            "previous_state": str(state_file.resolve()),
            "previous_state_sha256": sha256_file(state_file),
            "base": {
                "path": str(base_path.resolve()),
                "file_sha256": sha256_file(base_path),
                "array": array_descriptor(base),
            },
            "foreground": {
                "path": str(Path(foreground_path).resolve()),
                "file_sha256": sha256_file(foreground_path),
                "array": array_descriptor(foreground),
            },
            "old_mask": {
                "path": str(old_mask_path.resolve()),
                "array": array_descriptor(old_mask.astype(np.uint8)),
            },
            "new_mask": {
                "path": str(Path(new_mask_path).resolve()) if new_mask_path else str(old_mask_path.resolve()),
                "array": array_descriptor(new_mask.astype(np.uint8)),
            },
        }
        run_id = _run_id(input_summary, params, "incremental")
        report = {
            "schema_version": 1,
            "ok": True,
            "run_id": run_id,
            "run_type": "incremental_replacement",
            "seed_hint": seed_hint,
            "inputs": input_summary,
            "parameters": params.to_dict(),
            "mask_validation": full_bundle.extraction["mask_diagnostics"],
            "foreground_extraction": full_bundle.extraction,
            "solver": crop_solve.solver,
            "full_recompute_solver": full_solve.solver,
            "metrics": metrics,
            "updated_region": {
                **region_statistics(affected),
                "definition": "old_mask union new_mask",
                "bbox_with_numerical_margin": crop_box,
            },
            "mask_change": mask_change,
            "incremental_verification": verification,
            "outputs": outputs,
        }
        save_json(destination / "report.json", report)
        outputs["report_json"] = str((destination / "report.json").resolve())
        new_state = {
            "schema_version": 1,
            "run_type": "incremental",
            "run_id": run_id,
            "base_path": str(base_path.resolve()),
            "base_file_sha256": input_summary["base"]["file_sha256"],
            "foreground_path": str(Path(foreground_path).resolve()),
            "foreground_file_sha256": input_summary["foreground"]["file_sha256"],
            "mask_path": input_summary["new_mask"]["path"],
            "mask_sha256": array_sha256(new_mask.astype(np.uint8)),
            "fused_path": outputs["fused_u8_npy"],
            "fused_file_sha256": sha256_file(outputs["fused_u8_npy"]),
            "report_path": outputs["report_json"],
            "parameters": params.to_dict(),
            "previous_state_path": str(state_file.resolve()),
        }
        save_json(destination / "state.json", new_state)
        outputs["state_json"] = str((destination / "state.json").resolve())
        result = {
            "ok": True,
            "run_id": run_id,
            "run_type": "incremental_replacement",
            "output_dir": str(destination),
            "metrics": metrics,
            "mask_change": mask_change,
            "incremental_verification": verification,
            "solver": crop_solve.solver,
            "outputs": outputs,
        }
        log_record(
            log_path,
            {
                "event": "run",
                "status": "ok",
                "run_type": "incremental_replacement",
                "run_id": run_id,
                "seed_hint": seed_hint,
                "input_summary": input_summary,
                "parameters": params.to_dict(),
                "key_metrics": {
                    "seam_max": metrics["seam_error"]["max"],
                    "seam_mean": metrics["seam_error"]["mean"],
                    "gradient_p95": metrics["gradient_error"]["p95"],
                    "incremental_max_diff": verification["max_abs_difference"],
                    "incremental_within_tolerance": verification["within_tolerance"],
                },
                "updated_region": region_statistics(affected),
                "mask_change_detected": changed,
                "output_dir": str(destination),
                "output_files": outputs,
            },
        )
        return result
    except FusionError as exc:
        destination.mkdir(parents=True, exist_ok=True)
        save_json(destination / "error.json", exc.to_dict())
        log_record(
            log_path,
            {
                "event": "run",
                "status": "error",
                "run_type": "incremental_replacement",
                "seed_hint": seed_hint,
                "input_summary": input_summary,
                "error": exc.to_dict()["error"],
                "output_dir": str(destination),
            },
        )
        raise
