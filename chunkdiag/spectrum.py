"""Deviation spectrum probes and adjudication."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .partition import Layout
from .reference import (
    _align_corners_false,
    _clamped,
    _zero_pad,
    reference_bicubic,
    reference_bilinear,
    reference_conv2d,
    reference_interpolate,
)
from .schema import NormalizedJob


def _quantiles(values: np.ndarray) -> dict[str, float]:
    if values.size == 0:
        return {"p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0}
    p50, p90, p95, p99 = np.quantile(
        values, [0.50, 0.90, 0.95, 0.99], method="linear"
    )
    return {
        "p50": float(p50),
        "p90": float(p90),
        "p95": float(p95),
        "p99": float(p99),
    }


def _base_stats(values: np.ndarray) -> dict[str, Any]:
    if values.size == 0:
        return {
            "count": 0,
            "quantiles": _quantiles(values),
            "max_abs": 0.0,
            "mean_abs": 0.0,
            "rms_abs": 0.0,
            "sum_abs": 0.0,
        }
    return {
        "count": int(values.size),
        "quantiles": _quantiles(values),
        "max_abs": float(np.max(values)),
        "mean_abs": float(np.mean(values)),
        "rms_abs": float(np.sqrt(np.mean(np.square(values)))),
        "sum_abs": float(np.sum(values)),
    }


def _detail_payload(values: np.ndarray, limit: int) -> dict[str, Any]:
    flat = np.asarray(values, dtype=np.float64).ravel()
    complete = flat.size <= limit
    if complete:
        selected = flat
        sampling = "all_values"
    else:
        indices = np.linspace(0, flat.size - 1, num=limit, dtype=np.int64)
        selected = flat[indices]
        sampling = "deterministic_even_subsample"
    return {
        "count": int(selected.size),
        "complete": complete,
        "sampling": sampling,
        "values": selected.tolist(),
    }


def _source(
    *,
    name: str,
    definition: str,
    signal_type: str,
    values: np.ndarray,
    observed_p95: float,
    detail_limit: int,
) -> dict[str, Any]:
    stats = _base_stats(values)
    source_p95 = stats["quantiles"]["p95"]
    if observed_p95 > 0:
        amplification: float | None = source_p95 / observed_p95
    elif source_p95 == 0:
        amplification = 1.0
    else:
        amplification = None
    return {
        "name": name,
        "definition": definition,
        "signal_type": signal_type,
        **stats,
        "error_amplification_ratio": amplification,
        "amplification_definition": (
            "source p95(abs error) / observed chunk-vs-reference p95(abs error); "
            "None means observed is zero while the probe is nonzero"
        ),
        "details": _detail_payload(values, detail_limit),
    }


def _block_boundary_mask(job: NormalizedJob, layout: Layout) -> np.ndarray:
    batch = job.output_shape[0]
    if job.family == "conv":
        out_h, out_w = job.output_shape[2], job.output_shape[3]
    else:
        out_h, out_w = job.output_shape[1], job.output_shape[2]
    mask = np.zeros((batch, out_h, out_w), dtype=bool)
    for block in layout.blocks:
        mask[block.batch, block.h0, block.w0 : block.w1] = True
        mask[block.batch, block.h1 - 1, block.w0 : block.w1] = True
        mask[block.batch, block.h0 : block.h1, block.w0] = True
        mask[block.batch, block.h0 : block.h1, block.w1 - 1] = True
    return mask


def _conv_padding_mask(job: NormalizedJob) -> np.ndarray:
    assert job.kernel_shape is not None
    n, height, width, _ = job.input_shape
    out_h, out_w = job.output_shape[2], job.output_shape[3]
    stride_h, stride_w = job.stride
    kernel_h, kernel_w = job.kernel_shape[2], job.kernel_shape[3]
    top, _, left, _ = job.padding
    mask = np.zeros((n, out_h, out_w), dtype=bool)
    for batch in range(n):
        for oh in range(out_h):
            for ow in range(out_w):
                for kh in range(kernel_h):
                    ih = oh * stride_h - top + kh
                    if ih < 0 or ih >= height:
                        mask[batch, oh, ow] = True
                        break
                if mask[batch, oh, ow]:
                    continue
                for kw in range(kernel_w):
                    iw = ow * stride_w - left + kw
                    if iw < 0 or iw >= width:
                        mask[batch, oh, ow] = True
                        break
    return mask


def _interp_oob_mask(job: NormalizedJob) -> np.ndarray:
    assert job.scale is not None
    n, out_h, out_w, _ = job.output_shape
    padded_h = job.input_shape[1] + job.padding[0] + job.padding[1]
    padded_w = job.input_shape[2] + job.padding[2] + job.padding[3]
    scale_h, scale_w = job.scale
    mask = np.zeros((n, out_h, out_w), dtype=bool)
    offsets = (-1, 1) if job.interp_mode == "bilinear" else (-1, 2)
    if job.interp_mode == "nearest":
        offsets = (0, 0)
    for batch in range(n):
        for oh in range(out_h):
            source_y = _align_corners_false(oh, scale_h)
            if job.interp_mode == "nearest":
                y0 = math.floor(source_y + 0.5)
            else:
                y0 = math.floor(source_y)
            for ow in range(out_w):
                source_x = _align_corners_false(ow, scale_w)
                if job.interp_mode == "nearest":
                    x0 = math.floor(source_x + 0.5)
                else:
                    x0 = math.floor(source_x)
                y_low = y0 + offsets[0]
                y_high = y0 + offsets[1]
                x_low = x0 + offsets[0]
                x_high = x0 + offsets[1]
                if (
                    y_low < 0
                    or y_high >= padded_h
                    or x_low < 0
                    or x_high >= padded_w
                ):
                    mask[batch, oh, ow] = True
    return mask


def _probe_conv_kahan(
    job: NormalizedJob,
    input_array: np.ndarray,
    weight_array: np.ndarray,
) -> np.ndarray:
    assert job.kernel_shape is not None
    n, height, width, _ = job.input_shape
    out_channels, channels, kernel_h, kernel_w = job.kernel_shape
    stride_h, stride_w = job.stride
    top, _, left, _ = job.padding
    out_h, out_w = job.output_shape[2], job.output_shape[3]
    source = np.asarray(input_array, dtype=np.float64)
    weights = np.asarray(weight_array, dtype=np.float64)
    output = np.empty((n, out_channels, out_h, out_w), dtype=np.float64)
    for batch in range(n):
        for out_channel in range(out_channels):
            for oh in range(out_h):
                for ow in range(out_w):
                    total = np.float64(0.0)
                    compensation = np.float64(0.0)
                    for in_channel in range(channels):
                        for kh in range(kernel_h):
                            ih = oh * stride_h - top + kh
                            if ih < 0 or ih >= height:
                                continue
                            for kw in range(kernel_w):
                                iw = ow * stride_w - left + kw
                                if iw < 0 or iw >= width:
                                    continue
                                term = np.float64(
                                    source[batch, ih, iw, in_channel]
                                ) * np.float64(
                                    weights[out_channel, in_channel, kh, kw]
                                )
                                corrected = np.float64(term - compensation)
                                updated = np.float64(total + corrected)
                                compensation = np.float64(
                                    (updated - total) - corrected
                                )
                                total = updated
                    output[batch, out_channel, oh, ow] = total
    return output


def _probe_interp_float32_weights(
    job: NormalizedJob, input_array: np.ndarray
) -> np.ndarray:
    if job.interp_mode == "nearest":
        return reference_interpolate(job, input_array)
    if job.interp_mode == "bilinear":
        return reference_bilinear(job, input_array, weight_dtype=np.float32)
    return reference_bicubic(job, input_array, weight_dtype=np.float32)


def build_deviation_spectrum(
    job: NormalizedJob,
    *,
    actual: np.ndarray,
    reference: np.ndarray,
    input_array: np.ndarray,
    weight_array: np.ndarray | None,
    layout: Layout,
    comparison: dict[str, Any],
) -> dict[str, Any]:
    observed = np.abs(
        np.asarray(actual, dtype=np.float64) - np.asarray(reference, dtype=np.float64)
    )
    observed_p95 = _quantiles(observed.ravel())["p95"]
    dims = observed.shape
    if job.family == "conv":
        spatial = observed[:, :, :, :]
        batch = job.output_shape[0]
    else:
        spatial = observed
        batch = job.output_shape[0]

    sources: dict[str, dict[str, Any]] = {}
    if job.family == "conv":
        boundary_mask = _block_boundary_mask(job, layout) | _conv_padding_mask(job)
        boundary_values = observed.transpose(0, 2, 3, 1)[boundary_mask].ravel()
        interior_values = observed.transpose(0, 2, 3, 1)[~boundary_mask].ravel()
        kahan_probe = _probe_conv_kahan(job, input_array, weight_array)  # type: ignore[arg-type]
        compensation_values = np.abs(
            kahan_probe - np.asarray(reference, dtype=np.float64)
        ).ravel()
        sources["block_boundary_oob"] = _source(
            name="block_boundary_oob",
            definition=(
                "observed chunk-vs-reference absolute error at block-edge or "
                "zero-padding-exposed output positions"
            ),
            signal_type="observed_region",
            values=boundary_values,
            observed_p95=observed_p95,
            detail_limit=job.spectrum_detail_limit,
        )
        sources["accumulation_order"] = _source(
            name="accumulation_order",
            definition=(
                "observed chunk-vs-reference absolute error at positions outside "
                "the block-edge/padding boundary region"
            ),
            signal_type="observed_region",
            values=interior_values,
            observed_p95=observed_p95,
            detail_limit=job.spectrum_detail_limit,
        )
        sources["compensated_sum"] = _source(
            name="compensated_sum",
            definition=(
                "counterfactual difference between a Kahan-compensated scalar "
                "probe and the independent float64 reference"
            ),
            signal_type="counterfactual_intervention",
            values=compensation_values,
            observed_p95=observed_p95,
            detail_limit=job.spectrum_detail_limit,
        )
    else:
        oob_mask = _interp_oob_mask(job)
        oob_values = observed[oob_mask].ravel()
        interior_values = observed[~oob_mask].ravel()
        weight_probe = _probe_interp_float32_weights(job, input_array)
        weight_values = np.abs(
            np.asarray(weight_probe, dtype=np.float64)
            - np.asarray(reference, dtype=np.float64)
        ).ravel()
        sources["weight_calculation"] = _source(
            name="weight_calculation",
            definition=(
                "counterfactual difference caused by computing interpolation "
                "weights in float32 before float64 accumulation"
            ),
            signal_type="counterfactual_intervention",
            values=weight_values,
            observed_p95=observed_p95,
            detail_limit=job.spectrum_detail_limit,
        )
        sources["oob_neighbor_selection"] = _source(
            name="oob_neighbor_selection",
            definition=(
                "observed chunk-vs-reference absolute error where a clamped "
                "neighbor index was required"
            ),
            signal_type="observed_region",
            values=oob_values,
            observed_p95=observed_p95,
            detail_limit=job.spectrum_detail_limit,
        )
        sources["accumulation_order"] = _source(
            name="accumulation_order",
            definition=(
                "observed chunk-vs-reference absolute error outside the explicit "
                "out-of-bounds neighbor region"
            ),
            signal_type="observed_region",
            values=interior_values,
            observed_p95=observed_p95,
            detail_limit=job.spectrum_detail_limit,
        )

    def source_key(item: tuple[str, dict[str, Any]]) -> tuple[float, float, str]:
        name, source = item
        return (
            source["quantiles"]["p95"],
            source["sum_abs"],
            name,
        )

    winner = max(sources.items(), key=source_key)
    if winner[1]["quantiles"]["p95"] == 0 and winner[1]["sum_abs"] == 0:
        dominant = "none"
    else:
        dominant = winner[0]

    return {
        "method": {
            "comparison": "actual float64-cast chunk output minus float64 reference",
            "sources_are_recomputable_from": "sources[*].details.values",
            "quantile_method": "NumPy linear interpolation",
            "dominant_rule": "largest source p95, then sum_abs, then source name",
            "boundary_scope": (
                "conv: block edges plus zero-padding-exposed outputs; "
                "interpolate: outputs requiring clamped neighbors"
            ),
            "observed": _base_stats(observed.ravel()),
        },
        "dominant_source": dominant,
        "sources": sources,
        "observed": {
            **_base_stats(observed.ravel()),
            "details": _detail_payload(observed.ravel(), job.spectrum_detail_limit),
        },
        "output_shape": list(dims),
    }


def adjudicate_deviations(value: Any) -> dict[str, Any]:
    """Recompute source statistics from detail arrays and verify attribution."""

    spectrum: dict[str, Any]
    if hasattr(value, "report"):
        spectrum = value.report["deviation_spectrum"]
    elif isinstance(value, dict) and "deviation_spectrum" in value:
        spectrum = value["deviation_spectrum"]
    elif isinstance(value, dict) and "sources" in value:
        spectrum = value
    else:
        raise TypeError("expected ExecutionResult or deviation spectrum mapping")

    sources = spectrum.get("sources", {})
    recomputed: dict[str, Any] = {}
    mismatches: list[str] = []
    for name, source in sources.items():
        detail = source.get("details", {})
        values = np.asarray(detail.get("values", []), dtype=np.float64)
        stats = _base_stats(values)
        recomputed[name] = stats
        for key in ("max_abs", "mean_abs", "sum_abs"):
            expected = float(source.get(key, 0.0))
            actual = float(stats[key])
            if not math.isclose(expected, actual, rel_tol=1e-12, abs_tol=1e-15):
                mismatches.append(f"{name}.{key}")
        for key, expected_value in source.get("quantiles", {}).items():
            actual_value = stats["quantiles"][key]
            if not math.isclose(
                float(expected_value), actual_value, rel_tol=1e-12, abs_tol=1e-15
            ):
                mismatches.append(f"{name}.quantiles.{key}")
        if not detail.get("complete", True):
            mismatches.append(f"{name}.details_incomplete")

    def key(item: tuple[str, dict[str, Any]]) -> tuple[float, float, str]:
        name, stats = item
        return (stats["quantiles"]["p95"], stats["sum_abs"], name)

    winner = max(recomputed.items(), key=key) if recomputed else ("none", None)
    dominant = winner[0]
    if winner[1] is not None and winner[1]["sum_abs"] == 0:
        dominant = "none"
    declared = spectrum.get("dominant_source", "none")
    if dominant != declared:
        mismatches.append("dominant_source")

    return {
        "verified": not mismatches,
        "declared_dominant_source": declared,
        "recomputed_dominant_source": dominant,
        "mismatches": mismatches,
        "recomputed": recomputed,
        "note": (
            "Statistics are recomputed from each source's detail values using "
            "the same linear quantile rule."
        ),
    }
