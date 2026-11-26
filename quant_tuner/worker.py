from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np

from .estimator import estimate_candidate
from .workload import (
    compute_quant_report,
    deterministic_arrays,
    qbounds,
    quantize_dequantize,
)


def tiled_matmul(activation: np.ndarray, weight: np.ndarray, schedule: dict[str, Any]) -> np.ndarray:
    m, k = activation.shape
    k2, n = weight.shape
    if k != k2:
        raise ValueError("matmul shapes do not align")
    tile_m = max(1, int(schedule.get("tile_m", 16)))
    tile_n = max(1, int(schedule.get("tile_n", 16)))
    tile_k = max(1, int(schedule.get("tile_k", 16)))
    output = np.zeros((m, n), dtype=np.float64)
    for m0 in range(0, m, tile_m):
        m1 = min(m, m0 + tile_m)
        for n0 in range(0, n, tile_n):
            n1 = min(n, n0 + tile_n)
            accumulator = np.zeros((m1 - m0, n1 - n0), dtype=np.float64)
            for k0 in range(0, k, tile_k):
                k1 = min(k, k0 + tile_k)
                accumulator += activation[m0:m1, k0:k1] @ weight[k0:k1, n0:n1]
            output[m0:m1, n0:n1] += accumulator
    return output


def conv_im2col(activation: np.ndarray, shape: dict[str, Any]) -> np.ndarray:
    batch, height, width, in_c = activation.shape
    kh, kw = int(shape["kernel_h"]), int(shape["kernel_w"])
    stride, padding = int(shape.get("stride", 1)), int(shape.get("padding", 0))
    dilation = int(shape.get("dilation", 1))
    padded = np.pad(
        activation,
        ((0, 0), (padding, padding), (padding, padding), (0, 0)),
        mode="constant",
    )
    if dilation != 1:
        # Deterministic fallback for the uncommon dilated sample path.
        effective_kh = dilation * (kh - 1) + 1
        effective_kw = dilation * (kw - 1) + 1
        out_h = (height + 2 * padding - effective_kh) // stride + 1
        out_w = (width + 2 * padding - effective_kw) // stride + 1
        columns = np.zeros((batch * out_h * out_w, in_c * kh * kw), dtype=np.float64)
        row = 0
        for n_index in range(batch):
            for oh in range(out_h):
                for ow in range(out_w):
                    col = []
                    for c_index in range(in_c):
                        for i in range(kh):
                            for j in range(kw):
                                h = oh * stride + i * dilation
                                w = ow * stride + j * dilation
                                col.append(padded[n_index, h, w, c_index])
                    columns[row] = np.asarray(col)
                    row += 1
        return columns

    windows = np.lib.stride_tricks.sliding_window_view(
        padded, (kh, kw), axis=(1, 2)
    )
    windows = windows[:, ::stride, ::stride, :, :, :]
    n_dim, out_h, out_w, c_dim, _, _ = windows.shape
    return windows.reshape(n_dim * out_h * out_w, c_dim * kh * kw)


def run_scheduled_kernel(
    op_kind: str,
    shape: dict[str, Any],
    activation: np.ndarray,
    weight: np.ndarray,
    schedule: dict[str, Any],
) -> np.ndarray:
    if op_kind == "matmul":
        return tiled_matmul(activation, weight, schedule)

    columns = conv_im2col(activation, shape)
    kh, kw = int(shape["kernel_h"]), int(shape["kernel_w"])
    in_c = int(shape["in_channels"])
    out_c = int(shape["out_channels"])
    kernel_matrix = np.transpose(weight, (2, 0, 1, 3)).reshape(
        in_c * kh * kw, out_c
    )
    return tiled_matmul(columns, kernel_matrix, schedule)


def evaluate_point(request: dict[str, Any]) -> dict[str, Any]:
    """Top-level ProcessPool worker; request/result are JSON-serializable dicts."""
    staging_path = request.get("staging_path")
    if staging_path:
        path = Path(staging_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"point_id": request["point_id"], "phase": "started"},
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    tuning_id = request["tuning_id"]
    op_kind = request["op_kind"]
    shape = request["workload_shape"]
    quant_config = request["quant_config"]
    bits = int(request.get("effective_bits", quant_config.get("bits", 8)))
    schedule = request["schedule"]
    platform = request["platform"]

    report = compute_quant_report(tuning_id, op_kind, shape, quant_config, bits)
    estimate = estimate_candidate(
        op_kind,
        shape,
        schedule,
        platform,
        bits,
        request.get("active_threads_in_batch"),
    )
    arrays = deterministic_arrays(tuning_id, op_kind, shape)
    scale = float(quant_config["scale"])
    zero_point = int(quant_config["zero_point"])
    qmin, qmax = qbounds(bits, quant_config)

    def iteration() -> None:
        dequantized: dict[str, np.ndarray] = {}
        for role in ("activation", "weight"):
            _, restored = quantize_dequantize(
                arrays[role], scale, zero_point, bits, qmin, qmax
            )
            dequantized[role] = restored
        result = run_scheduled_kernel(
            op_kind,
            shape,
            dequantized["activation"],
            dequantized["weight"],
            schedule,
        )
        # A cheap data-dependent sink prevents dead-code elimination.
        if result.size and result[0, 0] == 123456789.123:
            raise RuntimeError("unreachable deterministic sink")

    warmup = int(request.get("warmup_repeats", 1))
    repeats = int(request.get("timing_repeats", 5))
    for _ in range(max(0, warmup)):
        iteration()

    samples_ns: list[int] = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        iteration()
        samples_ns.append(time.perf_counter_ns() - started)

    elapsed_ns = int(statistics.median(samples_ns))
    result = {
        "point_id": request["point_id"],
        "quant_report": report,
        "estimate": estimate,
        "measured": {
            "elapsed_ns_median": elapsed_ns,
            "latency_us": elapsed_ns / 1000.0,
            "samples_ns": samples_ns,
            "timing_repeats": repeats,
            "timer": "time.perf_counter_ns+median",
        },
    }
    if staging_path:
        Path(staging_path).write_text(
            json.dumps({"point_id": request["point_id"], "phase": "finished_uncommitted"}),
            encoding="utf-8",
        )
    return result
