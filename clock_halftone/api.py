"""End-to-end deterministic snapshot pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .config import load_config, validate_config
from .density import (
    build_density_report,
    calibrate_density_offsets,
    effective_target_densities,
)
from .detail import build_detail_report
from .diffusion import (
    DiffusionContext,
    DiffusionResult,
    build_diffusion_context,
    diffuse,
    kernel_usage_summary,
)
from .dither import build_dither_offsets
from .errors import BusinessError
from .io import sha256_file, write_json, write_pgm
from .spectrum import build_uniformity_report
from .tone import build_tone_report
from .validation import (
    InputBundle,
    build_input_analysis,
    load_and_validate,
)


def _part_report(
    label: int,
    density_report: dict[str, Any],
    tone_report: dict[str, Any],
    uniformity_report: dict[str, Any],
    detail_report: dict[str, Any],
) -> dict[str, Any]:
    density = next((item for item in density_report["parts"] if item["label"] == label), None)
    tone = next((item for item in tone_report["parts"] if item["label"] == label), None)
    uniformity = next((item for item in uniformity_report["parts"] if item["label"] == label), None)
    details = [
        item
        for item in detail_report["regions"]
        if int(item["part_label"]) == label
    ]
    return {
        "schema": "clock-halftone-part-report/v1",
        "label": label,
        "density": density,
        "tone": tone,
        "uniformity": uniformity,
        "detail_fidelity": details,
    }


def _quantization_report(
    bundle: InputBundle,
    baseline: DiffusionResult,
    final: DiffusionResult,
    kernel_usage: dict[str, Any],
    dither_amplitudes: dict[int, float],
    threshold_offset_map: np.ndarray,
    seed: int,
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    for label, spec in sorted(bundle.part_specs.items()):
        selected = bundle.mask == label
        dither_values = final.dither[selected]
        parts.append(
            {
                "label": label,
                "name": spec.get("name"),
                "pixels": int(selected.sum()),
                "dither_amplitude": float(dither_amplitudes.get(label, 0.0)),
                "dither_offset_min": float(dither_values.min()),
                "dither_offset_max": float(dither_values.max()),
                "dither_offset_mean": float(dither_values.mean()),
                "dither_offset_std": float(dither_values.std()),
                "density_threshold_offset": float(threshold_offset_map[selected].mean()),
                "incoming_error_mean": float(final.incoming_error[selected].mean()),
                "incoming_error_abs_max": float(np.abs(final.incoming_error[selected]).max()),
                "quantization_error_sum": float(final.quant_error[selected].sum()),
                "weight_clip_mass_sum": float(final.weight_clip_map[selected].sum()),
                "unallocated_residual_sum": float(final.unallocated_map[selected].sum()),
                "baseline_ink_density": float(baseline.binary[selected].mean()),
                "final_ink_density": float(final.binary[selected].mean()),
                "kernel_usage": kernel_usage.get(str(label)),
            }
        )
    background = bundle.mask == bundle.background_label
    return {
        "schema": "clock-halftone-quantization-report/v1",
        "algorithm": "adaptive gradient-oriented serpentine error diffusion with coordinate dither",
        "seed": seed,
        "scan": "serpentine rows, residual target order follows the selected forward kernel",
        "mask_policy": "residuals are redistributed only to unprocessed pixels of the same part label",
        "residual_balance_tolerance": final.stats["residual_conservation_tolerance"],
        "residual_balance_ok": final.stats["residual_conserved"],
        "overall": {
            key: final.stats[key]
            for key in (
                "scanned_pixels",
                "requested_ink_mass",
                "output_ink_mass",
                "quantization_error_sum",
                "residual_balance",
                "boundary_abs_loss",
                "weight_clipped_mass",
                "unallocated_residual_sum",
                "residual_conserved",
                "max_abs_incoming_error",
            )
        },
        "baseline_overall": baseline.stats,
        "background": {
            "label": bundle.background_label,
            "pixels": int(background.sum()),
            "dither_amplitude": float(dither_amplitudes.get(bundle.background_label, 0.0)),
            "input_ink_density": float((1.0 - bundle.gray.astype(np.float64) / 255.0)[background].mean()),
            "final_ink_density": float(final.binary[background].mean()),
            "weight_clip_mass_sum": float(final.weight_clip_map[background].sum()),
            "unallocated_residual_sum": float(final.unallocated_map[background].sum()),
            "kernel_usage": kernel_usage.get("background"),
        },
        "parts": parts,
        "artifacts": {
            "halftone_binary": "halftone.npy",
            "incoming_error": "incoming_error.npy",
            "quantization_error": "quantization_error.npy",
            "weight_clip_map": "weight_clip_map.npy",
            "unallocated_residual": "unallocated_residual.npy",
            "dither_offsets": "dither_offsets.npy",
            "threshold_offsets_per_pixel": "threshold_offsets_per_pixel.npy",
            "kernel_class": "kernel_class.npy",
        },
    }


def _write_part_artifacts(
    bundle: InputBundle,
    final: DiffusionResult,
    report: dict[str, Any],
    output_dir: Path,
) -> dict[int, str]:
    paths: dict[int, str] = {}
    for label, spec in sorted(bundle.part_specs.items()):
        name = str(spec.get("name", f"part_{label}"))
        directory = output_dir / "parts" / f"part_{label:02d}_{name}"
        directory.mkdir(parents=True, exist_ok=True)
        selected = bundle.mask == label
        part_binary = np.zeros_like(final.binary, dtype=np.uint8)
        part_binary[selected] = final.binary[selected]
        np.save(directory / "halftone.npy", part_binary, allow_pickle=False)
        write_pgm(directory / "halftone.pgm", np.where(part_binary == 1, 0, 255).astype(np.uint8))
        bbox = bundle.bbox_for_label(label)
        if bbox is not None:
            x, y, width, height = bbox
            np.save(
                directory / "halftone_crop.npy",
                part_binary[y : y + height, x : x + width],
                allow_pickle=False,
            )
            np.save(
                directory / "mask_crop.npy",
                selected[y : y + height, x : x + width].astype(np.uint8),
                allow_pickle=False,
            )
            write_pgm(
                directory / "halftone_crop.pgm",
                np.where(part_binary[y : y + height, x : x + width] == 1, 0, 255).astype(np.uint8),
            )
        write_json(directory / "report.json", report)
        paths[label] = str(directory.resolve())
    return paths


def _append_snapshot_catalog(path: str | Path | None, entry: dict[str, Any]) -> None:
    if path is None:
        return
    catalog_path = Path(path)
    if catalog_path.exists():
        import json

        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    else:
        catalog = {"schema": "clock-halftone-snapshot-catalog/v1", "snapshots": []}
    catalog.setdefault("snapshots", [])
    retained: list[dict[str, Any]] = []
    for item in catalog["snapshots"]:
        if item.get("kind") == "local_recompute":
            retained.append(item)
            continue
        if item.get("snapshot_id") == entry.get("snapshot_id"):
            continue
        if item.get("path") == entry.get("path"):
            continue
        retained.append(item)
    catalog["snapshots"] = retained
    catalog["snapshots"].append(entry)
    catalog["snapshots"].sort(key=lambda item: item.get("snapshot_id", ""))
    write_json(catalog_path, catalog)


def run_pipeline(
    sample: str | Path,
    *,
    config_path: str | Path | None = None,
    output_dir: str | Path,
    snapshot_id: str | None = None,
    target_overrides: dict[int, float] | None = None,
    catalog_path: str | Path | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    validate_config(config)
    bundle = load_and_validate(sample, config)
    input_analysis = build_input_analysis(bundle)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    texture_by_label = {
        label: str(spec["texture_class"]) for label, spec in bundle.part_specs.items()
    }
    seed = int(bundle.sample.get("seed", 0)) ^ int(config["dither"]["seed"])
    dither_offsets, dither_amplitudes = build_dither_offsets(
        bundle.mask,
        seed=seed,
        texture_amplitudes=config["dither"]["texture_amplitudes"],
        texture_by_label=texture_by_label,
        background_label=bundle.background_label,
    )
    context: DiffusionContext = build_diffusion_context(
        bundle.gray,
        bundle.mask,
        dither_offsets,
        config,
        background_label=bundle.background_label,
    )

    baseline = diffuse(context, {})
    targets = effective_target_densities(bundle, target_overrides)
    corrections, offsets, calibration = calibrate_density_offsets(bundle, baseline, targets)
    final = diffuse(context, offsets)
    if not final.stats["residual_conserved"]:
        raise BusinessError(
            "E_INTERNAL",
            "量化残差守恒超出容差",
            details={
                "residual_balance": final.stats["residual_balance"],
                "tolerance": final.stats["residual_conservation_tolerance"],
            },
        )

    density_report = build_density_report(bundle, baseline, final, calibration)
    tone_report = build_tone_report(bundle, baseline, final)
    uniformity_report = build_uniformity_report(bundle, final)
    detail_report = build_detail_report(bundle, final)
    kernel_usage = kernel_usage_summary(
        final,
        bundle.mask,
        bundle.part_specs,
        background_label=bundle.background_label,
    )

    threshold_offset_map = np.zeros(bundle.mask.shape, dtype=np.float32)
    for label, offset in offsets.items():
        threshold_offset_map[bundle.mask == label] = float(offset)

    quantization_report = _quantization_report(
        bundle,
        baseline,
        final,
        kernel_usage,
        dither_amplitudes,
        threshold_offset_map,
        seed,
    )

    np.save(output / "halftone.npy", final.binary.astype(np.uint8), allow_pickle=False)
    write_pgm(output / "halftone_preview.pgm", np.where(final.binary == 1, 0, 255).astype(np.uint8))
    np.save(output / "incoming_error.npy", final.incoming_error, allow_pickle=False)
    np.save(output / "quantization_error.npy", final.quant_error, allow_pickle=False)
    np.save(output / "weight_clip_map.npy", final.weight_clip_map, allow_pickle=False)
    np.save(output / "unallocated_residual.npy", final.unallocated_map, allow_pickle=False)
    np.save(output / "dither_offsets.npy", final.dither.astype(np.float32), allow_pickle=False)
    np.save(output / "threshold_offsets_per_pixel.npy", threshold_offset_map, allow_pickle=False)
    np.save(output / "kernel_class.npy", final.kernel_class.astype(np.uint8), allow_pickle=False)
    np.savez_compressed(
        output / "state.npz",
        binary=final.binary.astype(np.uint8),
        incoming_error=final.incoming_error.astype(np.float32),
        weight_clip_map=final.weight_clip_map.astype(np.float32),
        unallocated_map=final.unallocated_map.astype(np.float32),
        dither=dither_offsets.astype(np.float32),
        kernel_class=final.kernel_class.astype(np.uint8),
    )

    reports_dir = output / "reports"
    write_json(reports_dir / "input_analysis.json", input_analysis)
    write_json(reports_dir / "density_report.json", density_report)
    write_json(reports_dir / "tone_error_report.json", tone_report)
    write_json(reports_dir / "uniformity_report.json", uniformity_report)
    write_json(reports_dir / "detail_fidelity.json", detail_report)
    write_json(reports_dir / "quantization_report.json", quantization_report)
    write_json(reports_dir / "kernel_usage.json", kernel_usage)

    part_paths = _write_part_artifacts(
        bundle,
        final,
        {},
        output,
    )
    for label, directory in part_paths.items():
        part_report = _part_report(
            label,
            density_report,
            tone_report,
            uniformity_report,
            detail_report,
        )
        write_json(Path(directory) / "report.json", part_report)

    global_ok = bool(
        density_report["ok"]
        and tone_report["ok"]
        and uniformity_report["ok"]
        and detail_report["ok"]
        and final.stats["residual_conserved"]
    )
    sid = snapshot_id or (
        f"{bundle.sample.get('sample_id', 'sample')}_"
        f"{float(bundle.print_grid['lines_per_inch']):g}lpi_"
        f"{float(bundle.print_grid['ink_dot_diameter_mm']):.3f}mm"
    ).replace(" ", "_")
    snapshot = {
        "schema": "clock-halftone-snapshot/v1",
        "snapshot_id": sid,
        "sample_id": bundle.sample.get("sample_id"),
        "seed": seed,
        "ok": global_ok,
        "print_grid": bundle.print_grid,
        "targets": {str(label): float(value) for label, value in targets.items()},
        "corrections": {str(label): float(value) for label, value in corrections.items()},
        "threshold_offsets": {str(label): float(value) for label, value in offsets.items()},
        "calibration": {str(label): value for label, value in calibration.items()},
        "quantization": final.stats,
        "kernel_usage": kernel_usage,
        "dither_amplitudes": {str(label): float(value) for label, value in dither_amplitudes.items()},
        "files": {
            "halftone": "halftone.npy",
            "halftone_preview": "halftone_preview.pgm",
            "state": "state.npz",
            "incoming_error": "incoming_error.npy",
            "quantization_error": "quantization_error.npy",
            "weight_clip_map": "weight_clip_map.npy",
            "unallocated_residual": "unallocated_residual.npy",
            "dither_offsets": "dither_offsets.npy",
            "threshold_offsets_per_pixel": "threshold_offsets_per_pixel.npy",
            "kernel_class": "kernel_class.npy",
            "reports": {
                "input_analysis": "reports/input_analysis.json",
                "density": "reports/density_report.json",
                "tone_error": "reports/tone_error_report.json",
                "uniformity": "reports/uniformity_report.json",
                "detail_fidelity": "reports/detail_fidelity.json",
                "quantization": "reports/quantization_report.json",
                "kernel_usage": "reports/kernel_usage.json",
            },
            "parts": {
                str(label): str(Path(directory) / "report.json")
                for label, directory in part_paths.items()
            },
        },
        "report_status": {
            "input_analysis": True,
            "density": density_report["ok"],
            "tone_error": tone_report["ok"],
            "uniformity": uniformity_report["ok"],
            "detail_fidelity": detail_report["ok"],
            "residual_conservation": final.stats["residual_conserved"],
        },
    }
    data_files = [
        output / "halftone.npy",
        output / "state.npz",
        output / "incoming_error.npy",
        output / "quantization_error.npy",
        output / "weight_clip_map.npy",
        output / "unallocated_residual.npy",
        output / "dither_offsets.npy",
        output / "threshold_offsets_per_pixel.npy",
        output / "kernel_class.npy",
        reports_dir / "input_analysis.json",
        reports_dir / "density_report.json",
        reports_dir / "tone_error_report.json",
        reports_dir / "uniformity_report.json",
        reports_dir / "detail_fidelity.json",
        reports_dir / "quantization_report.json",
    ]
    snapshot["sha256"] = {file.name: sha256_file(file) for file in data_files if file.exists()}
    write_json(output / "snapshot.json", snapshot)

    _append_snapshot_catalog(
        catalog_path,
        {
            "snapshot_id": sid,
            "sample_id": bundle.sample.get("sample_id"),
            "path": str(output.resolve()),
            "lines_per_inch": bundle.print_grid["lines_per_inch"],
            "ink_dot_diameter_mm": bundle.print_grid["ink_dot_diameter_mm"],
            "targets": snapshot["targets"],
            "ok": global_ok,
        },
    )
    return {
        "ok": global_ok,
        "snapshot_id": sid,
        "snapshot_dir": str(output.resolve()),
        "sample_id": bundle.sample.get("sample_id"),
        "print_grid": bundle.print_grid,
        "targets": snapshot["targets"],
        "corrections": snapshot["corrections"],
        "report_status": snapshot["report_status"],
        "files": snapshot["files"],
    }
