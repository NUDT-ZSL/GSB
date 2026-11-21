"""运行编排：清单、注入、资源守卫、逐任务流水、误差谱、快照、报告。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

from . import __version__
from .errors import StageError
from .injection import load_anomalies, apply_anomalies, unattributed_record
from .pipeline import run_task_pipeline, boundary_mask, to_rgb_uint8
from .resources import apply_resource_limits, plan_parallel_batches
from .snapshots import (
    SnapshotStore, SNAPSHOT_CONFIG, SNAPSHOT_QUANT, SNAPSHOT_ERROR_SPECTRUM,
)
from .spectrum import compute_error_spectrum
from .tasks import load_manifest
from .thresholds import classify_residuals, top_k_candidates

RECENT_CODES_LIMIT = 16


def _array_hash(arr) -> str:
    a = np.ascontiguousarray(np.asarray(arr))
    return hashlib.sha256(a.tobytes()).hexdigest()


def _json_default(obj):
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return str(obj)


def _run_single_task(task, cfg, image_dir):
    """执行单任务并汇总诊断；StageError 向上抛出由运行器归因。"""
    active, reference, boundary_metrics = run_task_pipeline(task, cfg)
    mask = boundary_mask(task.latent_shape, task.block_grid)
    spectrum = compute_error_spectrum(active, reference, mask, cfg.quantiles)

    policy = cfg.threshold_policy
    residuals = np.abs(
        np.asarray(active["normed"]) - np.asarray(reference["normed"]))[mask]
    gate = classify_residuals(residuals, policy.boundary_residual_threshold, policy)

    candidates = top_k_candidates(np.abs(np.asarray(active["quantized"])),
                                  cfg.top_k, policy)

    rgb = to_rgb_uint8(active["decoded"])
    image_dir.mkdir(parents=True, exist_ok=True)
    image_path = image_dir / f"{task.task_id}.png"
    Image.fromarray(rgb, mode="RGB").save(image_path)

    return {
        "task_id": task.task_id,
        "status": "ok",
        "degraded": task.degraded,
        "degradation_category": task.degradation_category,
        "block_grid": list(task.block_grid),
        "original_block_grid": task.original_block_grid,
        "dtype_profile": active["profile"],
        "output_hashes": {
            "cond": _array_hash(active["cond"]),
            "latent": _array_hash(active["normed"]),
            "image": _array_hash(rgb),
        },
        "boundary_spectrum": boundary_metrics,
        "boundary_gate": gate,
        "error_spectrum": spectrum["stages"],
        "attribution": spectrum["attribution"],
        "top_k_candidates": candidates,
        "image_path": str(image_path),
    }


def run_workbench(cfg, manifest_path, anomalies_path=None, report_path=None,
                  image_subdir="images"):
    """运行整个诊断工作台，返回报告 dict 并写盘。"""
    tasks = load_manifest(manifest_path)
    anomaly_table = load_anomalies(anomalies_path)
    injections_applied = apply_anomalies(tasks, anomaly_table)

    limits = cfg.resource_limits
    batch_plan = plan_parallel_batches(len(tasks), limits)

    task_results = []
    error_records = []
    degradation_records = []
    if batch_plan["degraded"]:
        degradation_records.append({
            "task_id": "*",
            "category": batch_plan["category"],
            "reason": f"tasks {len(tasks)} > max_parallel_tasks "
                      f"{limits.max_parallel_tasks}",
            "batch_count": batch_plan["batch_count"],
        })

    image_dir = Path(cfg.cache_dir) / image_subdir
    for batch in batch_plan["batches"]:  # 分批顺序执行，确定性顺序
        for idx in batch:
            task = tasks[idx]
            try:
                degradation_records.extend(apply_resource_limits(task, limits))
                task_results.append(_run_single_task(task, cfg, image_dir))
            except StageError as exc:
                error_records.append(exc.to_record())
            except Exception as exc:  # 未被任何阶段归因
                error_records.append(unattributed_record(task.task_id, exc))

    # 快照：配置 / 量化参数 / 误差谱统计（整体替换、版本单调递增）
    store = SnapshotStore(cfg.snapshot_dir)
    config_snap = store.update(SNAPSHOT_CONFIG, {
        "workbench_version": __version__,
        "config": cfg.to_dict(),
    })
    quant_params = sorted({
        (t.dtype_profile, float(t.quant_scale), t.rounding_mode)
        for t in tasks
    })
    quant_snap = store.update(SNAPSHOT_QUANT, {
        "entries": [
            {"dtype_profile": p, "quant_scale": s, "rounding_mode": r}
            for p, s, r in quant_params
        ],
    })
    spectrum_stats = _aggregate_spectrum(task_results)
    spectrum_snap = store.update(SNAPSHOT_ERROR_SPECTRUM, spectrum_stats)

    # 门限策略留痕：与上一快照中的策略指纹比对
    policy_trace = {
        "current": cfg.threshold_policy.fingerprint(),
        "snapshot_version": config_snap["version"],
    }

    recent_codes = [r["code"] for r in error_records][-RECENT_CODES_LIMIT:]
    report = {
        "workbench_version": __version__,
        "manifest": str(manifest_path),
        "anomalies": str(anomalies_path) if anomalies_path else None,
        "task_count": len(tasks),
        "ok_count": len(task_results),
        "error_count": len(error_records),
        "threshold_policy_trace": policy_trace,
        "parallel_plan": {k: v for k, v in batch_plan.items() if k != "batches"},
        "degradations": degradation_records,
        "injections_applied": injections_applied,
        "tasks": task_results,
        "error_records": error_records,
        "unattributed_errors": [r for r in error_records
                                if r["stage"] == "unattributed"],
        "snapshots": {
            SNAPSHOT_CONFIG: {"version": config_snap["version"],
                              "checksum": config_snap["checksum"]},
            SNAPSHOT_QUANT: {"version": quant_snap["version"],
                             "checksum": quant_snap["checksum"]},
            SNAPSHOT_ERROR_SPECTRUM: {"version": spectrum_snap["version"],
                                      "checksum": spectrum_snap["checksum"]},
        },
        "recent_error_codes": recent_codes,
    }
    report["report_hash"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, default=_json_default
                   ).encode("utf-8")).hexdigest()

    if report_path is None:
        report_path = Path(cfg.report_dir) / "report.json"
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True,
                  ensure_ascii=False, default=_json_default)
    return report


def _aggregate_spectrum(task_results) -> dict:
    """跨任务聚合误差谱统计（确定性：按 task_id 排序）。"""
    from .spectrum import SPECTRUM_STAGES
    agg = {name: [] for name in SPECTRUM_STAGES}
    for res in sorted(task_results, key=lambda r: r["task_id"]):
        for name in SPECTRUM_STAGES:
            agg[name].append(res["error_spectrum"][name]["max_abs_dev"])
    stats = {}
    for name, values in agg.items():
        arr = np.asarray(values, dtype=np.float64) if values else np.zeros(1)
        stats[name] = {
            "task_count": len(values),
            "mean_max_abs_dev": float(arr.mean()),
            "worst_max_abs_dev": float(arr.max()),
        }
    dominant_counts = {}
    for res in task_results:
        src = res["attribution"]["dominant_source"]
        dominant_counts[src] = dominant_counts.get(src, 0) + 1
    return {"stages": stats, "dominant_source_counts": dominant_counts}
