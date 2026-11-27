"""Single-part update reusing frozen error-diffusion state outside the part."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from .config import load_config, validate_config
from .density import effective_target_densities
from .dither import build_dither_offsets
from .diffusion import build_diffusion_context, diffuse
from .errors import BusinessError, require
from .io import read_json, sha256_file, write_json, write_pgm
from .validation import InputBundle, load_and_validate


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    out = mask.copy()
    for step in range(1, radius + 1):
        shifted = np.zeros_like(mask)
        shifted[step:, :] |= mask[:-step, :]
        shifted[:-step, :] |= mask[step:, :]
        shifted[:, step:] |= mask[:, :-step]
        shifted[:, :-step] |= mask[:, step:]
        out |= shifted
    return out


def _row_intervals(mask: np.ndarray, max_intervals: int = 200) -> list[list[int]]:
    intervals: list[list[int]] = []
    for y in range(mask.shape[0]):
        xs = np.flatnonzero(mask[y])
        if xs.size == 0:
            continue
        start = int(xs[0])
        previous = start
        for x in xs[1:]:
            if int(x) != previous + 1:
                intervals.append([int(y), start, previous])
                start = int(x)
                if len(intervals) >= max_intervals:
                    return intervals
            previous = int(x)
        intervals.append([int(y), start, previous])
        if len(intervals) >= max_intervals:
            break
    return intervals


def load_snapshot(snapshot_dir: str | Path, bundle: InputBundle) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    directory = Path(snapshot_dir)
    meta_path = directory / "snapshot.json"
    state_path = directory / "state.npz"
    require(meta_path.exists(), "E_SNAPSHOT_NOT_FOUND", f"快照缺少 snapshot.json: {directory}")
    require(state_path.exists(), "E_SNAPSHOT_NOT_FOUND", f"快照缺少 state.npz: {directory}")
    meta = read_json(meta_path)
    require(
        meta.get("sample_id") == bundle.sample.get("sample_id"),
        "E_SNAPSHOT_NOT_FOUND",
        "快照与当前样本 sample_id 不一致",
        details={"snapshot_sample_id": meta.get("sample_id"), "sample_id": bundle.sample.get("sample_id")},
    )
    try:
        state_npz = np.load(state_path, allow_pickle=False)
        required = {"binary", "incoming_error", "weight_clip_map", "unallocated_map", "dither"}
        missing = required.difference(state_npz.files)
        require(
            not missing,
            "E_SNAPSHOT_NOT_FOUND",
            f"快照状态缺少数组: {sorted(missing)}",
            state_path=str(state_path),
        )
        state = {name: state_npz[name] for name in state_npz.files}
    except (ValueError, OSError) as exc:
        raise BusinessError("E_SNAPSHOT_NOT_FOUND", f"无法读取快照状态: {exc}") from exc
    expected_shape = bundle.gray.shape
    for name in ("binary", "incoming_error", "weight_clip_map", "unallocated_map", "dither"):
        require(
            state[name].shape == expected_shape,
            "E_SNAPSHOT_NOT_FOUND",
            f"快照数组 {name} 尺寸不匹配",
            details={"array": name, "shape": list(state[name].shape), "expected": list(expected_shape)},
        )
    return meta, state


def _target_for_label(
    bundle: InputBundle,
    snapshot_meta: dict[str, Any],
    label: int,
    new_target: float,
) -> dict[str, float]:
    targets = effective_target_densities(bundle)
    for key, value in snapshot_meta.get("targets", {}).items():
        targets[int(key)] = float(value)
    minimum = float(bundle.config["density"]["min_target"])
    maximum = float(bundle.config["density"]["max_target"])
    if not minimum <= new_target <= maximum:
        raise BusinessError(
            "E_DENSITY_BOUNDS",
            f"新目标密度 {new_target} 越界 [{minimum}, {maximum}]",
            details={"label": label, "target": new_target, "min": minimum, "max": maximum},
        )
    targets[label] = float(new_target)
    return targets


def _offsets_from_baseline(
    bundle: InputBundle,
    snapshot_meta: dict[str, Any],
    targets: dict[int, float],
) -> tuple[dict[int, float], dict[int, float], dict[int, dict[str, Any]]]:
    max_correction = float(bundle.config["density"]["max_correction"])
    baseline_densities = {
        int(key): float(value["baseline_density"])
        for key, value in snapshot_meta["calibration"].items()
    }
    offsets: dict[int, float] = {}
    corrections: dict[int, float] = {}
    calibration: dict[int, dict[str, Any]] = {}
    for label, target in targets.items():
        if label not in baseline_densities:
            raise BusinessError(
                "E_SNAPSHOT_NOT_FOUND",
                f"快照缺少零件 {label} 的基线密度",
                details={"label": label},
            )
        requested = target - baseline_densities[label]
        applied = float(np.clip(requested, -max_correction, max_correction))
        corrections[label] = applied
        offsets[label] = -applied
        old = snapshot_meta["calibration"][str(label)]
        calibration[label] = {
            "baseline_density": baseline_densities[label],
            "target_density": target,
            "requested_correction": requested,
            "applied_correction": applied,
            "threshold_offset": -applied,
            "bounded": bool(abs(requested - applied) > 1e-12),
            "part_pixels": old.get("part_pixels"),
            "bbox_xywh": old.get("bbox_xywh"),
            "screen_cell_capacity": old.get("screen_cell_capacity"),
            "target_dot_count_estimate": int(
                round(target * int(old.get("part_pixels", 0)))
            ),
            "dot_area_fraction_of_cell": old.get("dot_area_fraction_of_cell"),
            "print_density_capacity_ratio": old.get("print_density_capacity_ratio"),
        }
    return corrections, offsets, calibration


def recompute_part(
    sample: str | Path,
    config_path: str | Path | None,
    snapshot_dir: str | Path,
    output_dir: str | Path,
    *,
    part_label: int,
    new_target_density: float,
    verify_full: bool = True,
) -> dict[str, Any]:
    config = load_config(config_path)
    validate_config(config)
    bundle = load_and_validate(sample, config)
    if part_label not in bundle.part_specs:
        raise BusinessError(
            "E_RECOMPUTE_PART",
            f"零件标签 {part_label} 不存在",
            details={"part_label": part_label, "available": sorted(bundle.part_specs)},
        )
    meta, state = load_snapshot(snapshot_dir, bundle)
    targets = _target_for_label(bundle, meta, part_label, new_target_density)
    corrections, offsets, calibration = _offsets_from_baseline(bundle, meta, targets)

    texture_by_label = {
        label: str(spec["texture_class"]) for label, spec in bundle.part_specs.items()
    }
    dither_seed = int(bundle.sample.get("seed", 0)) ^ int(config["dither"]["seed"])
    dither, amplitudes = build_dither_offsets(
        bundle.mask,
        seed=dither_seed,
        texture_amplitudes=config["dither"]["texture_amplitudes"],
        texture_by_label=texture_by_label,
        background_label=bundle.background_label,
    )
    context = build_diffusion_context(
        bundle.gray,
        bundle.mask,
        dither,
        config,
        background_label=bundle.background_label,
    )
    frozen_dither = state["dither"].astype(np.float64)
    require(
        np.allclose(dither, frozen_dither, rtol=0.0, atol=2e-6),
        "E_SNAPSHOT_NOT_FOUND",
        "当前抖动场与快照不一致",
    )

    local = diffuse(
        context,
        offsets,
        only_label=part_label,
        frozen_binary=state["binary"].astype(np.uint8),
        frozen_incoming_error=state["incoming_error"].astype(np.float64),
        frozen_weight_clip=state["weight_clip_map"].astype(np.float64),
        frozen_unallocated=state["unallocated_map"].astype(np.float64),
    )

    scope = bundle.mask == part_label
    guard = _dilate(scope, 1)
    outside_scope = ~guard
    original_binary = state["binary"].astype(np.uint8)
    changed = local.binary != original_binary
    changed_in_part = changed & scope
    changed_outside = changed & ~scope
    changed_pixels = int(changed.sum())
    changed_bbox = None
    if changed_pixels:
        ys, xs = np.nonzero(changed)
        changed_bbox = [int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)]
    full_result = None
    verification: dict[str, Any]
    if verify_full:
        full_result = diffuse(context, offsets)
        mismatch = np.argwhere(local.binary != full_result.binary)
        outside_mismatch = int(np.count_nonzero((local.binary != full_result.binary) & outside_scope))
        part_mismatch = int(np.count_nonzero((local.binary != full_result.binary) & scope))
        boundary_error_delta = float(
            np.max(
                np.abs(
                    local.incoming_error.astype(np.float64)[scope]
                    - full_result.incoming_error.astype(np.float64)[scope]
                )
            )
            if np.any(scope)
            else 0.0
        )
        verification = {
            "full_recompute_performed": True,
            "full_recompute_mismatch_pixels": int(mismatch.shape[0]),
            "part_mismatch_pixels": part_mismatch,
            "outside_scope_mismatch_pixels": outside_mismatch,
            "scope_mismatch_pixels": int(mismatch.shape[0]),
            "selected_part_incoming_error_max_abs_delta": boundary_error_delta,
            "pixel_exact": bool(mismatch.shape[0] == 0),
            "outside_scope_pixel_exact": bool(outside_mismatch == 0),
        }
        if mismatch.shape[0] != 0:
            raise BusinessError(
                "E_RECOMPUTE_VERIFY",
                "局部重算与同参数全量重算不是逐像素一致",
                details={
                    "mismatch_pixels": int(mismatch.shape[0]),
                    "first_pixels_xy": mismatch[:8][:, ::-1].tolist(),
                },
            )
        if int(changed_outside.sum()) != 0:
            raise BusinessError(
                "E_RECOMPUTE_VERIFY",
                "修改零件时出现了所属零件外的像素变化",
                details={"outside_changed_pixels": int(changed_outside.sum())},
            )
    else:
        verification = {
            "full_recompute_performed": False,
            "outside_scope_pixel_exact": None,
        }

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    np.save(output / "halftone.npy", local.binary.astype(np.uint8), allow_pickle=False)
    write_pgm(output / "halftone_preview.pgm", np.where(local.binary == 1, 0, 255).astype(np.uint8))
    part_dir = output / "parts" / f"part_{part_label:02d}_{bundle.part_specs[part_label]['name']}"
    part_dir.mkdir(parents=True, exist_ok=True)
    part_binary = np.zeros_like(local.binary, dtype=np.uint8)
    part_binary[scope] = local.binary[scope]
    np.save(part_dir / "halftone.npy", part_binary, allow_pickle=False)
    write_pgm(part_dir / "halftone.pgm", np.where(part_binary == 1, 0, 255).astype(np.uint8))
    if full_result is not None:
        (output / "verify").mkdir(parents=True, exist_ok=True)
        np.save(output / "verify" / "full_modified_halftone.npy", full_result.binary.astype(np.uint8), allow_pickle=False)
    state_out = output / "state.npz"
    np.savez_compressed(
        state_out,
        binary=local.binary.astype(np.uint8),
        incoming_error=local.incoming_error.astype(np.float32),
        weight_clip_map=local.weight_clip_map.astype(np.float32),
        unallocated_map=local.unallocated_map.astype(np.float32),
        dither=dither.astype(np.float32),
        kernel_class=local.kernel_class.astype(np.uint8),
    )
    np.save(output / "incoming_error.npy", local.incoming_error, allow_pickle=False)
    np.save(output / "quantization_error.npy", local.quant_error, allow_pickle=False)

    diff_report = {
        "schema": "clock-halftone-local-recompute/v1",
        "ok": bool(verification.get("pixel_exact", True) and int(changed_outside.sum()) == 0),
        "sample_id": bundle.sample.get("sample_id"),
        "snapshot_dir": str(Path(snapshot_dir).resolve()),
        "part_label": part_label,
        "part_name": bundle.part_specs[part_label].get("name"),
        "old_target_density": float(meta["targets"][str(part_label)]),
        "new_target_density": float(targets[part_label]),
        "old_threshold_offset": float(meta["threshold_offsets"][str(part_label)]),
        "new_threshold_offset": float(offsets[part_label]),
        "applied_correction": float(corrections[part_label]),
        "calibration": calibration[part_label],
        "recompute_scope": {
            "part_pixels": int(scope.sum()),
            "guard_band_px": 1,
            "guard_pixels": int((guard & ~scope).sum()),
            "policy": "selected part plus one-pixel boundary guard; residuals are mask-contained",
        },
        "difference_from_original": {
            "changed_pixels": changed_pixels,
            "changed_inside_part": int(changed_in_part.sum()),
            "changed_outside_part": int(changed_outside.sum()),
            "changed_bbox_xywh": changed_bbox,
            "changed_row_intervals_y_x0_x1": _row_intervals(changed),
            "row_intervals_truncated": bool(changed_pixels > 0 and len(_row_intervals(changed, 10**9)) > 200),
        },
        "verification": verification,
        "state_continuity": {
            "outside_scope_state_sources": [
                "snapshot.incoming_error",
                "snapshot.weight_clip_map",
                "snapshot.unallocated_map",
            ],
            "outside_scope_preserved_pixel_count": int(outside_scope.sum()),
            "mask_aware_residual_boundary": True,
            "boundary_state_continuous": bool(
                verification.get("selected_part_incoming_error_max_abs_delta", 0.0) == 0.0
            ),
        },
        "state_file": str(state_out.resolve()),
        "halftone_file": str((output / "halftone.npy").resolve()),
        "full_verification_file": (
            str((output / "verify" / "full_modified_halftone.npy").resolve()) if full_result is not None else None
        ),
    }
    write_json(output / "recompute_diff.json", diff_report)
    return diff_report
