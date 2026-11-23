from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np

from .compute import (
    compare_arrays,
    decode_array,
    evaluate_output_consistency,
    evaluate_stat_consistency,
    rebuild_statistics_from_blocks,
)
from .models import JobSpec, ToleranceConfig
from .util import atomic_write_bytes, clamp_positions, npy_bytes, relative_to_root, sha256_bytes


def _augment_positions(summary: dict[str, Any], shape: tuple[int, ...]) -> None:
    summary["max_abs_position"] = (
        [int(item) for item in np.unravel_index(int(summary["max_abs_flat_index"]), shape)]
        if shape
        else []
    )
    summary["max_rel_position"] = (
        [int(item) for item in np.unravel_index(int(summary["max_rel_flat_index"]), shape)]
        if shape
        else []
    )
    summary["exceed_positions"] = clamp_positions(
        summary["exceed_flat_indices"], shape
    )


def _attach_spectrum(summary: dict[str, Any], block: np.ndarray, whole: np.ndarray) -> None:
    block_flat = np.asarray(block, dtype=np.float64).reshape(-1)
    whole_flat = np.asarray(whole, dtype=np.float64).reshape(-1)
    abs_dev = np.abs(block_flat - whole_flat)
    rel_dev = abs_dev / np.maximum(np.abs(whole_flat), np.finfo(np.float64).tiny)
    if abs_dev.size == 0:
        summary["spectrum"] = []
        return
    count = min(64, abs_dev.size)
    indices = np.argsort(abs_dev, kind="stable")[-count:][::-1]
    spectrum = []
    for flat in indices.tolist():
        spectrum.append(
            {
                "flat_index": int(flat),
                "position": [int(item) for item in np.unravel_index(int(flat), block.shape)],
                "abs": float(abs_dev[flat]),
                "rel": float(rel_dev[flat]),
                "block_value": float(block_flat[flat]),
                "whole_value": float(whole_flat[flat]),
            }
        )
    summary["spectrum"] = spectrum


def _write_deviation_detail(
    path: Path,
    block: np.ndarray,
    whole: np.ndarray,
) -> dict[str, Any]:
    block64 = np.asarray(block, dtype=np.float64)
    whole64 = np.asarray(whole, dtype=np.float64)
    abs_dev = np.abs(block64 - whole64)
    rel_dev = abs_dev / np.maximum(np.abs(whole64), np.finfo(np.float64).tiny)
    buffer = io.BytesIO()
    np.savez(
        buffer,
        block=block64,
        whole=whole64,
        abs_dev=abs_dev,
        rel_dev=rel_dev,
    )
    data = buffer.getvalue()
    atomic_write_bytes(path, data)
    return {
        "path": str(path),
        "sha256": sha256_bytes(data),
        "element_count": int(abs_dev.size),
    }


def _stat_rebuild_check(
    job: JobSpec,
    block_payload: dict[str, Any],
    normalize_payload: dict[str, Any],
    tolerance: ToleranceConfig,
) -> tuple[bool, dict[str, Any]]:
    rebuilt = rebuild_statistics_from_blocks(job.op_kind, block_payload)
    stored = {
        name: decode_array(value)
        for name, value in normalize_payload["merged_stats"].items()
    }
    checks: dict[str, Any] = {}
    passed = True
    for name in ("max", "denominator") if job.op_kind == "softmax" else ("mean", "variance"):
        comparison = compare_arrays(rebuilt[name], stored[name], tolerance, stat=True)
        checks[name] = comparison
        if comparison["exceed_count"]:
            passed = False
    return passed, {
        "passed": passed,
        "description": "block statistics re-merged and compared with normalize-stage statistics",
        "fields": checks,
    }


def build_job_diagnostics(
    job: JobSpec,
    stage_payloads: dict[str, dict[str, Any]],
    tolerance: ToleranceConfig,
    deviation_path: Path,
    *,
    root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    block_payload = stage_payloads["block_stats"]
    normalize_payload = stage_payloads["normalize"]
    output_payload = stage_payloads["output"]

    stat_passed, stat_report = evaluate_stat_consistency(job, normalize_payload, tolerance)
    if not stat_passed:
        issues.append(
            {
                "type": "error",
                "source": "diagnostic",
                "code": "E_STAT_MISMATCH",
                "message": "merged block statistics disagree with whole-path statistics",
                "stage": "normalize",
                "job_id": job.job_id,
                "field": "statistics",
                "details": stat_report,
            }
        )

    rebuild_passed, rebuild_report = _stat_rebuild_check(
        job, block_payload, normalize_payload, tolerance
    )
    if not rebuild_passed:
        issues.append(
            {
                "type": "error",
                "source": "diagnostic",
                "code": "E_STAT_MISMATCH",
                "message": "block-detail re-merge does not reproduce normalize-stage statistics",
                "stage": "normalize",
                "job_id": job.job_id,
                "field": "merge_rule",
                "details": rebuild_report,
            }
        )

    output_passed, deviation = evaluate_output_consistency(job, output_payload, tolerance)
    block = decode_array(output_payload["block_output_f64"])
    whole = decode_array(output_payload["whole_output_f64"])
    _augment_positions(deviation, tuple(block.shape))
    _attach_spectrum(deviation, block, whole)
    detail = _write_deviation_detail(deviation_path, block, whole)
    detail["report_path"] = relative_to_root(deviation_path, root)
    if not output_passed:
        issues.append(
            {
                "type": "error",
                "source": "diagnostic",
                "code": "E_TOLERANCE_EXCEEDED",
                "message": "block path and whole path exceed declared float64 tolerance",
                "stage": "output",
                "job_id": job.job_id,
                "field": "output",
                "details": deviation,
            }
        )
    output_array = block.astype(np.dtype(output_payload["output_dtype"]), copy=False)
    output_sha = sha256_bytes(npy_bytes(output_array))
    if output_sha != output_payload["output_npy_sha256"]:
        issues.append(
            {
                "type": "error",
                "source": "diagnostic",
                "code": "E_CACHE_CORRUPT",
                "message": "cached output bytes do not match recorded output checksum",
                "stage": "output",
                "job_id": job.job_id,
                "field": "output_npy_sha256",
                "details": {
                    "recorded": output_payload["output_npy_sha256"],
                    "actual": output_sha,
                },
            }
        )

    report = {
        "tolerance": tolerance.to_public(),
        "stat_consistency": stat_report,
        "stat_rebuild": rebuild_report,
        "deviation": deviation,
        "deviation_detail": detail,
        "byte_identical_to_whole_path": bool(output_payload["byte_identical_to_whole_path"]),
        "output_sha256": output_sha,
        "passed": not issues,
        "issue_codes": [issue["code"] for issue in issues],
    }
    return report, issues


def load_deviation_detail(path: Path) -> dict[str, np.ndarray]:
    try:
        with np.load(path, allow_pickle=False) as data:
            return {name: data[name].copy() for name in data.files}
    except (OSError, ValueError) as exc:
        from .errors import BusinessError

        raise BusinessError(
            "E_INCONSISTENT",
            f"cannot load deviation detail {path}: {exc}",
            field="deviation_detail",
            stage="selfcheck",
        ) from exc

