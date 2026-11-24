"""End-to-end deterministic registration, adjudication and stitching."""

from __future__ import annotations

import io
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from PIL import Image, PngImagePlugin

from .errors import E_DIGEST, E_INTERRUPTED, PipelineError
from .fusion import fuse_serial_reference, fuse_weighted, fusion_digest
from .geometry import bilinear_shift_image
from .loader import LoadedSample, load_project
from .masking import arbitrate_masks
from .radiometry import Radiometry, estimate_linear_correction, mean_absolute_difference
from .registration import phase_correlate
from .resources import ResourceGate
from .storage import (
    GRACEFUL_EXIT_CODE,
    clear_interrupt_marker,
    commit_tile,
    export_bytes_if_version_changed,
    read_json_report_version,
    read_png_version,
    archive_changed_version,
    verify_tile,
    write_interrupt_marker,
)


@dataclass
class PipelineConfig:
    sample_dir: Path
    output_dir: Path = Path("outputs/default")
    state_dir: Path = Path("state/default")
    workers: int = 2
    max_inflight_pixels: Optional[int] = None
    max_spectrum_bytes: Optional[int] = None
    check_serial: bool = True
    publish: bool = True
    interrupt_after: Optional[int] = None
    stop_event: Optional[threading.Event] = None
    image_name: str = "mosaic.png"
    report_name: str = "registration_verification_report.json"
    extra: Dict[str, Any] = field(default_factory=dict)


def _error_distance(est_dx: float, est_dy: float, truth: Dict[str, Any]) -> Dict[str, float]:
    true_xy = truth.get("offset_xy", [0.0, 0.0])
    true_dx, true_dy = float(true_xy[0]), float(true_xy[1])
    err_dx = float(est_dx - true_dx)
    err_dy = float(est_dy - true_dy)
    return {
        "dx_error": err_dx,
        "dy_error": err_dy,
        "max_abs_component_error": max(abs(err_dx), abs(err_dy)),
        "euclidean_error": float(np.hypot(err_dx, err_dy)),
    }


def _find_registration_overlap(
    sample: LoadedSample, tile: Dict[str, Any]
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    reference_id = str(sample.project.get("reference_tile_id", "tile_00"))
    tiles_by_id = {item["tile_id"]: item for item in sample.tiles}
    for overlap in sample.project["overlaps"]:
        ids = overlap["tile_ids"]
        if tile["tile_id"] in ids and reference_id in ids and tile["tile_id"] != reference_id:
            other_id = reference_id
            return overlap, tiles_by_id[other_id]
    for overlap in sample.project["overlaps"]:
        ids = overlap["tile_ids"]
        if tile["tile_id"] in ids:
            other_id = next(item for item in ids if item != tile["tile_id"])
            return overlap, tiles_by_id[other_id]
    raise PipelineError("E_OVERLAP", "tile has no overlap available for registration", tile_id=tile["tile_id"])


def _patch_pair(
    sample: LoadedSample, tile: Dict[str, Any], reference: Dict[str, Any], overlap: Dict[str, Any]
) -> tuple[np.ndarray, np.ndarray]:
    bbox = overlap["bbox"]
    x0, y0 = int(bbox["x"]), int(bbox["y"])
    width, height = int(bbox["width"]), int(bbox["height"])
    ref_x = x0 - int(reference["x"])
    ref_y = y0 - int(reference["y"])
    tile_x = x0 - int(tile["x"])
    tile_y = y0 - int(tile["y"])
    ref_image = sample.image_by_id[reference["tile_id"]]
    tile_image = sample.image_by_id[tile["tile_id"]]
    ref_patch = ref_image[ref_y:ref_y + height, ref_x:ref_x + width]
    tile_patch = tile_image[tile_y:tile_y + height, tile_x:tile_x + width]
    if ref_patch.shape != (height, width) or tile_patch.shape != (height, width):
        raise PipelineError("E_OVERLAP", "overlap patch extraction has wrong shape", overlap_id=overlap.get("overlap_id"))
    return ref_patch, tile_patch


def _process_tile(
    sample: LoadedSample,
    tile: Dict[str, Any],
    mask_weights: Dict[str, np.ndarray],
    mask_sources: Dict[str, str],
    gate: ResourceGate,
    state_dir: Path,
    stop_event: threading.Event,
) -> Dict[str, Any]:
    tile_id = str(tile["tile_id"])
    if stop_event.is_set():
        return {"tile_id": tile_id, "status": "skipped"}

    is_reference = tile_id == str(sample.project.get("reference_tile_id", "tile_00"))
    if is_reference:
        overlap_height = overlap_width = 1
        overlap: Optional[Dict[str, Any]] = None
        reference = tile
    else:
        overlap, reference = _find_registration_overlap(sample, tile)
        overlap_height = int(overlap["bbox"]["height"])
        overlap_width = int(overlap["bbox"]["width"])

    pixel_slots, spectrum_bytes = gate.estimate(
        int(tile["width"]), int(tile["height"]), overlap_height, overlap_width
    )
    gate.acquire(pixel_slots, spectrum_bytes)
    try:
        if stop_event.is_set():
            return {"tile_id": tile_id, "status": "skipped"}

        image = sample.image_by_id[tile_id].astype(np.float64)
        if is_reference:
            integer_dx = integer_dy = 0
            sub_dx = sub_dy = 0.0
            confidence = 1.0
            reg_dict = {
                "integer_offset_xy": [0, 0],
                "subpixel_offset_xy": [0.0, 0.0],
                "confidence": 1.0,
                "fft_peak_norm": 1.0,
                "response_peak": 0.0,
            }
            correction = Radiometry(1.0, 0.0)
            before_mae = after_mae = 0.0
        else:
            assert overlap is not None
            ref_patch, tile_patch = _patch_pair(sample, tile, reference, overlap)
            registration = phase_correlate(
                ref_patch,
                tile_patch,
                max_shift=int(sample.project.get("max_shift_px", 32)),
            )
            integer_dx = registration.integer_dx
            integer_dy = registration.integer_dy
            sub_dx = registration.subpixel_dx
            sub_dy = registration.subpixel_dy
            confidence = registration.confidence
            reg_dict = registration.to_dict()

            correction = estimate_linear_correction(
                ref_patch.astype(np.float64),
                tile_patch.astype(np.float64),
                integer_dy,
                integer_dx,
            )
            raw_aligned = bilinear_shift_image(
                sample.image_by_id[tile_id], sub_dx, sub_dy
            )
            corrected_source = correction.apply(sample.image_by_id[tile_id])
            aligned = bilinear_shift_image(corrected_source, sub_dx, sub_dy)

            bbox = overlap["bbox"]
            tx, ty = int(bbox["x"]) - int(tile["x"]), int(bbox["y"]) - int(tile["y"])
            rw, rh = int(bbox["width"]), int(bbox["height"])
            rx = int(bbox["x"]) - int(reference["x"])
            ry = int(bbox["y"]) - int(reference["y"])
            margin = int(np.ceil(max(abs(sub_dx), abs(sub_dy), 2.0))) + 3
            if min(rw, rh) > 2 * margin + 8:
                slices = (slice(margin, rh - margin), slice(margin, rw - margin))
            else:
                slices = (slice(2, rh - 2), slice(2, rw - 2))
            ref_region = sample.image_by_id[reference["tile_id"]][
                ry + slices[0].start:ry + slices[0].stop,
                rx + slices[1].start:rx + slices[1].stop,
            ].astype(np.float64)
            raw_region = raw_aligned[
                ty + slices[0].start:ty + slices[0].stop,
                tx + slices[1].start:tx + slices[1].stop,
            ]
            corrected_region = aligned[
                ty + slices[0].start:ty + slices[0].stop,
                tx + slices[1].start:tx + slices[1].stop,
            ]
            before_mae = mean_absolute_difference(ref_region, raw_region)
            after_mae = mean_absolute_difference(ref_region, corrected_region)

        if is_reference:
            aligned = image.copy()
        scale = correction.scale
        radiometry_offset = correction.offset
        weight = mask_weights[tile_id].astype(np.float32)
        aligned32 = aligned.astype(np.float32)
        contribution = (aligned32.astype(np.float64) * weight.astype(np.float64)).astype(np.float32)
        arrays = {
            "aligned": aligned32,
            "weight": weight,
            "contribution": contribution,
        }

        truth_tile = sample.truth_by_id[tile_id]
        errors = _error_distance(sub_dx, sub_dy, truth_tile)
        integer_truth = truth_tile.get("integer_offset_xy", [0, 0])
        integer_error = {
            "dx_error": int(integer_dx - int(integer_truth[0])),
            "dy_error": int(integer_dy - int(integer_truth[1])),
            "exact": integer_dx == int(integer_truth[0]) and integer_dy == int(integer_truth[1]),
        }
        tolerance = float(sample.project.get("subpixel_tolerance_px", 0.2))
        min_confidence = float(sample.project.get("min_confidence", 0.35))
        needs_review = confidence < min_confidence
        adoption_status = (
            "provisional_requires_manual_confirmation"
            if needs_review
            else "adopted"
        )
        metrics: Dict[str, Any] = {
            "tile_id": tile_id,
            "registration": reg_dict,
            "truth_subpixel_offset_xy": truth_tile.get("offset_xy"),
            "truth_integer_offset_xy": integer_truth,
            "subpixel_error": errors,
            "integer_error": integer_error,
            "subpixel_tolerance_px": tolerance,
            "within_subpixel_tolerance": bool(errors["max_abs_component_error"] <= tolerance),
            "confidence_threshold": min_confidence,
            "needs_manual_review": bool(needs_review),
            "adoption_status": adoption_status,
            "radiometry": {"scale": float(scale), "offset": float(radiometry_offset)},
            "truth_radiometry": truth_tile.get("radiometry"),
            "overlap_mean_abs_difference": {
                "before_correction": float(before_mae),
                "after_correction": float(after_mae),
                "improvement_ratio": float(before_mae / max(after_mae, 1e-9)),
            },
            "mask_source": mask_sources.get(tile_id, "unavailable"),
            "resource_estimate": {
                "pixel_slots": int(pixel_slots),
                "spectrum_bytes": int(spectrum_bytes),
            },
        }
        manifest = commit_tile(state_dir, tile_id, sample.version_id, arrays, metrics)
        stored, check = verify_tile(
            state_dir,
            tile_id,
            int(tile["width"]),
            int(tile["height"]),
            sample.version_id,
        )
        return {
            "tile_id": tile_id,
            "status": "processed",
            "arrays": stored.arrays if stored is not None else arrays,
            "manifest": manifest,
            "digest_check": check,
            "metrics": metrics,
        }
    finally:
        gate.release(pixel_slots, spectrum_bytes)


def _png_bytes(image: np.ndarray, version_id: str) -> bytes:
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("surface_stitch_version", version_id)
    buffer = io.BytesIO()
    Image.fromarray(image, mode="L").save(
        buffer, format="PNG", pnginfo=metadata, optimize=False, compress_level=6
    )
    return buffer.getvalue()


def run_pipeline(config: PipelineConfig) -> Dict[str, Any]:
    sample = load_project(config.sample_dir)
    arbitration = arbitrate_masks(
        sample.mask_records,
        sample.tiles,
        int(sample.project["canvas"]["width"]),
        int(sample.project["canvas"]["height"]),
    )

    state_dir = Path(config.state_dir).resolve()
    output_dir = Path(config.output_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    archived = archive_changed_version(state_dir, sample.version_id)

    workers = int(config.workers or sample.project.get("resources", {}).get("max_workers", 2))
    resources_cfg = sample.project.get("resources", {})
    gate = ResourceGate(
        max_workers=workers,
        max_inflight_pixels=int(
            config.max_inflight_pixels
            or resources_cfg.get("max_inflight_pixels", 2_000_000)
        ),
        max_spectrum_bytes=int(
            config.max_spectrum_bytes
            or resources_cfg.get("max_spectrum_bytes", 64 * 1024 * 1024)
        ),
    )
    stop_event = config.stop_event or threading.Event()

    arrays_by_tile: Dict[str, Dict[str, np.ndarray]] = {}
    tile_reports: Dict[str, Dict[str, Any]] = {}
    digest_events: List[Dict[str, Any]] = []
    initial_checks: Dict[str, Dict[str, Any]] = {}
    pending: List[Dict[str, Any]] = []

    for tile in sample.tiles:
        tile_id = str(tile["tile_id"])
        try:
            stored, check = verify_tile(
                state_dir,
                tile_id,
                int(tile["width"]),
                int(tile["height"]),
                sample.version_id,
            )
        except PipelineError as exc:
            check = {
                "tile_id": tile_id,
                "status": "invalid",
                "code": exc.code,
                "valid": False,
                "message": exc.message,
                "details": exc.details,
            }
            stored = None
        initial_checks[tile_id] = check
        digest_events.append(check)
        if stored is not None and check.get("valid"):
            arrays_by_tile[tile_id] = {
                "aligned": stored.arrays["aligned"],
                "weight": stored.arrays["weight"],
                "contribution": stored.arrays["contribution"],
            }
            tile_reports[tile_id] = {
                **stored.manifest.get("metrics", {}),
                "persistence_status": "reused",
                "digest_check": check,
            }
        else:
            pending.append(tile)

    processed_count = 0
    skipped: List[str] = []
    failures: List[Dict[str, Any]] = []

    if pending and not stop_event.is_set():
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="surface-tile") as executor:
            future_map: Dict[Any, Any] = {}
            next_index = 0

            def submit_next() -> None:
                nonlocal next_index
                if stop_event.is_set() or next_index >= len(pending):
                    return
                tile = pending[next_index]
                next_index += 1
                future = executor.submit(
                    _process_tile,
                    sample,
                    tile,
                    arbitration.weights_by_tile,
                    arbitration.sources_by_tile,
                    gate,
                    state_dir,
                    stop_event,
                )
                future_map[future] = str(tile["tile_id"])

            for _ in range(min(workers, len(pending))):
                submit_next()

            while future_map:
                done, _not_done = wait(future_map, return_when=FIRST_COMPLETED)
                for future in done:
                    tile_id = future_map.pop(future)
                    try:
                        outcome = future.result()
                    except PipelineError as exc:
                        failures.append({"tile_id": tile_id, "code": exc.code, "message": exc.message})
                        continue
                    except Exception as exc:
                        failures.append({"tile_id": tile_id, "code": "E_STATE", "message": str(exc)})
                        continue

                    if outcome.get("status") == "skipped":
                        skipped.append(tile_id)
                        continue
                    processed_count += 1
                    arrays_by_tile[tile_id] = outcome["arrays"]
                    check = outcome["digest_check"]
                    digest_events.append(check)
                    tile_reports[tile_id] = {
                        **outcome["metrics"],
                        "persistence_status": "processed",
                        "digest_check": check,
                    }
                    if config.interrupt_after is not None and processed_count >= int(config.interrupt_after):
                        stop_event.set()

                while len(future_map) < workers and not stop_event.is_set():
                    previous_index = next_index
                    submit_next()
                    if next_index == previous_index:
                        break

    if failures and not stop_event.is_set():
        raise PipelineError(
            "E_STATE",
            "one or more tiles failed before fusion",
            {"failures": failures},
        )

    if stop_event.is_set() or skipped:
        completed = sorted(tile_reports)
        pending_ids = [tile["tile_id"] for tile in sample.tiles if tile["tile_id"] not in tile_reports]
        marker_payload = {
            "interrupted": True,
            "exit_code": GRACEFUL_EXIT_CODE,
            "version_id": sample.version_id,
            "completed_tiles": completed,
            "pending_tiles": sorted(set(pending_ids + skipped)),
            "processed_in_invocation": processed_count,
            "failures": failures,
        }
        write_interrupt_marker(state_dir, marker_payload)
        report = {
            "ok": not failures,
            "interrupted": True,
            "exit_code": GRACEFUL_EXIT_CODE,
            "version_id": sample.version_id,
            "archived_previous_state": archived,
            "completed_tiles": completed,
            "pending_tiles": marker_payload["pending_tiles"],
            "failures": failures,
            "digest_events": digest_events,
            "resource_usage": gate.usage.to_dict(),
        }
        if failures:
            report["error"] = {"code": "E_STATE", "failures": failures}
        return report

    clear_interrupt_marker(state_dir)

    canvas_width = int(sample.project["canvas"]["width"])
    canvas_height = int(sample.project["canvas"]["height"])
    fused, fused_float, denominator = fuse_weighted(
        arrays_by_tile, sample.tiles, canvas_width, canvas_height
    )
    serial_image = None
    serial_matches = None
    if config.check_serial:
        serial_image = fuse_serial_reference(
            arrays_by_tile, sample.tiles, canvas_width, canvas_height
        )
        serial_matches = bool(np.array_equal(fused, serial_image))

    tile_report_list: List[Dict[str, Any]] = []
    business_codes: List[str] = []
    max_integer_error = 0
    max_subpixel_error = 0.0
    registration_passed = True
    review_tiles: List[str] = []
    before_values: List[float] = []
    after_values: List[float] = []

    for tile in sample.tiles:
        tile_id = str(tile["tile_id"])
        item = tile_reports[tile_id]
        codes: List[str] = []
        initial = initial_checks[tile_id]
        if initial.get("code"):
            codes.append(initial["code"])
        final_check = item.get("digest_check", {})
        if final_check.get("code") and final_check.get("status") != "valid":
            codes.append(final_check["code"])
        item["business_codes"] = codes
        business_codes.extend(codes)

        int_error = item.get("integer_error", {})
        sub_error = item.get("subpixel_error", {})
        max_integer_error = max(max_integer_error, abs(int(int_error.get("dx_error", 0))), abs(int(int_error.get("dy_error", 0))))
        max_subpixel_error = max(max_subpixel_error, float(sub_error.get("max_abs_component_error", 0.0)))
        if not int_error.get("exact", True) or not item.get("within_subpixel_tolerance", True):
            registration_passed = False
        if item.get("needs_manual_review"):
            review_tiles.append(tile_id)
        diffs = item.get("overlap_mean_abs_difference", {})
        if diffs:
            before_values.append(float(diffs.get("before_correction", 0.0)))
            after_values.append(float(diffs.get("after_correction", 0.0)))
        registration_info = item.get("registration", {})
        item["integer_offset_xy"] = registration_info.get("integer_offset_xy", [0, 0])
        item["subpixel_offset_xy"] = registration_info.get("subpixel_offset_xy", [0.0, 0.0])
        item["confidence"] = registration_info.get("confidence", 1.0)
        item["error_vs_truth"] = {
            "integer": int_error,
            "subpixel": sub_error,
        }
        tile_report_list.append(item)

    uncovered = int(np.sum(denominator <= 1e-12))
    mean_before = float(np.mean(before_values)) if before_values else 0.0
    mean_after = float(np.mean(after_values)) if after_values else 0.0
    report: Dict[str, Any] = {
        "ok": True,
        "interrupted": False,
        "version_id": sample.version_id,
        "sample_seed": sample.project.get("seed"),
        "canvas": sample.project["canvas"],
        "merge_order": "tile_id ascending; within each array C row-major/pixel row order",
        "processing_convention": {
            "resampling_kernel": "deterministic_bilinear",
            "out_of_bounds_fill": 0,
            "order": [
                "estimate_linear_radiometry",
                "apply_linear_radiometry",
                "bilinear_resample",
                "normalize_soft_masks",
                "sorted_weighted_merge",
            ],
        },
        "archived_previous_state": archived,
        "conflict_count": len(arbitration.conflicts),
        "conflicts": arbitration.conflicts,
        "mask_tombstones": arbitration.tombstones,
        "same_id_revisions": arbitration.revisions,
        "digest_checks": [
            {
                "tile_id": check.get("tile_id"),
                "status": check.get("status"),
                "valid": check.get("valid"),
                "code": check.get("code"),
                "block_digest": check.get("block_digest"),
                "message": check.get("message"),
            }
            for check in digest_events
        ],
        "business_codes": sorted(set(business_codes)),
        "tiles": tile_report_list,
        "registration_summary": {
            "integer_tolerance_px": sample.project.get("integer_tolerance_px", 0),
            "subpixel_tolerance_px": sample.project.get("subpixel_tolerance_px", 0.2),
            "max_integer_error_px": max_integer_error,
            "max_subpixel_error_px": max_subpixel_error,
            "passed": bool(
                max_integer_error <= int(sample.project.get("integer_tolerance_px", 0))
                and max_subpixel_error <= float(sample.project.get("subpixel_tolerance_px", 0.2))
            ),
            "needs_manual_review_tiles": review_tiles,
        },
        "radiometry_summary": {
            "mean_overlap_mae_before": mean_before,
            "mean_overlap_mae_after": mean_after,
            "improvement_ratio": float(mean_before / max(mean_after, 1e-9)),
            "significantly_improved": bool(mean_after < mean_before * 0.8),
        },
        "fusion_consistency": {
            "serial_reference_checked": bool(config.check_serial),
            "weighted_and_row_serial_pixels_identical": serial_matches,
            "fused_sha256": fusion_digest(fused),
            "uncovered_pixels": uncovered,
        },
        "resource_usage": gate.usage.to_dict(),
    }

    if config.publish:
        output_dir.mkdir(parents=True, exist_ok=True)
        image_path = output_dir / config.image_name
        report_path = output_dir / config.report_name
        image_bytes = _png_bytes(fused, sample.version_id)
        image_publication = export_bytes_if_version_changed(
            image_path,
            image_bytes,
            sample.version_id,
            read_png_version(image_path),
        )
        report["publication"] = {
            "image": image_publication,
            "report_path": str(report_path),
        }
        report_bytes = (
            __import__("json").dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        report_publication = export_bytes_if_version_changed(
            report_path,
            report_bytes,
            sample.version_id,
            read_json_report_version(report_path),
        )
        report["publication"]["report"] = report_publication

    return report
