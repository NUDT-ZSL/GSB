from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .determinism import dataset_fingerprint, shard_manifest
from .errors import BusinessError, ErrorCode
from .models import (
    CacheEntrySpec,
    CacheViewSpec,
    DatasetSpec,
    JobSpec,
    ScheduleConfig,
    Workload,
)
from .util import sha256_value


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BusinessError(
                ErrorCode.DUPLICATE_JSON_KEY,
                f"duplicate JSON key: {key}",
                details={"key": key},
            )
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BusinessError(
            ErrorCode.INVALID_WORKLOAD,
            f"cannot read workload JSON: {exc}",
            details={"path": str(path)},
        ) from exc
    try:
        return json.loads(text, object_pairs_hook=_json_pairs)
    except BusinessError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BusinessError(
            ErrorCode.INVALID_JSON,
            f"invalid workload JSON: {exc}",
            details={"path": str(path)},
        ) from exc


def load_workload(path: str | Path) -> Workload:
    return parse_workload(read_json(Path(path)))


def _required(mapping: dict[str, Any], names: tuple[str, ...], context: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    raise BusinessError(
        ErrorCode.INVALID_WORKLOAD,
        f"missing required field {names[0]} in {context}",
        details={"context": context, "fields": list(names)},
    )


def _as_dict(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BusinessError(
            ErrorCode.INVALID_WORKLOAD,
            f"{context} must be an object",
            details={"context": context},
        )
    return value


def _as_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise BusinessError(
            ErrorCode.INVALID_WORKLOAD,
            f"{context} must be an array",
            details={"context": context},
        )
    return value


def _integer(
    value: Any,
    code: ErrorCode,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BusinessError(
            code,
            f"{field} must be an integer",
            details={"field": field, "value": value},
        )
    if minimum is not None and value < minimum:
        raise BusinessError(
            code,
            f"{field} must be at least {minimum}",
            details={"field": field, "value": value, "minimum": minimum},
        )
    if maximum is not None and value > maximum:
        raise BusinessError(
            code,
            f"{field} must be at most {maximum}",
            details={"field": field, "value": value, "maximum": maximum},
        )
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise BusinessError(
            ErrorCode.INVALID_BOOLEAN,
            f"{field} must be a boolean",
            details={"field": field, "value": value},
        )
    return value


def _name(value: Any, field: str, code: ErrorCode) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BusinessError(
            code,
            f"{field} must be a non-empty string",
            details={"field": field, "value": value},
        )
    if any(ord(character) < 32 for character in value):
        raise BusinessError(
            code,
            f"{field} cannot contain control characters",
            details={"field": field},
        )
    return value


def _parse_dataset(value: Any, index: int) -> DatasetSpec:
    raw = _as_dict(value, f"datasets[{index}]")
    name = _name(_required(raw, ("name", "dataset_name"), "dataset"), "dataset.name", ErrorCode.INVALID_NAME)
    sample_count = _integer(
        _required(raw, ("sample_count", "num_samples"), "dataset"),
        ErrorCode.INVALID_SAMPLE_COUNT,
        f"dataset {name}.sample_count",
        minimum=1,
    )
    feature_dim = _integer(
        _required(raw, ("feature_dim", "dimension"), "dataset"),
        ErrorCode.INVALID_FEATURE_DIM,
        f"dataset {name}.feature_dim",
        minimum=1,
    )
    sample_bytes = _integer(
        _required(raw, ("sample_bytes", "bytes_per_sample"), "dataset"),
        ErrorCode.INVALID_SAMPLE_BYTES,
        f"dataset {name}.sample_bytes",
        minimum=feature_dim * 4,
    )
    shard_size = _integer(
        _required(raw, ("shard_size",), "dataset"),
        ErrorCode.INVALID_SHARD_SIZE,
        f"dataset {name}.shard_size",
        minimum=1,
    )
    if sample_count % shard_size != 0:
        raise BusinessError(
            ErrorCode.SHARD_SIZE_NOT_DIVISIBLE,
            "shard_size must divide sample_count",
            details={
                "dataset": name,
                "sample_count": sample_count,
                "shard_size": shard_size,
            },
        )
    cacheable_raw = _required(raw, ("cacheable", "is_cacheable"), "dataset")
    cacheable = _boolean(cacheable_raw, f"dataset {name}.cacheable")
    version = _name(
        raw.get("version", "v1"), f"dataset {name}.version", ErrorCode.INVALID_NAME
    )
    generation_params_raw = raw.get("generation_params", {})
    generation_params = _as_dict(
        generation_params_raw, f"dataset {name}.generation_params"
    )
    return DatasetSpec(
        name=name,
        sample_count=sample_count,
        feature_dim=feature_dim,
        sample_bytes=sample_bytes,
        cacheable=cacheable,
        shard_size=shard_size,
        version=version,
        generation_params=generation_params,
    )


def _parse_cache(
    value: Any,
    dataset_map: dict[str, DatasetSpec],
    global_seed: int,
    fingerprint_lookup: Callable[[DatasetSpec], str],
) -> CacheViewSpec:
    raw = _as_dict(value, "cache")
    generation = _integer(
        _required(raw, ("generation", "cache_generation"), "cache"),
        ErrorCode.INVALID_CACHE_GENERATION,
        "cache.generation",
        minimum=0,
    )
    version = _name(
        raw.get("version", "cache-v1"), "cache.version", ErrorCode.INVALID_NAME
    )
    entries_raw = _as_list(raw.get("entries", []), "cache.entries")
    entries: list[CacheEntrySpec] = []
    seen: set[tuple[str, int]] = set()
    for index, entry_value in enumerate(entries_raw):
        entry_raw = _as_dict(entry_value, f"cache.entries[{index}]")
        dataset_name = _name(
            _required(entry_raw, ("dataset", "dataset_name"), "cache entry"),
            "cache entry.dataset",
            ErrorCode.INVALID_NAME,
        )
        dataset = dataset_map.get(dataset_name)
        if dataset is None:
            raise BusinessError(
                ErrorCode.UNKNOWN_DATASET,
                f"cache entry references unknown dataset {dataset_name!r}",
                details={"dataset": dataset_name},
            )
        shard_index = _integer(
            _required(entry_raw, ("shard_index", "shard"), "cache entry"),
            ErrorCode.INVALID_SHARD_INDEX,
            "cache entry.shard_index",
            minimum=0,
            maximum=dataset.shard_count - 1,
        )
        entry_generation = _integer(
            _required(
                entry_raw,
                ("generation", "cache_generation"),
                "cache entry",
            ),
            ErrorCode.INVALID_CACHE_GENERATION,
            "cache entry.generation",
            minimum=0,
            maximum=generation,
        )
        byte_count = _integer(
            _required(entry_raw, ("bytes", "byte_size"), "cache entry"),
            ErrorCode.CACHE_BYTES_MISMATCH,
            "cache entry.bytes",
            minimum=1,
        )
        expected_bytes = dataset.shard_bytes
        if byte_count != expected_bytes:
            raise BusinessError(
                ErrorCode.CACHE_BYTES_MISMATCH,
                "cache entry bytes do not match the shard declaration",
                details={
                    "dataset": dataset_name,
                    "shard_index": shard_index,
                    "entry_bytes": byte_count,
                    "expected_bytes": expected_bytes,
                },
            )
        last_used_clock = _integer(
            _required(entry_raw, ("last_used_clock", "last_used"), "cache entry"),
            ErrorCode.INVALID_CACHE_GENERATION,
            "cache entry.last_used_clock",
            minimum=0,
        )
        fingerprint = entry_raw.get("generation_fingerprint")
        if fingerprint is None:
            fingerprint = fingerprint_lookup(dataset)
        fingerprint = _name(
            fingerprint,
            "cache entry.generation_fingerprint",
            ErrorCode.INVALID_NAME,
        )
        digest = entry_raw.get("digest")
        manifest = shard_manifest(
            global_seed=global_seed,
            dataset=dataset,
            dataset_fingerprint_value=fingerprint,
            shard_index=shard_index,
        )
        if digest is None:
            digest = manifest["digest"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise BusinessError(
                ErrorCode.INVALID_WORKLOAD,
                "cache entry.digest must be a lowercase sha256 hex string",
                details={"dataset": dataset_name, "shard_index": shard_index},
            )
        if digest != manifest["digest"]:
            raise BusinessError(
                ErrorCode.CACHE_DIGEST_MISMATCH,
                "cache entry digest does not match deterministic shard content",
                details={
                    "dataset": dataset_name,
                    "shard_index": shard_index,
                    "entry_digest": digest,
                    "expected_digest": manifest["digest"],
                },
            )
        identity = (dataset_name, shard_index)
        if identity in seen:
            raise BusinessError(
                ErrorCode.DUPLICATE_CACHE_ENTRY,
                "cache entries must be unique by dataset and shard",
                details={"dataset": dataset_name, "shard_index": shard_index},
            )
        seen.add(identity)
        entries.append(
            CacheEntrySpec(
                dataset=dataset_name,
                shard_index=shard_index,
                generation=entry_generation,
                bytes=byte_count,
                last_used_clock=last_used_clock,
                digest=digest,
                generation_fingerprint=fingerprint,
            )
        )

    entries.sort(key=lambda item: (item.dataset, item.shard_index))
    previous_dataset: str | None = None
    previous_generation = -1
    for entry in entries:
        if previous_dataset is not None and entry.dataset == previous_dataset:
            if entry.generation < previous_generation:
                raise BusinessError(
                    ErrorCode.CACHE_GENERATION_NOT_MONOTONIC,
                    "cache generations must be monotonic within each dataset",
                    details={
                        "dataset": entry.dataset,
                        "shard_index": entry.shard_index,
                        "generation": entry.generation,
                        "previous_generation": previous_generation,
                    },
                )
        previous_dataset = entry.dataset
        previous_generation = entry.generation

    return CacheViewSpec(generation=generation, entries=tuple(entries), version=version)


def _parse_job(value: Any, index: int, dataset_map: dict[str, DatasetSpec]) -> JobSpec:
    raw = _as_dict(value, f"jobs[{index}]")
    name = _name(
        _required(raw, ("name", "job_name"), "job"),
        "job.name",
        ErrorCode.INVALID_NAME,
    )
    dataset_name = _name(
        _required(raw, ("dataset", "dataset_name"), "job"),
        f"job {name}.dataset",
        ErrorCode.INVALID_NAME,
    )
    if dataset_name not in dataset_map:
        raise BusinessError(
            ErrorCode.UNKNOWN_DATASET,
            f"job {name!r} references unknown dataset {dataset_name!r}",
            details={"job": name, "dataset": dataset_name},
        )
    batch_size = _integer(
        _required(raw, ("batch_size", "batch"), "job"),
        ErrorCode.INVALID_BATCH_SIZE,
        f"job {name}.batch_size",
        minimum=1,
    )
    epochs = _integer(
        raw.get("epochs", 1),
        ErrorCode.INVALID_EPOCHS,
        f"job {name}.epochs",
        minimum=1,
    )
    data_parallelism = _integer(
        _required(raw, ("data_parallelism", "world_size"), "job"),
        ErrorCode.INVALID_DATA_PARALLELISM,
        f"job {name}.data_parallelism",
        minimum=1,
    )
    prefetch_depth = _integer(
        _required(raw, ("prefetch_depth",), "job"),
        ErrorCode.INVALID_PREFETCH_DEPTH,
        f"job {name}.prefetch_depth",
        minimum=0,
    )
    allow_reuse = _boolean(
        _required(
            raw,
            ("allow_shard_reuse", "allow_cross_worker_shard_reuse"),
            "job",
        ),
        f"job {name}.allow_shard_reuse",
    )
    return JobSpec(
        name=name,
        dataset=dataset_name,
        batch_size=batch_size,
        epochs=epochs,
        data_parallelism=data_parallelism,
        prefetch_depth=prefetch_depth,
        allow_shard_reuse=allow_reuse,
    )


def _parse_config(value: Any) -> ScheduleConfig:
    raw = _as_dict(value, "scheduling")
    generation_cost = _as_dict(
        raw.get("generation_cost", {}), "scheduling.generation_cost"
    )
    budget = _integer(
        _required(
            raw,
            ("logical_time_budget_ms", "per_round_time_budget_ms"),
            "scheduling",
        ),
        ErrorCode.INVALID_CONFIG,
        "scheduling.logical_time_budget_ms",
        minimum=1,
    )
    per_dataset_limit = _integer(
        _required(raw, ("per_dataset_prefetch_limit",), "scheduling"),
        ErrorCode.INVALID_CONFIG,
        "scheduling.per_dataset_prefetch_limit",
        minimum=1,
    )
    memory_mib = _integer(
        _required(raw, ("global_memory_budget_mib",), "scheduling"),
        ErrorCode.INVALID_CONFIG,
        "scheduling.global_memory_budget_mib",
        minimum=1,
    )
    cache_capacity = _integer(
        _required(raw, ("cache_capacity_entries",), "scheduling"),
        ErrorCode.INVALID_CONFIG,
        "scheduling.cache_capacity_entries",
        minimum=1,
    )
    tolerance = _integer(
        raw.get("tolerance_ms", 0),
        ErrorCode.INVALID_CONFIG,
        "scheduling.tolerance_ms",
        minimum=0,
    )
    step = _integer(
        _required(raw, ("logical_clock_step_ms",), "scheduling"),
        ErrorCode.INVALID_CONFIG,
        "scheduling.logical_clock_step_ms",
        minimum=1,
    )
    start = _integer(
        raw.get("logical_clock_start_ms", 0),
        ErrorCode.INVALID_CONFIG,
        "scheduling.logical_clock_start_ms",
        minimum=0,
    )
    base = _integer(
        generation_cost.get("base_ms", raw.get("generation_base_ms", 1)),
        ErrorCode.INVALID_CONFIG,
        "scheduling.generation_cost.base_ms",
        minimum=0,
    )
    bytes_per_ms = _integer(
        generation_cost.get(
            "bytes_per_ms", raw.get("generation_bytes_per_ms", 4096)
        ),
        ErrorCode.INVALID_CONFIG,
        "scheduling.generation_cost.bytes_per_ms",
        minimum=1,
    )
    hit_ms = _integer(
        generation_cost.get("hit_ms", raw.get("cache_hit_ms", 0)),
        ErrorCode.INVALID_CONFIG,
        "scheduling.generation_cost.hit_ms",
        minimum=0,
    )
    return ScheduleConfig(
        logical_time_budget_ms=budget,
        per_dataset_prefetch_limit=per_dataset_limit,
        global_memory_budget_mib=memory_mib,
        cache_capacity_entries=cache_capacity,
        tolerance_ms=tolerance,
        logical_clock_step_ms=step,
        logical_clock_start_ms=start,
        generation_base_ms=base,
        generation_bytes_per_ms=bytes_per_ms,
        cache_hit_ms=hit_ms,
    )


def parse_workload(document: Any) -> Workload:
    raw = _as_dict(document, "workload")
    global_seed = _integer(
        _required(raw, ("global_seed", "seed"), "workload"),
        ErrorCode.INVALID_GLOBAL_SEED,
        "global_seed",
        minimum=0,
    )
    datasets_raw = _as_list(
        _required(raw, ("datasets", "dataset_manifest"), "workload"),
        "datasets",
    )
    if not datasets_raw:
        raise BusinessError(
            ErrorCode.INVALID_WORKLOAD, "datasets cannot be empty"
        )
    datasets: list[DatasetSpec] = []
    dataset_map: dict[str, DatasetSpec] = {}
    for index, dataset_value in enumerate(datasets_raw):
        dataset = _parse_dataset(dataset_value, index)
        if dataset.name in dataset_map:
            raise BusinessError(
                ErrorCode.DUPLICATE_DATASET,
                f"duplicate dataset name: {dataset.name}",
                details={"dataset": dataset.name},
            )
        dataset_map[dataset.name] = dataset
        datasets.append(dataset)

    fingerprint_cache: dict[str, str] = {}

    def fingerprint_lookup(dataset: DatasetSpec) -> str:
        if dataset.name not in fingerprint_cache:
            fingerprint_cache[dataset.name] = dataset_fingerprint(
                global_seed, dataset
            )
        return fingerprint_cache[dataset.name]

    for dataset in datasets:
        fingerprint_lookup(dataset)

    cache_raw = _required(raw, ("cache", "cache_view"), "workload")
    cache = _parse_cache(cache_raw, dataset_map, global_seed, fingerprint_lookup)

    if "jobs" in raw:
        jobs_raw = _as_list(raw["jobs"], "jobs")
    elif "job" in raw:
        jobs_raw = [raw["job"]]
    else:
        raise BusinessError(
            ErrorCode.INVALID_WORKLOAD,
            "missing required field job or jobs",
            details={"context": "workload"},
        )
    if not jobs_raw:
        raise BusinessError(ErrorCode.INVALID_WORKLOAD, "jobs cannot be empty")
    jobs: list[JobSpec] = []
    job_names: set[str] = set()
    for index, job_value in enumerate(jobs_raw):
        job = _parse_job(job_value, index, dataset_map)
        if job.name in job_names:
            raise BusinessError(
                ErrorCode.DUPLICATE_JOB,
                f"duplicate job name: {job.name}",
                details={"job": job.name},
            )
        job_names.add(job.name)
        jobs.append(job)

    config = _parse_config(
        _required(raw, ("scheduling", "schedule_config", "config"), "workload")
    )

    canonical: dict[str, Any] = {
        "global_seed": global_seed,
        "datasets": [
            {
                "name": dataset.name,
                "sample_count": dataset.sample_count,
                "feature_dim": dataset.feature_dim,
                "sample_bytes": dataset.sample_bytes,
                "cacheable": dataset.cacheable,
                "shard_size": dataset.shard_size,
                "version": dataset.version,
                "generation_params": dict(dataset.generation_params),
                "generation_fingerprint": fingerprint_lookup(dataset),
            }
            for dataset in datasets
        ],
        "cache": {
            "generation": cache.generation,
            "version": cache.version,
            "entries": [
                {
                    "dataset": entry.dataset,
                    "shard_index": entry.shard_index,
                    "generation": entry.generation,
                    "bytes": entry.bytes,
                    "last_used_clock": entry.last_used_clock,
                    "digest": entry.digest,
                    "generation_fingerprint": entry.generation_fingerprint,
                }
                for entry in cache.entries
            ],
        },
        "jobs": [
            {
                "name": job.name,
                "dataset": job.dataset,
                "batch_size": job.batch_size,
                "epochs": job.epochs,
                "data_parallelism": job.data_parallelism,
                "prefetch_depth": job.prefetch_depth,
                "allow_shard_reuse": job.allow_shard_reuse,
            }
            for job in jobs
        ],
        "scheduling": {
            "logical_time_budget_ms": config.logical_time_budget_ms,
            "per_dataset_prefetch_limit": config.per_dataset_prefetch_limit,
            "global_memory_budget_mib": config.global_memory_budget_mib,
            "cache_capacity_entries": config.cache_capacity_entries,
            "tolerance_ms": config.tolerance_ms,
            "logical_clock_step_ms": config.logical_clock_step_ms,
            "logical_clock_start_ms": config.logical_clock_start_ms,
            "generation_cost": {
                "base_ms": config.generation_base_ms,
                "bytes_per_ms": config.generation_bytes_per_ms,
                "hit_ms": config.cache_hit_ms,
            },
        },
    }
    canonical["_dataset_fingerprints"] = {
        dataset.name: fingerprint_lookup(dataset) for dataset in datasets
    }
    return Workload(
        global_seed=global_seed,
        datasets=tuple(datasets),
        cache=cache,
        jobs=tuple(jobs),
        config=config,
        canonical=canonical,
        digest=sha256_value(canonical),
    )
