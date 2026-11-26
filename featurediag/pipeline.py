"""Four-stage diagnostic pipeline with atomic checkpoints and recovery."""

from __future__ import annotations

import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .aggregation import (
    aggregate_in_blocks,
    aggregate_monolithic,
    compare_block_results,
    pair_specs,
)
from .cache import (
    CacheIndex,
    aggregation_cache_key,
    normalization_cache_key,
    quantization_cache_key,
    spectrum_cache_key,
)
from .canonical import (
    array_hash,
    atomic_write_array,
    atomic_write_json,
    atomic_write_jsonl,
    canonical_json,
    file_sha256,
    read_array,
    read_json,
    sha256_bytes,
    sha256_json,
)
from .errors import BusinessError, SimulatedCrash, exit_code_for
from .models import Batch, RunConfig, Task
from .numeric import generate_samples, normalize_zscore, quantize
from .snapshot import Snapshot


STAGE_NAMES = ("normalize", "quantize", "aggregate", "spectrum")
STAGE_INDEX = {name: index for index, name in enumerate(STAGE_NAMES)}


@dataclass(frozen=True)
class RunPaths:
    root: Path
    run_id: str

    @property
    def run_dir(self) -> Path:
        return self.root / "artifacts" / "runs" / self.run_id

    @property
    def checkpoint_dir(self) -> Path:
        return self.run_dir / "checkpoints"

    @property
    def ledger_path(self) -> Path:
        return self.run_dir / "error_ledger.jsonl"

    @property
    def state_path(self) -> Path:
        return self.run_dir / "state.json"

    @property
    def recovery_report_path(self) -> Path:
        return self.run_dir / "recovery_report.json"

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "run_manifest.json"

    @property
    def cache_index_path(self) -> Path:
        return self.root / "artifacts" / "cache" / "index.json"


def _root_relative(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def _resolve_path(root: Path, relative: str) -> Path:
    return root / relative


def _all_batch_ids(batches: list[Batch]) -> list[str]:
    return [batch.batch_id for batch in sorted(batches, key=lambda item: item.batch_id)]


def run_fingerprint(
    tasks: dict[str, Task], batches: list[Batch], snapshot: Snapshot, config: RunConfig
) -> str:
    return sha256_json(
        {
            "tasks": [tasks[key].identity() for key in sorted(tasks)],
            "batches": [batch.identity() for batch in sorted(batches, key=lambda item: item.batch_id)],
            "snapshot": {
                "version": snapshot.snapshot_version,
                "checksum": snapshot.checksum(),
                "id": snapshot.snapshot_id,
                "clock": snapshot.logical_clock_ms,
            },
            "config": {
                "master_seed": config.master_seed,
                "tolerance": config.tolerance.to_dict(),
                "reduction_rule": config.reduction_rule,
            },
        }
    )


def _checkpoint_path(paths: RunPaths, stage_index: int) -> Path:
    return paths.checkpoint_dir / f"stage_{stage_index}.json"


def _load_checkpoint_chain(
    paths: RunPaths, expected_batches: list[str]
) -> tuple[list[dict[str, Any]], list[Path]]:
    chain: list[dict[str, Any]] = []
    payloads: list[Path] = []
    previous_version = 0
    expected_set = set(expected_batches)
    for stage_index in range(len(STAGE_NAMES)):
        checkpoint_path = _checkpoint_path(paths, stage_index)
        if not checkpoint_path.exists():
            break
        try:
            checkpoint = read_json(checkpoint_path)
        except Exception as exc:
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                f"cannot read checkpoint {checkpoint_path}: {exc}",
            ) from exc
        if not isinstance(checkpoint, dict):
            raise BusinessError("E_FORMAT_CORRUPT", "checkpoint must be an object")
        if checkpoint.get("stage") != STAGE_NAMES[stage_index]:
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                "checkpoint stage name does not match its slot",
                context={"path": str(checkpoint_path), "stage": checkpoint.get("stage")},
            )
        version = checkpoint.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise BusinessError("E_FORMAT_CORRUPT", "checkpoint version must be an integer")
        if version <= previous_version:
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                "checkpoint versions are not strictly increasing",
                context={"previous_version": previous_version, "version": version},
            )
        completed = checkpoint.get("completed_batches")
        if not isinstance(completed, list) or not set(completed) == expected_set:
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                "checkpoint completed_batches does not cover the full batch stream",
                context={
                    "path": str(checkpoint_path),
                    "expected": sorted(expected_set),
                    "completed": completed,
                },
            )
        if checkpoint.get("complete") is not True:
            raise BusinessError("E_FORMAT_CORRUPT", "checkpoint is not marked complete")
        payload_file = checkpoint.get("payload_file")
        if not isinstance(payload_file, str):
            raise BusinessError("E_FORMAT_CORRUPT", "checkpoint payload_file is missing")
        payload_path = _resolve_path(paths.root, payload_file)
        if not payload_path.exists():
            raise BusinessError(
                "E_FORMAT_CORRUPT", f"checkpoint payload is missing: {payload_path}"
            )
        try:
            payload_bytes = payload_path.read_bytes()
            payload = read_json(payload_path)
        except Exception as exc:
            raise BusinessError(
                "E_FORMAT_CORRUPT", f"cannot read checkpoint payload {payload_path}: {exc}"
            ) from exc
        if sha256_bytes(payload_bytes) != checkpoint.get("payload_checksum"):
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                "checkpoint payload checksum mismatch",
                context={"payload_file": payload_file},
            )
        if payload.get("stage") != STAGE_NAMES[stage_index] or payload.get("version") != version:
            raise BusinessError(
                "E_FORMAT_CORRUPT", "checkpoint payload metadata does not match checkpoint"
            )
        chain.append(checkpoint)
        payloads.append(payload_path)
        previous_version = version
    return chain, payloads


def _cleanup_uncommitted(
    paths: RunPaths, committed_payload_paths: list[Path]
) -> list[str]:
    discarded: list[str] = []
    if not paths.run_dir.exists():
        return discarded
    committed_dirs = {path.parent for path in committed_payload_paths}
    for child in sorted(paths.run_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".stage_") and child.name.endswith(".tmp"):
            shutil.rmtree(child, ignore_errors=True)
            discarded.append(_root_relative(child, paths.root))
            continue
        if child.name.startswith("stage_") and child not in committed_dirs:
            shutil.rmtree(child, ignore_errors=True)
            discarded.append(_root_relative(child, paths.root))
    return discarded


def _load_payload(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise BusinessError("E_FORMAT_CORRUPT", "stage payload must be an object")
    return value


def _item_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = payload.get("items", [])
    return {item["batch_id"]: item for item in items if item.get("status") == "ok"}


def _failure_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["batch_id"]: item for item in payload.get("failures", [])}


def _merge_failures(
    previous: dict[str, dict[str, Any]],
    new: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = dict(previous)
    for item in new:
        merged[item["batch_id"]] = item
    return [merged[key] for key in sorted(merged)]


def _array_metrics(
    candidate: np.ndarray,
    reference: np.ndarray,
    *,
    relative_floor: float,
    previous_abs_max: float | None,
    previous_abs_rms: float | None,
    initial_injection: bool = False,
) -> dict[str, Any]:
    candidate_array = np.asarray(candidate, dtype=np.float64)
    reference_array = np.asarray(reference, dtype=np.float64)
    if candidate_array.shape != reference_array.shape:
        raise BusinessError(
            "E_QUANT_LAYER",
            "diagnostic arrays have different shapes",
            context={
                "candidate_shape": list(candidate_array.shape),
                "reference_shape": list(reference_array.shape),
            },
        )
    difference = np.abs(candidate_array - reference_array)
    abs_max = float(np.max(difference)) if difference.size else 0.0
    abs_rms = float(np.sqrt(np.mean(np.square(difference))) if difference.size else 0.0)
    abs_mean = float(np.mean(difference)) if difference.size else 0.0
    reference_rms = (
        float(np.sqrt(np.mean(np.square(reference_array))) if reference_array.size else 0.0)
    )
    relative_scale = max(reference_rms, float(relative_floor))
    rel_max = abs_max / relative_scale
    rel_rms = abs_rms / relative_scale
    if initial_injection or previous_abs_max is None or previous_abs_max == 0.0:
        amplification_max = 1.0
        amplification_rms = 1.0
    else:
        amplification_max = abs_max / previous_abs_max
        amplification_rms = (
            abs_rms / previous_abs_rms
            if previous_abs_rms is not None and previous_abs_rms > 0.0
            else 1.0
        )
    return {
        "abs_max": abs_max,
        "abs_deviation": abs_max,
        "abs_rms": abs_rms,
        "abs_mean": abs_mean,
        "rel_max": rel_max,
        "rel_deviation": rel_max,
        "rel_rms": rel_rms,
        "amplification_max": amplification_max,
        "error_amplification_ratio": amplification_max,
        "amplification_rms": amplification_rms,
        "reference_rms": reference_rms,
        "element_count": int(difference.size),
    }


def _spectrum(residual: np.ndarray) -> dict[str, float]:
    values = np.abs(np.asarray(residual, dtype=np.float64)).reshape(-1)
    if values.size == 0:
        return {"mean_abs": 0.0, "rms": 0.0, "p95": 0.0, "max_abs": 0.0, "l2": 0.0}
    squared_sum = float(np.sum(np.square(values)))
    return {
        "mean_abs": float(np.mean(values)),
        "rms": float(np.sqrt(np.mean(np.square(values)))),
        "p95": float(np.percentile(values, 95)),
        "max_abs": float(np.max(values)),
        "l2": float(math.sqrt(squared_sum)),
    }


def _spectrum_stage_metrics(
    residual: np.ndarray,
    reference_aggregate: np.ndarray,
    *,
    relative_floor: float,
    previous_abs_max: float,
    previous_abs_rms: float,
) -> tuple[dict[str, Any], dict[str, float]]:
    summary = _spectrum(residual)
    reference_scale = max(
        float(np.sqrt(np.mean(np.square(reference_aggregate)))) if reference_aggregate.size else 0.0,
        relative_floor,
    )
    abs_max = summary["max_abs"]
    abs_rms = summary["rms"]
    rel_max = abs_max / reference_scale
    rel_rms = abs_rms / reference_scale
    amplification_max = abs_max / previous_abs_max if previous_abs_max > 0 else 1.0
    amplification_rms = abs_rms / previous_abs_rms if previous_abs_rms > 0 else 1.0
    return (
        {
            "abs_max": abs_max,
            "abs_deviation": abs_max,
            "abs_rms": abs_rms,
            "abs_mean": summary["mean_abs"],
            "rel_max": rel_max,
            "rel_deviation": rel_max,
            "rel_rms": rel_rms,
            "amplification_max": amplification_max,
            "error_amplification_ratio": amplification_max,
            "amplification_rms": amplification_rms,
            "reference_rms": reference_scale,
            "element_count": int(residual.size),
        },
        summary,
    )


def _cache_artifacts_exist(
    record: dict[str, Any] | None,
    *,
    key: str,
    stage: str,
    root: Path,
    required: tuple[str, ...],
) -> bool:
    if not isinstance(record, dict) or record.get("key") != key or record.get("stage") != stage:
        return False
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, dict):
        return False
    for name in required:
        item = artifacts.get(name)
        if not isinstance(item, dict):
            return False
        relative = item.get("path")
        expected_hash = item.get("hash")
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            return False
        path = _resolve_path(root, relative)
        if not path.exists() or file_sha256(path) != expected_hash:
            return False
    return True


def _cache_record(
    *,
    key: str,
    stage: str,
    task: Task,
    batch: Batch,
    stage_dir: Path,
    root: Path,
    artifacts: dict[str, tuple[Path, str]],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "key": key,
        "stage": stage,
        "task_id": task.task_id,
        "batch_id": batch.batch_id,
        "artifacts": {
            name: {"path": _root_relative(path, root), "hash": digest}
            for name, (path, digest) in artifacts.items()
        },
        "metadata": metadata,
    }


def _commit_stage(
    paths: RunPaths,
    *,
    stage_index: int,
    version: int,
    staging_dir: Path,
    payload: dict[str, Any],
    batch_ids: list[str],
) -> dict[str, Any]:
    payload = dict(payload)
    payload["schema"] = "featurediag.stage-payload.v1"
    payload["stage"] = STAGE_NAMES[stage_index]
    payload["stage_index"] = stage_index
    payload["version"] = version
    payload["complete"] = True
    payload["completed_batches"] = batch_ids
    payload_path = staging_dir / "payload.json"
    atomic_write_json(payload_path, payload)
    payload_checksum = file_sha256(payload_path)
    final_dir = paths.run_dir / f"stage_{stage_index:02d}.v{version}"
    os.replace(staging_dir, final_dir)
    checkpoint = {
        "schema": "featurediag.checkpoint.v1",
        "stage": STAGE_NAMES[stage_index],
        "stage_index": stage_index,
        "version": version,
        "complete": True,
        "completed_batches": batch_ids,
        "failed_batches": sorted(payload.get("failed_batches", [])),
        "payload_file": _root_relative(final_dir / "payload.json", paths.root),
        "payload_checksum": payload_checksum,
        "artifact_checksums": payload.get("artifact_checksums", {}),
    }
    atomic_write_json(_checkpoint_path(paths, stage_index), checkpoint)
    return checkpoint


def _write_state(
    paths: RunPaths,
    *,
    fingerprint: str,
    snapshot: Snapshot,
    chain: list[dict[str, Any]],
) -> None:
    atomic_write_json(
        paths.state_path,
        {
            "schema": "featurediag.run-state.v1",
            "run_id": paths.run_id,
            "run_fingerprint": fingerprint,
            "snapshot": {
                "version": snapshot.snapshot_version,
                "checksum": snapshot.checksum(),
                "id": snapshot.snapshot_id,
                "logical_clock_ms": snapshot.logical_clock_ms,
            },
            "checkpoints": [
                {
                    "stage": item["stage"],
                    "stage_index": item["stage_index"],
                    "version": item["version"],
                    "payload_checksum": item["payload_checksum"],
                }
                for item in chain
            ],
            "latest_committed_stage": chain[-1]["stage"] if chain else None,
        },
    )


def _failed_entry(
    failure: dict[str, Any],
    *,
    task: Task,
    snapshot: Snapshot,
    batch: Batch,
) -> dict[str, Any]:
    return {
        "schema": "featurediag.error-ledger-entry.v1",
        "task_id": task.task_id,
        "batch_id": batch.batch_id,
        "status": "failed",
        "revision": task.revision,
        "logical_clock_ms": task.logical_clock_ms,
        "snapshot": {
            "version": snapshot.snapshot_version,
            "snapshot_id": snapshot.snapshot_id,
            "checksum": snapshot.checksum(),
        },
        "cache_keys": failure.get("cache_keys", {}),
        "stage_metrics": {},
        "error_spectrum": {},
        "dominant_level": failure.get("stage"),
        "tolerance": task.tolerance.to_dict(),
        "tolerance_exceeded": False,
        "errors": [failure["error"]],
    }


def _successful_entry(
    *,
    task: Task,
    batch: Batch,
    snapshot: Snapshot,
    stage0: dict[str, Any],
    stage1: dict[str, Any],
    stage2: dict[str, Any],
    raw: np.ndarray,
    normalized: np.ndarray,
    dequantized: np.ndarray,
    reference_aggregate: np.ndarray,
    candidate_aggregate: np.ndarray,
    spectrum_key: str,
) -> dict[str, Any]:
    normalization_reference = (
        raw - np.asarray(stage0["mean"], dtype=np.float64)
    ) / np.asarray(stage0["denominator"], dtype=np.float64)
    normalization_metrics = _array_metrics(
        normalized,
        normalization_reference,
        relative_floor=task.tolerance.relative_floor,
        previous_abs_max=None,
        previous_abs_rms=None,
        initial_injection=True,
    )
    quantization_metrics = _array_metrics(
        dequantized,
        normalized,
        relative_floor=task.tolerance.relative_floor,
        previous_abs_max=normalization_metrics["abs_max"],
        previous_abs_rms=normalization_metrics["abs_rms"],
        initial_injection=normalization_metrics["abs_max"] == 0.0,
    )
    block_metrics = _array_metrics(
        candidate_aggregate,
        reference_aggregate,
        relative_floor=task.tolerance.relative_floor,
        previous_abs_max=quantization_metrics["abs_max"],
        previous_abs_rms=quantization_metrics["abs_rms"],
    )
    residual = candidate_aggregate - reference_aggregate
    spectrum_metrics, spectrum_summary = _spectrum_stage_metrics(
        residual,
        reference_aggregate,
        relative_floor=task.tolerance.relative_floor,
        previous_abs_max=block_metrics["abs_max"],
        previous_abs_rms=block_metrics["abs_rms"],
    )
    stage_metrics = {
        "sample_normalization": normalization_metrics,
        "fixed_point": quantization_metrics,
        "block_aggregation": block_metrics,
        "error_spectrum": spectrum_metrics,
    }
    introduced = {
        "sample_normalization": 0.0,
        "fixed_point": quantization_metrics["rel_rms"],
        "block_aggregation": max(
            0.0, block_metrics["rel_rms"] - quantization_metrics["rel_rms"]
        ),
        "error_spectrum": max(0.0, spectrum_metrics["rel_rms"] - block_metrics["rel_rms"]),
    }
    dominant_level = max(introduced, key=lambda key: introduced[key])
    tolerance_exceeded = (
        quantization_metrics["abs_max"] > task.tolerance.abs_max
        or block_metrics["abs_max"] > task.tolerance.abs_max
        or quantization_metrics["rel_rms"] > task.tolerance.rel_max
        or block_metrics["rel_rms"] > task.tolerance.rel_max
    )
    errors: list[dict[str, Any]] = []
    status = "ok"
    if tolerance_exceeded:
        status = "failed"
        error = BusinessError(
            "E_TOLERANCE_EXCEEDED",
            "diagnostic deviation exceeds configured tolerance",
            stage="spectrum",
            task_id=task.task_id,
            batch_id=batch.batch_id,
            context={
                "tolerance": task.tolerance.to_dict(),
                "fixed_point_abs_max": quantization_metrics["abs_max"],
                "fixed_point_rel_rms": quantization_metrics["rel_rms"],
                "block_aggregation_abs_max": block_metrics["abs_max"],
                "block_aggregation_rel_rms": block_metrics["rel_rms"],
            },
        )
        errors.append(error.to_dict())
    cache_keys = dict(stage2["cache_keys"])
    cache_keys["spectrum"] = spectrum_key
    entry = {
        "schema": "featurediag.error-ledger-entry.v1",
        "task_id": task.task_id,
        "batch_id": batch.batch_id,
        "status": status,
        "revision": task.revision,
        "logical_clock_ms": task.logical_clock_ms,
        "snapshot": {
            "version": snapshot.snapshot_version,
            "snapshot_id": snapshot.snapshot_id,
            "checksum": snapshot.checksum(),
        },
        "cache_keys": cache_keys,
        "cache_inputs": {
            "raw_sample_hash": stage0["raw_hash"],
            "quant_scale": task.quant_scale,
            "quant_zero_point": task.quant_zero_point,
            "block_plan": task.block_plan,
            "snapshot_version": snapshot.snapshot_version,
            "task_spec_digest": snapshot.task_spec_digest(task.feature_ids),
            "upstream_hashes": {
                "normalized": stage0["normalized_hash"],
                "dequantized": stage1["dequantized_hash"],
                "aggregate": stage2["candidate_hash"],
            },
        },
        "normalization_stats": {
            "mean": stage0["mean"],
            "std": stage0["std"],
            "denominator": stage0["denominator"],
            "ddof": stage0["ddof"],
            "epsilon": stage0["epsilon"],
            "stats_hash": stage0["stats_hash"],
        },
        "input_hashes": {
            "raw": stage0["raw_hash"],
            "normalized": stage0["normalized_hash"],
            "quantized": stage1["quantized_hash"],
            "dequantized": stage1["dequantized_hash"],
            "aggregate_reference": stage2["reference_hash"],
            "aggregate_fixed_point": stage2["candidate_hash"],
        },
        "stage_metrics": stage_metrics,
        "error_spectrum": spectrum_summary,
        "normalization_ledger": {
            "hash": stage0["stats_hash"],
            "shared_float64_and_fixed_path": True,
        },
        "dominant_level": dominant_level,
        "introduced_error_scores": introduced,
        "tolerance": task.tolerance.to_dict(),
        "tolerance_exceeded": tolerance_exceeded,
        "errors": errors,
    }
    return entry


def _verify_artifacts_in_item(item: dict[str, Any], root: Path) -> None:
    artifacts = item.get("artifacts", {})
    for name, record in artifacts.items():
        if not isinstance(record, dict):
            raise BusinessError("E_FORMAT_CORRUPT", f"artifact record {name} is invalid")
        path = _resolve_path(root, record["path"])
        if not path.exists():
            raise BusinessError("E_FORMAT_CORRUPT", f"artifact file is missing: {path}")
        if file_sha256(path) != record["hash"]:
            raise BusinessError(
                "E_FORMAT_CORRUPT", f"artifact checksum mismatch: {path}"
            )


def run_pipeline(
    *,
    root: Path,
    run_id: str,
    tasks: dict[str, Task],
    batches: list[Batch],
    snapshot: Snapshot,
    config: RunConfig,
    fresh: bool = False,
    crash_after_stage: int | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    paths = RunPaths(root, run_id)
    batch_ids = _all_batch_ids(batches)
    expected_set = set(batch_ids)
    if fresh and paths.run_dir.exists():
        shutil.rmtree(paths.run_dir)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    paths.checkpoint_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = run_fingerprint(tasks, batches, snapshot, config)
    cache_index = CacheIndex(paths.cache_index_path)
    index_data = cache_index.load() if use_cache else {}
    chain, payload_paths = _load_checkpoint_chain(paths, batch_ids)
    discarded = _cleanup_uncommitted(paths, payload_paths)
    recovered_from = [item["stage"] for item in chain]
    cache_hits: list[dict[str, str]] = []
    newly_published: list[dict[str, Any]] = []
    payload_by_index: dict[int, dict[str, Any]] = {}
    for stage_index, payload_path in enumerate(payload_paths):
        payload_by_index[stage_index] = _load_payload(payload_path)

    if chain:
        state_raw = read_json(paths.state_path) if paths.state_path.exists() else {}
        if state_raw and state_raw.get("run_fingerprint") != fingerprint:
            raise BusinessError(
                "E_FORMAT_CORRUPT",
                "existing run state belongs to a different input/snapshot fingerprint",
                context={
                    "run_id": run_id,
                    "existing_fingerprint": state_raw.get("run_fingerprint"),
                    "incoming_fingerprint": fingerprint,
                },
            )
    previous_version = max((item["version"] for item in chain), default=0)

    for stage_index in range(len(chain), len(STAGE_NAMES)):
        version = previous_version + 1
        final_dir = paths.run_dir / f"stage_{stage_index:02d}.v{version}"
        staging = paths.run_dir / f".stage_{stage_index:02d}.v{version}.tmp"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        cache_records: list[dict[str, Any]] = []
        try:
            if stage_index == 0:
                items: list[dict[str, Any]] = []
                failures: list[dict[str, Any]] = []
                artifact_checksums: dict[str, str] = {}
                for batch in batches:
                    task = tasks[batch.task_id]
                    try:
                        raw = generate_samples(task, batch)
                        raw_path = staging / "data" / f"{batch.batch_id}.raw.npy"
                        raw_path.parent.mkdir(parents=True, exist_ok=True)
                        atomic_write_array(raw_path, raw)
                        raw_hash = array_hash(raw)
                        key = normalization_cache_key(
                            task, batch, raw_sample_hash=raw_hash, snapshot=snapshot
                        )
                        cached = index_data.get(key)
                        reused_normalization = False
                        if _cache_artifacts_exist(
                            cached,
                            key=key,
                            stage="normalize",
                            root=root,
                            required=("normalized",),
                        ):
                            cached_normalized = _resolve_path(
                                root, cached["artifacts"]["normalized"]["path"]
                            )
                            normalized_path = cached_normalized
                            normalized_hash = cached["artifacts"]["normalized"]["hash"]
                            stats = dict(cached["metadata"]["normalization_stats"])
                            stats_hash = str(cached["metadata"]["stats_hash"])
                            mean = stats["mean"]
                            std = stats["std"]
                            denominator = stats["denominator"]
                            cache_hits.append(
                                {
                                    "stage": "normalize",
                                    "task_id": task.task_id,
                                    "batch_id": batch.batch_id,
                                    "key": key,
                                }
                            )
                            reused_normalization = True
                        else:
                            normalized_result = normalize_zscore(
                                raw,
                                ddof=task.normalization_ddof,
                                epsilon=task.normalization_epsilon,
                                coordinate_dimension=task.feature_dim,
                            )
                            stats = normalized_result.stats_dict(task, batch)
                            stats_hash = normalized_result.stats_hash
                            mean = stats["mean"]
                            std = stats["std"]
                            denominator = stats["denominator"]
                            normalized_path = staging / "data" / f"{batch.batch_id}.normalized.npy"
                            atomic_write_array(normalized_path, normalized_result.normalized)
                            normalized_hash = normalized_result.normalized_hash
                        final_raw_path = final_dir / "data" / f"{batch.batch_id}.raw.npy"
                        final_normalized_path = (
                            normalized_path
                            if reused_normalization
                            else final_dir / "data" / f"{batch.batch_id}.normalized.npy"
                        )
                        item = {
                            "status": "ok",
                            "batch_id": batch.batch_id,
                            "task_id": batch.task_id,
                            "sample_count": batch.sample_count,
                            "feature_dim": task.feature_dim,
                            "feature_seed": batch.feature_seed,
                            "raw_hash": raw_hash,
                            "normalized_hash": normalized_hash,
                            "stats_hash": stats_hash,
                            "mean": mean,
                            "std": std,
                            "denominator": denominator,
                            "ddof": task.normalization_ddof,
                            "epsilon": task.normalization_epsilon,
                            "cache_keys": {"normalize": key},
                            "artifacts": {
                                "raw": {
                                    "path": _root_relative(final_raw_path, root),
                                    "hash": raw_hash,
                                },
                                "normalized": {
                                    "path": _root_relative(final_normalized_path, root),
                                    "hash": normalized_hash,
                                },
                            },
                        }
                        if not reused_normalization:
                            cache_records.append(
                                _cache_record(
                                    key=key,
                                    stage="normalize",
                                    task=task,
                                    batch=batch,
                                    stage_dir=final_dir,
                                    root=root,
                                    artifacts={
                                        "raw": (final_raw_path, raw_hash),
                                        "normalized": (final_normalized_path, normalized_hash),
                                    },
                                    metadata={
                                        "normalization_stats": stats,
                                        "stats_hash": stats_hash,
                                    },
                                )
                            )
                        for name, record in item["artifacts"].items():
                            artifact_checksums[f"{batch.batch_id}:{name}"] = record["hash"]
                        items.append(item)
                    except BusinessError as exc:
                        failures.append(
                            {
                                "batch_id": batch.batch_id,
                                "task_id": batch.task_id,
                                "stage": "normalize",
                                "error": exc.to_dict(),
                                "cache_keys": {},
                            }
                        )
                payload = {
                    "stage": "normalize",
                    "items": sorted(items, key=lambda item: item["batch_id"]),
                    "failures": sorted(failures, key=lambda item: item["batch_id"]),
                    "failed_batches": sorted({item["batch_id"] for item in failures}),
                    "artifact_checksums": artifact_checksums,
                }
            elif stage_index == 1:
                stage0_payload = payload_by_index[0]
                stage0_items = _item_map(stage0_payload)
                prior_failures = _failure_map(stage0_payload)
                new_failures: list[dict[str, Any]] = []
                items = []
                artifact_checksums = {}
                for batch in batches:
                    task = tasks[batch.task_id]
                    stage0 = stage0_items.get(batch.batch_id)
                    if stage0 is None:
                        continue
                    try:
                        normalized_path = _resolve_path(
                            root, stage0["artifacts"]["normalized"]["path"]
                        )
                        normalized = read_array(normalized_path)
                        normalized_hash = array_hash(normalized)
                        if normalized_hash != stage0["normalized_hash"]:
                            raise BusinessError(
                                "E_FORMAT_CORRUPT",
                                "normalized artifact changed after checkpoint commit",
                                stage="quantize",
                                task_id=task.task_id,
                                batch_id=batch.batch_id,
                            )
                        key = quantization_cache_key(
                            task,
                            batch,
                            raw_sample_hash=stage0["raw_hash"],
                            upstream_normalized_hash=normalized_hash,
                            snapshot=snapshot,
                        )
                        cached = index_data.get(key)
                        reused_quantization = False
                        if _cache_artifacts_exist(
                            cached,
                            key=key,
                            stage="quantize",
                            root=root,
                            required=("quantized", "dequantized"),
                        ):
                            quantized_path = _resolve_path(
                                root, cached["artifacts"]["quantized"]["path"]
                            )
                            dequantized_path = _resolve_path(
                                root, cached["artifacts"]["dequantized"]["path"]
                            )
                            quantized_hash = cached["artifacts"]["quantized"]["hash"]
                            dequantized_hash = cached["artifacts"]["dequantized"]["hash"]
                            cache_hits.append(
                                {
                                    "stage": "quantize",
                                    "task_id": task.task_id,
                                    "batch_id": batch.batch_id,
                                    "key": key,
                                }
                            )
                            reused_quantization = True
                        else:
                            quant_result = quantize(
                                normalized,
                                scale=task.quant_scale,
                                zero_point=task.quant_zero_point,
                                rounding_mode=task.rounding_mode,
                                expected_shape=(batch.sample_count, task.feature_dim),
                                task_id=task.task_id,
                                batch_id=batch.batch_id,
                            )
                            quantized_path = staging / "data" / f"{batch.batch_id}.quantized.npy"
                            dequantized_path = staging / "data" / f"{batch.batch_id}.dequantized.npy"
                            quantized_path.parent.mkdir(parents=True, exist_ok=True)
                            atomic_write_array(quantized_path, quant_result.quantized)
                            atomic_write_array(dequantized_path, quant_result.dequantized)
                            quantized_hash = quant_result.quantized_hash
                            dequantized_hash = quant_result.dequantized_hash
                        final_quantized_path = (
                            quantized_path
                            if reused_quantization
                            else final_dir / "data" / f"{batch.batch_id}.quantized.npy"
                        )
                        final_dequantized_path = (
                            dequantized_path
                            if reused_quantization
                            else final_dir / "data" / f"{batch.batch_id}.dequantized.npy"
                        )
                        item = {
                            "status": "ok",
                            "batch_id": batch.batch_id,
                            "task_id": batch.task_id,
                            "raw_hash": stage0["raw_hash"],
                            "normalized_hash": normalized_hash,
                            "stats_hash": stage0["stats_hash"],
                            "quantized_hash": quantized_hash,
                            "dequantized_hash": dequantized_hash,
                            "cache_keys": {
                                **stage0["cache_keys"],
                                "quantize": key,
                            },
                            "artifacts": {
                                "quantized": {
                                    "path": _root_relative(final_quantized_path, root),
                                    "hash": quantized_hash,
                                },
                                "dequantized": {
                                    "path": _root_relative(final_dequantized_path, root),
                                    "hash": dequantized_hash,
                                },
                            },
                        }
                        if not reused_quantization:
                            cache_records.append(
                                _cache_record(
                                    key=key,
                                    stage="quantize",
                                    task=task,
                                    batch=batch,
                                    stage_dir=final_dir,
                                    root=root,
                                    artifacts={
                                        "quantized": (final_quantized_path, quantized_hash),
                                        "dequantized": (final_dequantized_path, dequantized_hash),
                                    },
                                    metadata={
                                        "normalization_stats_hash": stage0["stats_hash"],
                                        "shared_normalization_stats": True,
                                    },
                                )
                            )
                        for name, record in item["artifacts"].items():
                            artifact_checksums[f"{batch.batch_id}:{name}"] = record["hash"]
                        items.append(item)
                    except BusinessError as exc:
                        new_failures.append(
                            {
                                "batch_id": batch.batch_id,
                                "task_id": batch.task_id,
                                "stage": "quantize",
                                "error": exc.to_dict(),
                                "cache_keys": {
                                    **stage0["cache_keys"],
                                    "quantize": quantization_cache_key(
                                        task,
                                        batch,
                                        raw_sample_hash=stage0["raw_hash"],
                                        upstream_normalized_hash=stage0["normalized_hash"],
                                        snapshot=snapshot,
                                    ),
                                },
                            }
                        )
                failures = _merge_failures(prior_failures, new_failures)
                payload = {
                    "stage": "quantize",
                    "items": sorted(items, key=lambda item: item["batch_id"]),
                    "failures": failures,
                    "failed_batches": sorted(item["batch_id"] for item in failures),
                    "artifact_checksums": artifact_checksums,
                }
            elif stage_index == 2:
                stage1_payload = payload_by_index[1]
                stage1_items = _item_map(stage1_payload)
                prior_failures = _failure_map(stage1_payload)
                new_failures = []
                items = []
                artifact_checksums = {}
                for batch in batches:
                    task = tasks[batch.task_id]
                    stage1 = stage1_items.get(batch.batch_id)
                    if stage1 is None:
                        continue
                    try:
                        stage0_item = next(
                            item
                            for item in payload_by_index[0]["items"]
                            if item["batch_id"] == batch.batch_id
                        )
                        normalized_path = _resolve_path(
                            root, stage0_item["artifacts"]["normalized"]["path"]
                        )
                        dequantized_path = _resolve_path(
                            root, stage1["artifacts"]["dequantized"]["path"]
                        )
                        normalized = read_array(normalized_path)
                        dequantized = read_array(dequantized_path)
                        if array_hash(normalized) != stage1["normalized_hash"]:
                            raise BusinessError(
                                "E_FORMAT_CORRUPT",
                                "normalized artifact checksum changed before aggregation",
                                stage="aggregate",
                                task_id=task.task_id,
                                batch_id=batch.batch_id,
                            )
                        if array_hash(dequantized) != stage1["dequantized_hash"]:
                            raise BusinessError(
                                "E_FORMAT_CORRUPT",
                                "dequantized artifact checksum changed before aggregation",
                                stage="aggregate",
                                task_id=task.task_id,
                                batch_id=batch.batch_id,
                            )
                        key = aggregation_cache_key(
                            task,
                            batch,
                            raw_sample_hash=stage1["raw_hash"],
                            upstream_dequantized_hash=stage1["dequantized_hash"],
                            snapshot=snapshot,
                        )
                        cached = index_data.get(key)
                        reused_aggregation = False
                        if _cache_artifacts_exist(
                            cached,
                            key=key,
                            stage="aggregate",
                            root=root,
                            required=("reference", "candidate"),
                        ):
                            reference_path = _resolve_path(
                                root, cached["artifacts"]["reference"]["path"]
                            )
                            candidate_path = _resolve_path(
                                root, cached["artifacts"]["candidate"]["path"]
                            )
                            reference_hash = cached["artifacts"]["reference"]["hash"]
                            candidate_hash = cached["artifacts"]["candidate"]["hash"]
                            block_metadata = list(cached["metadata"]["block_metadata"])
                            pair_count = int(cached["metadata"]["pair_count"])
                            equality = dict(cached["metadata"]["equality"])
                            if equality.get("equal") is not True:
                                raise BusinessError(
                                    "E_BLOCK_MISMATCH",
                                    "cached aggregation record did not pass block equality",
                                    stage="aggregate",
                                    task_id=task.task_id,
                                    batch_id=batch.batch_id,
                                )
                            cache_hits.append(
                                {
                                    "stage": "aggregate",
                                    "task_id": task.task_id,
                                    "batch_id": batch.batch_id,
                                    "key": key,
                                }
                            )
                            reused_aggregation = True
                        else:
                            reference = aggregate_monolithic(normalized, task)
                            candidate, block_metadata = aggregate_in_blocks(dequantized, task)
                            candidate_monolithic = aggregate_monolithic(dequantized, task)
                            equality = compare_block_results(
                                candidate, candidate_monolithic, task, batch_id=batch.batch_id
                            )
                            reference_blocks, _ = aggregate_in_blocks(normalized, task)
                            reference_monolithic = aggregate_monolithic(normalized, task)
                            compare_block_results(
                                reference_blocks,
                                reference_monolithic,
                                task,
                                batch_id=batch.batch_id,
                            )
                            reference_path = staging / "data" / f"{batch.batch_id}.reference.npy"
                            candidate_path = staging / "data" / f"{batch.batch_id}.candidate.npy"
                            reference_path.parent.mkdir(parents=True, exist_ok=True)
                            atomic_write_array(reference_path, reference)
                            atomic_write_array(candidate_path, candidate)
                            reference_hash = array_hash(reference)
                            candidate_hash = array_hash(candidate)
                            pair_count = len(pair_specs(task))
                        final_reference_path = (
                            reference_path
                            if reused_aggregation
                            else final_dir / "data" / f"{batch.batch_id}.reference.npy"
                        )
                        final_candidate_path = (
                            candidate_path
                            if reused_aggregation
                            else final_dir / "data" / f"{batch.batch_id}.candidate.npy"
                        )
                        item = {
                            "status": "ok",
                            "batch_id": batch.batch_id,
                            "task_id": batch.task_id,
                            "raw_hash": stage1["raw_hash"],
                            "normalized_hash": stage1["normalized_hash"],
                            "dequantized_hash": stage1["dequantized_hash"],
                            "reference_hash": reference_hash,
                            "candidate_hash": candidate_hash,
                            "pair_count": pair_count,
                            "block_metadata": block_metadata,
                            "equality": equality,
                            "cache_keys": {
                                **stage1["cache_keys"],
                                "aggregate": key,
                            },
                            "artifacts": {
                                "reference": {
                                    "path": _root_relative(final_reference_path, root),
                                    "hash": reference_hash,
                                },
                                "candidate": {
                                    "path": _root_relative(final_candidate_path, root),
                                    "hash": candidate_hash,
                                },
                            },
                        }
                        if not reused_aggregation:
                            cache_records.append(
                                _cache_record(
                                    key=key,
                                    stage="aggregate",
                                    task=task,
                                    batch=batch,
                                    stage_dir=final_dir,
                                    root=root,
                                    artifacts={
                                        "reference": (final_reference_path, reference_hash),
                                        "candidate": (final_candidate_path, candidate_hash),
                                    },
                                    metadata={
                                        "block_metadata": block_metadata,
                                        "pair_count": pair_count,
                                        "equality": equality,
                                    },
                                )
                            )
                        for name, record in item["artifacts"].items():
                            artifact_checksums[f"{batch.batch_id}:{name}"] = record["hash"]
                        items.append(item)
                    except BusinessError as exc:
                        new_failures.append(
                            {
                                "batch_id": batch.batch_id,
                                "task_id": batch.task_id,
                                "stage": "aggregate",
                                "error": exc.to_dict(),
                                "cache_keys": dict(stage1["cache_keys"]),
                            }
                        )
                failures = _merge_failures(prior_failures, new_failures)
                payload = {
                    "stage": "aggregate",
                    "items": sorted(items, key=lambda item: item["batch_id"]),
                    "failures": failures,
                    "failed_batches": sorted(item["batch_id"] for item in failures),
                    "artifact_checksums": artifact_checksums,
                }
            else:
                stage2_payload = payload_by_index[2]
                stage0_payload = payload_by_index[0]
                stage1_payload = payload_by_index[1]
                stage0_items = _item_map(stage0_payload)
                stage1_items = _item_map(stage1_payload)
                stage2_items = _item_map(stage2_payload)
                failures = _failure_map(stage2_payload)
                ledger_entries: list[dict[str, Any]] = []
                item_summaries: list[dict[str, Any]] = []
                for batch in batches:
                    task = tasks[batch.task_id]
                    if batch.batch_id in failures:
                        ledger_entries.append(
                            _failed_entry(
                                failures[batch.batch_id],
                                task=task,
                                snapshot=snapshot,
                                batch=batch,
                            )
                        )
                        item_summaries.append(
                            {
                                "batch_id": batch.batch_id,
                                "task_id": batch.task_id,
                                "status": "failed",
                                "stage": failures[batch.batch_id]["stage"],
                            }
                        )
                        continue
                    stage0 = stage0_items[batch.batch_id]
                    stage1 = stage1_items[batch.batch_id]
                    stage2 = stage2_items[batch.batch_id]
                    try:
                        spectrum_key = spectrum_cache_key(
                            task,
                            batch,
                            raw_sample_hash=stage2["raw_hash"],
                            upstream_aggregate_hash=stage2["candidate_hash"],
                            snapshot=snapshot,
                        )
                        cached_spectrum = index_data.get(spectrum_key)
                        if _cache_artifacts_exist(
                            cached_spectrum,
                            key=spectrum_key,
                            stage="spectrum",
                            root=root,
                            required=("ledger_entry",),
                        ):
                            cached_entry_path = _resolve_path(
                                root, cached_spectrum["artifacts"]["ledger_entry"]["path"]
                            )
                            cached_entry = read_json(cached_entry_path)
                            if (
                                cached_entry.get("task_id") != task.task_id
                                or cached_entry.get("batch_id") != batch.batch_id
                                or cached_entry.get("schema") != "featurediag.error-ledger-entry.v1"
                            ):
                                raise BusinessError(
                                    "E_FORMAT_CORRUPT",
                                    "cached spectrum ledger entry identity mismatch",
                                    stage="spectrum",
                                    task_id=task.task_id,
                                    batch_id=batch.batch_id,
                                )
                            ledger_entries.append(cached_entry)
                            item_summaries.append(
                                {
                                    "batch_id": batch.batch_id,
                                    "task_id": batch.task_id,
                                    "status": cached_entry["status"],
                                    "dominant_level": cached_entry.get("dominant_level"),
                                    "tolerance_exceeded": cached_entry.get("tolerance_exceeded", False),
                                    "reused": True,
                                }
                            )
                            cache_hits.append(
                                {
                                    "stage": "spectrum",
                                    "task_id": task.task_id,
                                    "batch_id": batch.batch_id,
                                    "key": spectrum_key,
                                }
                            )
                            continue
                        raw = read_array(_resolve_path(root, stage0["artifacts"]["raw"]["path"]))
                        normalized = read_array(
                            _resolve_path(root, stage0["artifacts"]["normalized"]["path"])
                        )
                        dequantized = read_array(
                            _resolve_path(root, stage1["artifacts"]["dequantized"]["path"])
                        )
                        reference_aggregate = read_array(
                            _resolve_path(root, stage2["artifacts"]["reference"]["path"])
                        )
                        candidate_aggregate = read_array(
                            _resolve_path(root, stage2["artifacts"]["candidate"]["path"])
                        )
                        if array_hash(raw) != stage0["raw_hash"]:
                            raise BusinessError(
                                "E_FORMAT_CORRUPT", "raw artifact checksum mismatch", stage="spectrum"
                            )
                        if array_hash(normalized) != stage0["normalized_hash"]:
                            raise BusinessError(
                                "E_FORMAT_CORRUPT", "normalized artifact checksum mismatch", stage="spectrum"
                            )
                        if array_hash(dequantized) != stage1["dequantized_hash"]:
                            raise BusinessError(
                                "E_FORMAT_CORRUPT", "dequantized artifact checksum mismatch", stage="spectrum"
                            )
                        if array_hash(reference_aggregate) != stage2["reference_hash"]:
                            raise BusinessError(
                                "E_FORMAT_CORRUPT", "reference aggregation hash mismatch", stage="spectrum"
                            )
                        if array_hash(candidate_aggregate) != stage2["candidate_hash"]:
                            raise BusinessError(
                                "E_FORMAT_CORRUPT", "candidate aggregation hash mismatch", stage="spectrum"
                            )
                        entry = _successful_entry(
                            task=task,
                            batch=batch,
                            snapshot=snapshot,
                            stage0=stage0,
                            stage1=stage1,
                            stage2=stage2,
                            raw=raw,
                            normalized=normalized,
                            dequantized=dequantized,
                            reference_aggregate=reference_aggregate,
                            candidate_aggregate=candidate_aggregate,
                            spectrum_key=spectrum_key,
                        )
                        staging_entry_path = (
                            staging / "ledger_entries" / f"{batch.batch_id}.json"
                        )
                        staging_entry_path.parent.mkdir(parents=True, exist_ok=True)
                        atomic_write_json(staging_entry_path, entry)
                        final_entry_path = (
                            final_dir / "ledger_entries" / f"{batch.batch_id}.json"
                        )
                        cache_records.append(
                            _cache_record(
                                key=spectrum_key,
                                stage="spectrum",
                                task=task,
                                batch=batch,
                                stage_dir=final_dir,
                                root=root,
                                artifacts={
                                    "ledger_entry": (
                                        final_entry_path,
                                        file_sha256(staging_entry_path),
                                    )
                                },
                                metadata={
                                    "status": entry["status"],
                                    "tolerance_exceeded": entry["tolerance_exceeded"],
                                },
                            )
                        )
                        ledger_entries.append(entry)
                        item_summaries.append(
                            {
                                "batch_id": batch.batch_id,
                                "task_id": batch.task_id,
                                "status": entry["status"],
                                "dominant_level": entry["dominant_level"],
                                "tolerance_exceeded": entry["tolerance_exceeded"],
                            }
                        )
                    except BusinessError as exc:
                        failures[batch.batch_id] = {
                            "batch_id": batch.batch_id,
                            "task_id": batch.task_id,
                            "stage": "spectrum",
                            "error": exc.to_dict(),
                            "cache_keys": dict(stage2["cache_keys"]),
                        }
                        ledger_entries.append(
                            _failed_entry(
                                failures[batch.batch_id],
                                task=task,
                                snapshot=snapshot,
                                batch=batch,
                            )
                        )
                        item_summaries.append(
                            {
                                "batch_id": batch.batch_id,
                                "task_id": batch.task_id,
                                "status": "failed",
                                "stage": "spectrum",
                            }
                        )
                ledger_entries = sorted(ledger_entries, key=lambda item: item["batch_id"])
                ledger_rows = "".join(canonical_json(entry) + "\n" for entry in ledger_entries)
                ledger_checksum = sha256_bytes(ledger_rows.encode("utf-8"))
                ledger_file = staging / "error_ledger.jsonl"
                ledger_file.write_text(ledger_rows, encoding="utf-8")
                payload = {
                    "stage": "spectrum",
                    "items": item_summaries,
                    "failures": [failures[key] for key in sorted(failures)],
                    "failed_batches": sorted(failures),
                    "ledger_entries": ledger_entries,
                    "ledger_checksum": ledger_checksum,
                    "ledger_file": _root_relative(final_dir / "error_ledger.jsonl", root),
                    "artifact_checksums": {"ledger": ledger_checksum},
                }
            checkpoint = _commit_stage(
                paths,
                stage_index=stage_index,
                version=version,
                staging_dir=staging,
                payload=payload,
                batch_ids=batch_ids,
            )
            chain.append(checkpoint)
            payload_by_index[stage_index] = payload
            previous_version = version
            _write_state(paths, fingerprint=fingerprint, snapshot=snapshot, chain=chain)
            if cache_records and use_cache:
                cache_index.publish(cache_records)
                newly_published.extend(cache_records)
            if crash_after_stage == stage_index:
                raise SimulatedCrash(
                    f"simulated crash after committed stage {STAGE_NAMES[stage_index]}"
                )
        except SimulatedCrash:
            raise
        except BusinessError:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    if len(chain) < len(STAGE_NAMES):
        raise BusinessError("E_INTERNAL", "pipeline ended before all stages committed")
    spectrum_payload = payload_by_index[3]
    ledger_entries = spectrum_payload["ledger_entries"]
    ledger_rows = "".join(canonical_json(entry) + "\n" for entry in ledger_entries)
    atomic_write_jsonl(paths.ledger_path, ledger_entries)
    ledger_checksum = sha256_bytes(ledger_rows.encode("utf-8"))
    if ledger_checksum != spectrum_payload["ledger_checksum"]:
        raise BusinessError("E_FORMAT_CORRUPT", "materialized ledger checksum mismatch")
    errors: list[dict[str, Any]] = []
    tolerance_errors: list[dict[str, Any]] = []
    for entry in ledger_entries:
        for error in entry.get("errors", []):
            if error.get("code") == "E_TOLERANCE_EXCEEDED":
                tolerance_errors.append(error)
            else:
                errors.append(error)
    errors.extend(tolerance_errors)
    exit_code = exit_code_for(errors)
    first_error_code = errors[0]["code"] if errors else "OK"
    cache_hit_pairs = {
        (item["stage"], item["batch_id"]) for item in cache_hits
    }
    recompute_artifacts: list[dict[str, Any]] = []
    for stage_index, stage_name in enumerate(STAGE_NAMES[:3]):
        for item in payload_by_index[stage_index].get("items", []):
            pair = (stage_name, item["batch_id"])
            key = item.get("cache_keys", {}).get(stage_name)
            if key and pair not in cache_hit_pairs:
                recompute_artifacts.append(
                    {
                        "stage": stage_name,
                        "task_id": item["task_id"],
                        "batch_id": item["batch_id"],
                        "key": key,
                    }
                )
    for entry in ledger_entries:
        key = entry.get("cache_keys", {}).get("spectrum")
        pair = ("spectrum", entry["batch_id"])
        if key and pair not in cache_hit_pairs:
            recompute_artifacts.append(
                {
                    "stage": "spectrum",
                    "task_id": entry["task_id"],
                    "batch_id": entry["batch_id"],
                    "key": key,
                }
            )
    recompute_artifacts = sorted(
        recompute_artifacts,
        key=lambda item: (item["batch_id"], item["stage"]),
    )
    manifest = {
        "schema": "featurediag.run-manifest.v1",
        "run_id": run_id,
        "run_fingerprint": fingerprint,
        "snapshot": {
            "version": snapshot.snapshot_version,
            "snapshot_id": snapshot.snapshot_id,
            "checksum": snapshot.checksum(),
            "logical_clock_ms": snapshot.logical_clock_ms,
        },
        "batch_ids": batch_ids,
        "checkpoints": [
            {
                "stage": item["stage"],
                "version": item["version"],
                "payload_checksum": item["payload_checksum"],
                "payload_file": item["payload_file"],
            }
            for item in chain
        ],
        "ledger_checksum": ledger_checksum,
        "ledger_entry_count": len(ledger_entries),
        "failed_entry_count": sum(1 for entry in ledger_entries if entry["status"] == "failed"),
        "cache_hits": sorted(
            cache_hits,
            key=lambda item: (item["batch_id"], item["stage"]),
        ),
        "cache_published_count": len(newly_published),
        "cache_recomputed": recompute_artifacts,
        "recovered_from_committed_stages": recovered_from,
        "discarded_uncommitted_paths": discarded,
        "status": "ok" if not errors else "failed",
        "first_error_code": first_error_code,
    }
    atomic_write_json(paths.manifest_path, manifest)
    if recovered_from or discarded:
        atomic_write_json(
            paths.recovery_report_path,
            {
                "schema": "featurediag.recovery-report.v1",
                "run_id": run_id,
                "recovered_from_committed_stages": recovered_from,
                "discarded_uncommitted_paths": discarded,
                "resumed_stage": STAGE_NAMES[len(chain) - 1] if chain else None,
                "ledger_checksum": ledger_checksum,
                "checkpoint_versions": {
                    item["stage"]: item["version"] for item in chain
                },
            },
        )
    return {
        "code": "OK" if not errors else first_error_code,
        "exit_code": exit_code,
        "run_id": run_id,
        "run_dir": str(paths.run_dir),
        "ledger_path": str(paths.ledger_path),
        "ledger_checksum": ledger_checksum,
        "ledger_entry_count": len(ledger_entries),
        "failed_entry_count": sum(1 for entry in ledger_entries if entry["status"] == "failed"),
        "checkpoints": manifest["checkpoints"],
        "cache_hits": manifest["cache_hits"],
        "cache_recomputed": recompute_artifacts,
        "recovered_from_committed_stages": recovered_from,
        "discarded_uncommitted_paths": discarded,
        "errors": errors,
        "entries": ledger_entries,
    }


def load_checkpoint_chain_for_run(
    root: Path, run_id: str, batch_ids: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    paths = RunPaths(root, run_id)
    chain, payload_paths = _load_checkpoint_chain(paths, batch_ids)
    return chain, [_load_payload(path) for path in payload_paths]


def extract_baseline_items(root: Path, run_id: str, batch_ids: list[str]) -> list[Any]:
    from .cache import BaselineItem

    chain, payloads = load_checkpoint_chain_for_run(root, run_id, batch_ids)
    if len(payloads) < 4:
        raise BusinessError(
            "E_FORMAT_CORRUPT", "baseline run does not contain all committed stages"
        )
    stage0 = _item_map(payloads[0])
    stage1 = _item_map(payloads[1])
    stage2 = _item_map(payloads[2])
    ledger_by_batch = {
        entry["batch_id"]: entry for entry in payloads[3].get("ledger_entries", [])
    }
    result: list[BaselineItem] = []
    for batch_id in sorted(stage2):
        item0 = stage0[batch_id]
        item1 = stage1[batch_id]
        item2 = stage2[batch_id]
        result.append(
            BaselineItem(
                task_id=item2["task_id"],
                batch_id=batch_id,
                raw_sample_hash=item0["raw_hash"],
                normalized_hash=item0["normalized_hash"],
                dequantized_hash=item1["dequantized_hash"],
                aggregate_hash=item2["candidate_hash"],
                cache_keys={
                    **item2["cache_keys"],
                    **ledger_by_batch.get(batch_id, {}).get("cache_keys", {}),
                },
                artifact_paths={
                    "normalize": str(
                        _resolve_path(root, item0["artifacts"]["normalized"]["path"])
                    ),
                    "quantize": str(
                        _resolve_path(root, item1["artifacts"]["dequantized"]["path"])
                    ),
                    "aggregate": str(
                        _resolve_path(root, item2["artifacts"]["candidate"]["path"])
                    ),
                    "spectrum": str(
                        _resolve_path(root, chain[3]["payload_file"])
                    ),
                },
            )
        )
    return result


def verify_run(root: Path, run_id: str, tasks: dict[str, Task], batches: list[Batch]) -> dict[str, Any]:
    paths = RunPaths(root, run_id)
    batch_ids = _all_batch_ids(batches)
    chain, payload_paths = _load_checkpoint_chain(paths, batch_ids)
    if len(chain) != len(STAGE_NAMES):
        raise BusinessError(
            "E_FORMAT_CORRUPT",
            "run does not have all four committed stage checkpoints",
            context={"committed_stages": [item["stage"] for item in chain]},
        )
    payloads = [_load_payload(path) for path in payload_paths]
    checked_arrays = 0
    for payload in payloads[:3]:
        for item in payload.get("items", []):
            _verify_artifacts_in_item(item, root)
            checked_arrays += len(item.get("artifacts", {}))
    spectrum_payload = payloads[3]
    ledger_entries = spectrum_payload["ledger_entries"]
    rows = "".join(canonical_json(entry) + "\n" for entry in ledger_entries)
    if sha256_bytes(rows.encode("utf-8")) != spectrum_payload["ledger_checksum"]:
        raise BusinessError("E_FORMAT_CORRUPT", "ledger payload checksum mismatch")
    if paths.ledger_path.exists():
        if file_sha256(paths.ledger_path) != spectrum_payload["ledger_checksum"]:
            raise BusinessError("E_FORMAT_CORRUPT", "materialized ledger differs from committed ledger")
    versions = [item["version"] for item in chain]
    if any(right <= left for left, right in zip(versions, versions[1:])):
        raise BusinessError("E_FORMAT_CORRUPT", "checkpoint versions are not monotonic")
    return {
        "code": "OK",
        "run_id": run_id,
        "checkpoint_versions": {item["stage"]: item["version"] for item in chain},
        "completed_batches": sorted(batch_ids),
        "ledger_entries_verified": len(ledger_entries),
        "ledger_checksum": spectrum_payload["ledger_checksum"],
        "artifact_files_verified": checked_arrays,
        "monotonic_versions": True,
        "ledger_row_checksum_matches": True,
    }
