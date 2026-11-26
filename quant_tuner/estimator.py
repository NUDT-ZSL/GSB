from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .errors import E_FORMAT_CORRUPT, E_NOT_FOUND, TuningError
from .workload import normalize_shape


def load_platform_stats(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise TuningError(E_NOT_FOUND, f"platform stats not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        stats = json.load(handle)
    for field in ("cache", "throughput", "thread_occupancy", "frequency_mhz"):
        if field not in stats:
            raise TuningError(E_FORMAT_CORRUPT, f"platform stats missing {field}")
    return stats


def workload_counters(
    op_kind: str, shape: dict[str, Any], schedule: dict[str, Any], bits: int = 8
) -> dict[str, int | float]:
    dims = normalize_shape(op_kind, shape)
    bytes_per_quant = max(1, bits // 8)
    tile_m = max(1, int(schedule.get("tile_m", 16)))
    tile_n = max(1, int(schedule.get("tile_n", 16)))
    tile_k = max(1, int(schedule.get("tile_k", 16)))

    if op_kind == "matmul":
        m, n, k = dims["M"], dims["N"], dims["K"]
        macs = m * n * k * int(shape.get("batch", 1))
        activation_elements = m * k
        weight_elements = k * n
        output_elements = m * n
        gemm_m, gemm_n, gemm_k = m, n, k
    else:
        batch = dims["batch"]
        oh, ow = dims["out_height"], dims["out_width"]
        in_c, out_c = dims["in_channels"], dims["out_channels"]
        kh, kw = dims["kernel_h"], dims["kernel_w"]
        gemm_m = batch * oh * ow
        gemm_n = out_c
        gemm_k = in_c * kh * kw
        macs = gemm_m * gemm_n * gemm_k
        activation_elements = (
            batch * dims["height"] * dims["width"] * in_c
            + batch * oh * ow * gemm_k
        )
        weight_elements = kh * kw * in_c * out_c
        output_elements = gemm_m * gemm_n

    input_elements = activation_elements + weight_elements
    quant_bytes = input_elements * bytes_per_quant
    dequant_temp_bytes = input_elements * 8
    output_bytes = output_elements * 8
    tile_bytes = 8 * (tile_m * tile_k + tile_k * tile_n + tile_m * tile_n)
    dequant_ops_per_element = 2 if bits <= 8 else 4
    dequant_ops = input_elements * dequant_ops_per_element

    return {
        "macs": int(macs),
        "input_elements": int(input_elements),
        "output_elements": int(output_elements),
        "quant_bytes": int(quant_bytes),
        "dequant_temp_bytes": int(dequant_temp_bytes),
        "output_bytes": int(output_bytes),
        "tile_bytes": int(tile_bytes),
        "dequant_ops": int(dequant_ops),
        "total_compute_ops": int(macs * 2 + dequant_ops),
        "gemm_m": gemm_m,
        "gemm_n": gemm_n,
        "gemm_k": gemm_k,
        "tile_iterations": math.ceil(gemm_m / tile_m)
        * math.ceil(gemm_n / tile_n)
        * math.ceil(gemm_k / tile_k),
    }


def estimate_candidate(
    op_kind: str,
    shape: dict[str, Any],
    schedule: dict[str, Any],
    platform: dict[str, Any],
    bits: int = 8,
    active_threads_in_batch: int | None = None,
) -> dict[str, Any]:
    counters = workload_counters(op_kind, shape, schedule, bits)
    occupancy = platform["thread_occupancy"]
    cores = int(occupancy.get("cores", 1))
    threads = max(1, int(schedule.get("threads", 1)))
    usable_threads = min(threads, cores)
    active_threads = int(active_threads_in_batch or threads)

    quant_memory_mb = (
        counters["quant_bytes"]
        + counters["dequant_temp_bytes"]
        + counters["output_bytes"]
        + counters["tile_bytes"]
    ) / (1024 * 1024)
    resident_mb = (
        float(occupancy.get("base_process_mb", 8.0))
        + float(occupancy.get("per_worker_mb", 4.0))
        + threads * float(occupancy.get("per_thread_mb", 1.0))
        + quant_memory_mb
    )

    throughput_cfg = platform["throughput"][op_kind]
    mac_per_cycle = float(throughput_cfg["mac_per_cycle_per_core"])
    efficiency = float(throughput_cfg.get("efficiency", 1.0))
    frequency_mhz = float(platform["frequency_mhz"])
    compute_us = counters["macs"] / (
        mac_per_cycle * frequency_mhz * usable_threads * max(0.01, efficiency)
    )
    dequant_us = counters["dequant_ops"] / (
        float(platform.get("dequant_ops_per_cycle_per_core", 8.0))
        * frequency_mhz
        * usable_threads
    )
    bandwidth_gbps = float(platform.get("memory_bandwidth_gbps", 20.0))
    memory_bytes = (
        counters["quant_bytes"]
        + counters["dequant_temp_bytes"]
        + counters["output_bytes"]
    )
    memory_us = memory_bytes / max(0.1, bandwidth_gbps * 1000.0)

    footprint_kb = (
        counters["quant_bytes"]
        + counters["tile_bytes"]
        + counters["output_bytes"]
    ) / 1024.0
    cache_penalty_us = 0.0
    cache_breakdown: list[dict[str, Any]] = []
    for cache in platform["cache"]:
        size_kb = float(cache["size_kb"])
        if footprint_kb > size_kb:
            spill_mb = max(0.0, footprint_kb - size_kb) / 1024.0
            penalty = spill_mb * float(cache.get("spill_cost_us_per_mb", 0.1))
            cache_penalty_us += penalty
            cache_breakdown.append(
                {"level": cache["level"], "spill_mb": spill_mb, "penalty_us": penalty}
            )

    tile_dispatch_overhead_us = float(
        schedule.get(
            "tile_dispatch_overhead_us",
            platform.get("tile_dispatch_overhead_us", 1.25),
        )
    )
    schedule_overhead_us = counters["tile_iterations"] * tile_dispatch_overhead_us
    interference = 1.0 + float(occupancy.get("interference_penalty", 0.05)) * max(
        0, active_threads - cores
    )
    expected_latency_us = (
        max(compute_us + dequant_us, memory_us) + cache_penalty_us + schedule_overhead_us
    ) * interference

    return {
        **counters,
        "threads": threads,
        "usable_threads": usable_threads,
        "active_threads_in_batch": active_threads,
        "interference_factor": interference,
        "resident_memory_mb": resident_mb,
        "footprint_kb": footprint_kb,
        "compute_us": compute_us,
        "dequant_us": dequant_us,
        "memory_us": memory_us,
        "cache_penalty_us": cache_penalty_us,
        "cache_breakdown": cache_breakdown,
        "schedule_overhead_us": schedule_overhead_us,
        "expected_latency_us": expected_latency_us,
        "mac_budget": counters["macs"],
    }


def batch_memory_mb(estimates: list[dict[str, Any]], occupancy: dict[str, Any]) -> float:
    if not estimates:
        return 0.0
    # resident_memory_mb already contains the per-worker and per-thread costs.
    return sum(item["resident_memory_mb"] for item in estimates)


def batch_interference(
    schedules: list[dict[str, Any]], platform: dict[str, Any]
) -> float:
    occupancy = platform["thread_occupancy"]
    cores = int(occupancy.get("cores", 1))
    total_threads = sum(max(1, int(schedule.get("threads", 1))) for schedule in schedules)
    return 1.0 + float(occupancy.get("interference_penalty", 0.05)) * max(
        0, total_threads - cores
    )
