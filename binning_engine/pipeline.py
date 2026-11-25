"""End-to-end deterministic binning/encoding pipeline."""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Union

from .binning import compute_boundaries
from .cache import ContentCache, make_cache_key
from .checkpoint import CheckpointStore
from .codes import EngineError, ErrorCode
from .config import effective_config_with_resources, load_config, normalize_config
from .encoding import build_encoding_context, build_feature_spec
from .hashing import canonical_hash, sha256_file, write_atomic_json
from .resource import plan_resources, read_runtime_memory
from .scheduler import BatchScheduler, build_batch_plan
from .snapshot import SnapshotStore
from .stats import build_statistics, cross_check_cache_and_artifacts
from .validation import load_and_validate, validate_samples


EngineResult = Dict[str, Any]


def _input_identity(
    samples_path: Optional[Union[str, Path]],
    records: Optional[Sequence[Mapping[str, Any]]],
) -> Dict[str, Any]:
    if samples_path is not None:
        path = Path(samples_path)
        return {
            "input_kind": "jsonl",
            "input_sha256": sha256_file(path),
        }
    if records is not None:
        try:
            records_hash = canonical_hash({"records": list(records)})
        except (ValueError, TypeError):
            records_hash = "unhashed-invalid-records"
        return {"input_kind": "records", "records_hash": records_hash}
    raise EngineError(
        ErrorCode.E_INVALID_INPUT, "either samples_path or records is required"
    )


def _ordered_encoded_rows(
    rows: Sequence[Mapping[str, Any]], batch_results: Sequence[Mapping[str, Any]]
) -> tuple:
    encoded_by_id: Dict[Any, Dict[str, Any]] = {}
    row_errors = []
    for batch in batch_results:
        for result in batch.get("row_results", []):
            encoded_by_id[result["row_id"]] = result
        row_errors.extend(batch.get("row_errors", []))

    ordered_results = []
    missing = []
    for row in rows:
        row_id = row["row_id"]
        if row_id in encoded_by_id:
            ordered_results.append(encoded_by_id[row_id])
        else:
            missing.append(row_id)
    return ordered_results, row_errors, missing


def write_output_manifest(output_dir: Path) -> Dict[str, Any]:
    files = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(output_dir).as_posix()
        if relative == "output_manifest.json":
            continue
        files.append({"path": relative, "sha256": sha256_file(path), "bytes": path.stat().st_size})
    manifest = {"schema": "engine-output-manifest/v1", "files": files}
    write_atomic_json(output_dir / "output_manifest.json", manifest)
    return manifest


def run_pipeline(
    config: Union[str, Path, Mapping[str, Any]],
    *,
    samples_path: Optional[Union[str, Path]] = None,
    records: Optional[Sequence[Mapping[str, Any]]] = None,
    output_dir: Union[str, Path] = "runs/default",
    cache_dir: Optional[Union[str, Path]] = None,
    workers: Optional[int] = None,
    batch_size: Optional[int] = None,
    use_process_pool: bool = True,
    resume: bool = True,
    kill_after_batch: Optional[int] = None,
    keep_going: bool = False,
    write_snapshots: bool = True,
) -> EngineResult:
    if isinstance(config, (str, Path)):
        normalized_config = load_config(Path(config))
    else:
        normalized_config = normalize_config(config)

    requested_workers = int(
        workers if workers is not None else normalized_config["scheduler"]["workers"]
    )
    requested_batch_size = int(
        batch_size
        if batch_size is not None
        else normalized_config["scheduler"]["batch_size"]
    )
    identity = _input_identity(samples_path, records)
    run_seed = canonical_hash(
        {
            "identity": identity,
            "config": normalized_config,
            "requested_workers": requested_workers,
            "requested_batch_size": requested_batch_size,
            "engine": "binning-diagnostic-engine-v1",
        }
    )
    run_id = f"run-{run_seed[:24]}"

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    cache = ContentCache(Path(cache_dir) if cache_dir is not None else out / "cache")
    snapshots = SnapshotStore(out / "snapshots") if write_snapshots else None
    if snapshots is not None:
        latest_config = snapshots.get_latest("binning_config")
        config_checksum = canonical_hash(normalized_config)
        if latest_config is not None:
            latest_version = int(latest_config["version"])
            incoming_version = int(normalized_config["version"])
            if incoming_version < latest_version:
                raise EngineError(
                    ErrorCode.E_CONFIG_VERSION,
                    "configuration version rollback is not allowed",
                    {
                        "attempted_version": incoming_version,
                        "current_version": latest_version,
                    },
                )
            if (
                incoming_version == latest_version
                and latest_config.get("checksum") != config_checksum
            ):
                raise EngineError(
                    ErrorCode.E_CONFIG_VERSION,
                    "configuration checksum changed without a monotonic version bump",
                    {"version": incoming_version},
                )
        snapshots.put(
            "binning_config", normalized_config, int(normalized_config["version"])
        )
        quantization_snapshot = {
            "version": int(normalized_config["version"]),
            "columns": {
                name: {
                    "quant_scale": spec["quant_scale"],
                    "zero_point": spec["zero_point"],
                }
                for name, spec in normalized_config["column_specs"].items()
            },
        }
        snapshots.put(
            "quantization_config",
            quantization_snapshot,
            int(normalized_config["version"]),
        )
    checkpoints = CheckpointStore(out, run_id)
    if resume:
        checkpoints.load()

    validation_spec = {
        "input_identity": identity,
        "config_version": normalized_config["version"],
        "validation_rules": canonical_hash(
            {
                "numeric_columns": normalized_config["numeric_columns"],
                "categorical_columns": normalized_config["categorical_columns"],
                "vocabulary": normalized_config["vocabulary"],
                "label_column": normalized_config["label_column"],
            }
        ),
        "node_params": {"stage": "input_validation", "parser": "jsonl-v1"},
    }
    validation_key = make_cache_key("input_validation", validation_spec)
    validation_data = cache.get("input_validation", validation_key)
    if validation_data is None:
        if samples_path is not None:
            validation_data = load_and_validate(Path(samples_path), normalized_config)
        else:
            validation_data = validate_samples(
                list(records or []),
                normalized_config,
                raw_input_sha256=identity.get("input_sha256"),
            )
        cache.put("input_validation", validation_key, validation_data)
    checkpoints.commit_stage(
        "input_validation", validation_data["summary"]["sample_content_hash"]
    )
    rows = validation_data["rows"]

    resource_plan = plan_resources(
        normalized_config,
        len(rows),
        requested_workers=requested_workers,
        requested_batch_size=requested_batch_size,
    )
    effective_config = effective_config_with_resources(normalized_config, resource_plan)

    boundary_spec = {
        "validation_key": validation_key,
        "sample_content_hash": validation_data["sample_content_hash"],
        "config_version": normalized_config["version"],
        "effective_bins": {
            name: {
                "bin_count": spec["bin_count"],
                "bin_method": spec["bin_method"],
            }
            for name, spec in effective_config["column_specs"].items()
        },
        "node_params": {"stage": "bin_boundaries", "quantile": "linear-r7-equivalent"},
    }
    boundary_key = make_cache_key("bin_boundaries", boundary_spec)
    boundary_result = cache.get("bin_boundaries", boundary_key)
    if boundary_result is None:
        boundary_result = compute_boundaries(rows, effective_config)
        boundary_result["sample_content_hash"] = validation_data["sample_content_hash"]
        cache.put("bin_boundaries", boundary_key, boundary_result)
    checkpoints.commit_stage("bin_boundaries", boundary_result["boundary_hash"])

    encoding_semantic_hash = canonical_hash(
        {
            "encoding": {
                name: {
                    "method": spec["encoding_method"],
                    "smoothing": spec["smoothing"],
                    "quant_scale": spec["quant_scale"],
                    "zero_point": spec["zero_point"],
                    "bin_count": spec["bin_count"],
                }
                for name, spec in effective_config["column_specs"].items()
            },
            "precision_tier": effective_config["precision_tier"],
        }
    )
    encoding_spec = {
        "boundary_key": boundary_key,
        "boundary_hash": boundary_result["boundary_hash"],
        "config_version": normalized_config["version"],
        "encoding_semantic_hash": encoding_semantic_hash,
        "node_params": {"stage": "encoding_generation", "feature_spec_version": 1},
    }
    encoding_key = make_cache_key("encoding_generation", encoding_spec)
    encoding_stage = cache.get("encoding_generation", encoding_key)
    if encoding_stage is None:
        feature_spec = build_feature_spec(effective_config, boundary_result)
        context = build_encoding_context(rows, effective_config, boundary_result)
        encoding_stage = {"feature_spec": feature_spec, "context": context}
        cache.put("encoding_generation", encoding_key, encoding_stage)
    checkpoints.commit_stage(
        "encoding_generation", encoding_stage["context"]["context_hash"]
    )
    feature_spec = encoding_stage["feature_spec"]
    context = encoding_stage["context"]

    batch_plan = build_batch_plan(
        [row["row_id"] for row in rows], int(resource_plan["batch_size"])
    )
    tasks = []
    results_by_id: Dict[str, Dict[str, Any]] = {}
    recovered_batches = []
    pending_descriptors = []

    for descriptor in batch_plan:
        batch_id = descriptor["batch_id"]
        if checkpoints.has_batch(batch_id):
            artifact = checkpoints.batch_record(batch_id)
            if artifact is not None:
                if list(artifact.get("row_ids", [])) != descriptor["row_ids"]:
                    raise EngineError(
                        ErrorCode.E_CHECKPOINT_CORRUPT,
                        "checkpoint batch row_ids do not match deterministic batch plan",
                        batch_id=batch_id,
                    )
                results_by_id[batch_id] = artifact
                recovered_batches.append(batch_id)
                continue
        start = descriptor["start_row_position"]
        end = descriptor["end_row_position"]
        tasks.append(
            {
                "batch_id": batch_id,
                "rows": rows[start:end],
                "config": effective_config,
                "boundary_result": boundary_result,
                "feature_spec": feature_spec,
                "context": context,
                "isolate_failures": True,
                "resource_decisions": resource_plan["decisions"],
            }
        )
        pending_descriptors.append(descriptor)

    committed_in_process = 0

    def persist_batch(result: Dict[str, Any]) -> None:
        nonlocal committed_in_process
        batch_id = str(result["batch_id"])
        checkpoints.commit_batch(batch_id, result)
        committed_in_process += 1
        if kill_after_batch is not None and committed_in_process >= int(kill_after_batch):
            # CheckpointStore.save has atomically replaced the durable file.
            os.kill(os.getpid(), signal.SIGKILL)

    scheduler = BatchScheduler(
        workers=int(resource_plan["workers"]),
        use_process_pool=use_process_pool,
    )
    pending_results = scheduler.run_ordered(tasks, on_batch_result=persist_batch)
    for result in pending_results:
        results_by_id[str(result["batch_id"])] = result

    batch_results = [results_by_id[item["batch_id"]] for item in batch_plan]
    ordered_rows, row_errors, missing_row_ids = _ordered_encoded_rows(rows, batch_results)
    if missing_row_ids:
        raise EngineError(
            ErrorCode.E_WORKER,
            "some rows have no committed encoding result",
            details={"missing_row_ids": missing_row_ids},
        )

    fatal_batches = [batch for batch in batch_results if batch.get("batch_error")]
    if fatal_batches:
        fatal = fatal_batches[0]["batch_error"]
        raise EngineError(
            fatal.get("code", ErrorCode.E_WORKER),
            fatal.get("message", "batch worker failed"),
            details=fatal.get("details"),
            batch_id=fatal.get("batch_id"),
        )

    total_comparisons = int(context.get("comparison_count", 0)) + sum(
        int(batch.get("comparison_count", 0)) for batch in batch_results
    )
    if total_comparisons > int(resource_plan["budget"]["max_comparisons"]):
        raise EngineError(
            ErrorCode.E_RESOURCE_LIMIT,
            "actual comparison count exceeds configured cap",
            {
                "comparison_count": total_comparisons,
                "max_comparisons": resource_plan["budget"]["max_comparisons"],
            },
        )

    if row_errors and not keep_going:
        first_error = row_errors[0]["error"]
        raise EngineError(
            first_error.get("code", ErrorCode.E_INVALID_INPUT),
            first_error.get("message", "row processing failed"),
            details=first_error.get("details"),
            row_id=first_error.get("row_id"),
            batch_id=first_error.get("batch_id"),
        )

    global_error_interval = {
        "observed_min": min(
            row["error_interval"]["observed_min"] for row in ordered_rows
        ),
        "observed_max": max(
            row["error_interval"]["observed_max"] for row in ordered_rows
        ),
        "observed_abs_max": max(
            row["error_interval"]["observed_abs_max"] for row in ordered_rows
        ),
        "theoretical_half_step": max(
            row["error_interval"]["theoretical_half_step"] for row in ordered_rows
        ),
        "clipped_count": sum(
            row["error_interval"].get("clipped_count", 0) for row in ordered_rows
        ),
    }
    encoding_result: Dict[str, Any] = {
        "schema": "binning-encoding-result/v1",
        "engine_version": "1.0.0",
        "version": int(normalized_config["version"]),
        "config_name": normalized_config["config_name"],
        "config_version": int(normalized_config["version"]),
        "sample_content_hash": validation_data["sample_content_hash"],
        "boundary_hash": boundary_result["boundary_hash"],
        "feature_spec": feature_spec,
        "rows": ordered_rows,
        "row_errors": row_errors,
        "business_codes": sorted(
            {error["error"]["code"] for error in row_errors}
            | {decision["code"] for decision in resource_plan["decisions"]}
        ),
        "quantization_error": global_error_interval,
    }
    encoding_result["result_hash"] = canonical_hash(
        {key: value for key, value in encoding_result.items() if key != "result_hash"}
    )

    statistics_spec = {
        "stage": "stats_export",
        "validation_key": validation_key,
        "boundary_key": boundary_key,
        "encoding_key": encoding_key,
        "encoding_result_hash": encoding_result["result_hash"],
        "config_version": normalized_config["version"],
        "node_params": {"export_format": "canonical-json-v1"},
    }
    statistics_key = make_cache_key("stats_export", statistics_spec)
    statistics = cache.get("stats_export", statistics_key)
    if statistics is None:
        statistics = build_statistics(
            rows=rows,
            config=effective_config,
            boundaries=boundary_result,
            feature_spec=feature_spec,
            context=context,
            encoded_rows=ordered_rows,
            encoding_result_hash=encoding_result["result_hash"],
            row_errors=row_errors,
        )
        cache.put("stats_export", statistics_key, statistics)
    checkpoints.commit_stage("stats_export", statistics["statistics_hash"])

    self_check = cross_check_cache_and_artifacts(
        cache, encoding_result=encoding_result, statistics=statistics
    )
    if not self_check["ok"]:
        raise EngineError(
            ErrorCode.E_SELF_CHECK,
            "cache/artifact consistency self-check failed",
            {"self_check": self_check},
        )

    write_atomic_json(out / "encoding_result.json", encoding_result)
    write_atomic_json(out / "statistics.json", statistics)
    write_atomic_json(out / "resource_plan.json", resource_plan)
    write_atomic_json(
        out / "batch_plan.json",
        {
            "batch_size": int(resource_plan["batch_size"]),
            "batch_count": len(batch_plan),
            "batches": batch_plan,
        },
    )

    snapshot_envelopes = {}
    if snapshots is not None:
        snapshot_envelopes["encoding_result"] = snapshots.put(
            "encoding_result", encoding_result, int(normalized_config["version"])
        )
        snapshot_envelopes["feature_statistics"] = snapshots.put(
            "feature_statistics", statistics, int(normalized_config["version"])
        )
        cache_statistics = {
            "schema": "cache-statistics/v1",
            "version": int(normalized_config["version"]),
            "inventory": cache.inventory(),
        }
        snapshot_envelopes["cache_statistics"] = snapshots.put(
            "cache_statistics", cache_statistics, int(normalized_config["version"])
        )

    manifest = write_output_manifest(out)
    runtime_report = {
        "ok": True,
        "run_id": run_id,
        "output_dir": str(out),
        "workers": int(resource_plan["workers"]),
        "batch_size": int(resource_plan["batch_size"]),
        "recovered_batches": recovered_batches,
        "new_batch_count": len(pending_results),
        "batch_summaries": [
            {
                "batch_id": batch.get("batch_id"),
                "ok": batch.get("ok"),
                "row_count": batch.get("row_count"),
                "success_count": batch.get("success_count"),
                "error_count": batch.get("error_count"),
                "business_codes": batch.get("business_codes", []),
                "comparison_count": batch.get("comparison_count"),
                "resource_decisions": batch.get("resource_decisions", []),
            }
            for batch in batch_results
        ],
        "comparison_count": total_comparisons,
        "runtime_memory": read_runtime_memory(),
        "resource_plan": resource_plan,
        "scheduler_order": scheduler.order_report(),
        "cache_events": cache.event_summary(),
        "cache_inventory": cache.inventory(),
        "self_check": self_check,
        "manifest": manifest,
    }
    runtime_report.update(
        {
            "encoding_result": encoding_result,
            "statistics": statistics,
            "snapshots": snapshot_envelopes,
        }
    )
    return runtime_report
