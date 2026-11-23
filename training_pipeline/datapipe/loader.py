"""Parse and strictly validate every local JSON input file.

Validation runs entirely before any persistent state is created, so malformed
input can never leave partial cache / SQLite artifacts behind.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple

from .errors import (
    E_BAD_REFERENCE,
    E_CACHE_DIGEST_MISMATCH,
    E_DIVISIBILITY,
    E_DUPLICATE_NAME,
    E_VALIDATION,
    fail,
)
from .model import (
    CacheEntryView,
    ClockStep,
    DatasetSpec,
    GenParams,
    Injection,
    JobSpec,
    Scenario,
)
from .sharding import build_shard_manifest, cache_algebra

VALID_STAGES = ("generate", "cache", "prefetch", "assemble")
VALID_KINDS = (
    "gen_digest_mismatch",
    "cache_algebra_rollback",
    "cache_write_failure",
    "prefetch_failure",
    "prefetch_timeout",
    "clock_rollback",
    "batch_digest_conflict",
    "crash",
)


def _read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise fail(E_VALIDATION, "input file not found", path=path)
    except json.JSONDecodeError as exc:
        raise fail(E_VALIDATION, "invalid JSON", path=path, detail=str(exc))


def _positive_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise fail(E_VALIDATION, f"{field_name} must be a positive integer", got=repr(value))
    return value


def _non_negative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise fail(E_VALIDATION, f"{field_name} must be a non-negative integer", got=repr(value))
    return value


def _parse_datasets(obj: Any) -> Tuple[DatasetSpec, ...]:
    if not isinstance(obj, list) or not obj:
        raise fail(E_VALIDATION, "datasets must be a non-empty list")
    seen = set()
    specs: List[DatasetSpec] = []
    for item in obj:
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise fail(E_VALIDATION, "dataset name must be a non-empty string")
        if name in seen:
            raise fail(E_DUPLICATE_NAME, "duplicate dataset name", name=name)
        seen.add(name)

        total = _positive_int(item.get("total_samples"), "total_samples")
        feature_dim = _positive_int(item.get("feature_dim"), "feature_dim")
        bytes_per_sample = _positive_int(item.get("bytes_per_sample"), "bytes_per_sample")
        shard_size = _positive_int(item.get("shard_size"), "shard_size")
        seed = _non_negative_int(item.get("global_seed"), "global_seed")
        if total % shard_size != 0:
            raise fail(
                E_DIVISIBILITY,
                "shard_size must divide total_samples",
                dataset=name,
                total_samples=total,
                shard_size=shard_size,
            )
        gen_obj = item.get("gen_params", {})
        if not isinstance(gen_obj, dict):
            raise fail(E_VALIDATION, "gen_params must be an object", dataset=name)
        gen = GenParams.from_obj(gen_obj)
        max_conc = _positive_int(item.get("max_concurrent_prefetch", 1), "max_concurrent_prefetch")
        specs.append(
            DatasetSpec(
                name=name,
                total_samples=total,
                feature_dim=feature_dim,
                bytes_per_sample=bytes_per_sample,
                shard_size=shard_size,
                global_seed=seed,
                gen_params=gen,
                max_concurrent_prefetch=max_conc,
            )
        )
    return tuple(specs)


def _parse_job(obj: Any, dataset_names: List[str]) -> JobSpec:
    name = obj.get("name")
    if not isinstance(name, str) or not name:
        raise fail(E_VALIDATION, "job name must be a non-empty string")
    batch_size = _positive_int(obj.get("batch_size"), "batch_size")
    data_parallel = _positive_int(obj.get("data_parallel"), "data_parallel")
    prefetch_depth = _positive_int(obj.get("prefetch_depth"), "prefetch_depth")
    epochs = _positive_int(obj.get("epochs"), "epochs")
    memory_mib = _positive_int(obj.get("memory_budget_mib"), "memory_budget_mib")
    round_budget = _positive_int(obj.get("round_time_budget_ms"), "round_time_budget_ms")
    datasets = obj.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        raise fail(E_VALIDATION, "job.datasets must be a non-empty list")
    for dataset in datasets:
        if dataset not in dataset_names:
            raise fail(E_BAD_REFERENCE, "job references unknown dataset", dataset=dataset, job=name)
    if len(set(datasets)) != len(datasets):
        raise fail(E_DUPLICATE_NAME, "job datasets must be unique", job=name)
    budget_bytes = int(obj.get("memory_budget_bytes", memory_mib * 1024 * 1024))
    return JobSpec(
        name=name,
        batch_size=batch_size,
        data_parallel=data_parallel,
        prefetch_depth=prefetch_depth,
        epochs=epochs,
        memory_budget_bytes=budget_bytes,
        round_time_budget_ms=round_budget,
        datasets=tuple(datasets),
    )


def _parse_cache_view(obj: Any, datasets: Dict[str, DatasetSpec]) -> Tuple[CacheEntryView, ...]:
    if not isinstance(obj, list):
        raise fail(E_VALIDATION, "cache_view must be a list")
    entries: List[CacheEntryView] = []
    seen_keys = set()
    last_algebra: Dict[str, int] = {}
    # Entries are validated in stable (dataset, shard) order for monotonicity.
    ordered = sorted(obj, key=lambda item: (str(item.get("dataset")), int(item.get("shard_index", -1))))
    for item in ordered:
        dataset = item.get("dataset")
        if dataset not in datasets:
            raise fail(E_BAD_REFERENCE, "cache entry references unknown dataset", dataset=dataset)
        spec = datasets[dataset]
        shard_index = _non_negative_int(item.get("shard_index"), "shard_index")
        if shard_index >= spec.shard_count:
            raise fail(E_BAD_REFERENCE, "cache shard_index out of range", dataset=dataset, shard_index=shard_index)
        key = (dataset, shard_index)
        if key in seen_keys:
            raise fail(E_DUPLICATE_NAME, "duplicate cache entry", dataset=dataset, shard_index=shard_index)
        seen_keys.add(key)
        byte_size = _positive_int(item.get("byte_size"), "byte_size")
        algebra = _positive_int(item.get("algebra"), "algebra")
        last_used = _non_negative_int(item.get("last_used_clock"), "last_used_clock")
        digest = item.get("digest")
        if not isinstance(digest, str) or len(digest) != 64:
            raise fail(E_VALIDATION, "cache digest must be a 64-char hex string", dataset=dataset)

        previous = last_algebra.get(dataset)
        if previous is not None and algebra < previous:
            raise fail(E_VALIDATION, "cache algebra must be monotonic per dataset", dataset=dataset,
                        previous=previous, current=algebra)
        last_algebra[dataset] = algebra

        # Entries at the current algebra must agree byte-for-byte with the
        # deterministic derivation.  Entries at an older algebra represent a
        # superseded generation and are validated structurally only; precise
        # invalidation is decided later against the current algebra.
        current_algebra = cache_algebra(spec)
        if algebra == current_algebra:
            manifest = build_shard_manifest(spec, shard_index)
            if byte_size != manifest.byte_size:
                raise fail(
                    E_CACHE_DIGEST_MISMATCH,
                    "declared cache byte_size disagrees with deterministic shard",
                    dataset=dataset,
                    shard_index=shard_index,
                    declared=byte_size,
                    expected=manifest.byte_size,
                )
            if digest != manifest.digest:
                raise fail(
                    E_CACHE_DIGEST_MISMATCH,
                    "declared cache digest disagrees with deterministic shard",
                    dataset=dataset,
                    shard_index=shard_index,
                    declared=digest,
                    expected=manifest.digest,
                )
        elif algebra > current_algebra:
            raise fail(
                E_VALIDATION,
                "cache algebra ahead of current generation params",
                dataset=dataset,
                cache_algebra=algebra,
                expected_algebra=current_algebra,
            )
        entries.append(
            CacheEntryView(
                dataset=dataset,
                shard_index=shard_index,
                byte_size=byte_size,
                algebra=algebra,
                last_used_clock=last_used,
                digest=digest,
            )
        )
    return tuple(entries)


def _parse_clock(obj: Any) -> Tuple[ClockStep, ...]:
    if not isinstance(obj, list) or not obj:
        raise fail(E_VALIDATION, "clock_steps must be a non-empty list")
    steps: List[ClockStep] = []
    for item in obj:
        # advance_ms may be negative to inject a controlled clock rollback.
        advance = item.get("advance_ms")
        if not isinstance(advance, int) or isinstance(advance, bool):
            raise fail(E_VALIDATION, "advance_ms must be an integer", got=repr(advance))
        sleep_ms = _non_negative_int(item.get("sleep_ms", 0), "sleep_ms")
        steps.append(ClockStep(advance_ms=advance, sleep_ms=sleep_ms))
    return tuple(steps)


def _parse_injections(obj: Any, datasets: Dict[str, DatasetSpec]) -> Tuple[Injection, ...]:
    if not isinstance(obj, list):
        raise fail(E_VALIDATION, "injections must be a list")
    injections: List[Injection] = []
    for idx, item in enumerate(obj):
        stage = item.get("stage")
        if stage not in VALID_STAGES:
            raise fail(E_BAD_REFERENCE, f"unknown injection stage at index {idx}", stage=stage,
                       allowed=list(VALID_STAGES))
        kind = item.get("kind")
        if kind not in VALID_KINDS:
            raise fail(E_BAD_REFERENCE, f"unknown injection kind at index {idx}", kind=kind,
                       allowed=list(VALID_KINDS))
        target = item.get("target")
        if not isinstance(target, str) or not target:
            raise fail(E_VALIDATION, "injection target must be a non-empty string", index=idx)
        if target != "*" and "#s" in target:
            dataset, raw = target.split("#s", 1)
            if dataset not in datasets or not raw.isdigit() or int(raw) >= datasets[dataset].shard_count:
                raise fail(E_BAD_REFERENCE, "injection targets unknown shard", target=target)
        elif target != "*" and "#b" in target:
            epoch_part, rest = target.split("#b", 1)
            if not epoch_part.startswith("epoch") or not epoch_part[5:].isdigit() or not rest.isdigit():
                raise fail(E_BAD_REFERENCE, "injection batch target must look like epoch0#b0", target=target)
        elif target not in ("*",) and target not in datasets:
            raise fail(E_BAD_REFERENCE, "injection target must be a dataset, shard key or '*'", target=target)
        fire_after = _non_negative_int(item.get("fire_after", 0), "fire_after")
        options = item.get("options", {})
        if not isinstance(options, dict):
            raise fail(E_VALIDATION, "injection options must be an object", index=idx)
        injections.append(Injection(stage=stage, kind=kind, target=target, fire_after=fire_after, options=options))
    return tuple(injections)


def load_scenario(input_dir: str) -> Scenario:
    """Load and fully validate all five JSON documents under ``input_dir``."""
    datasets_obj = _read_json(os.path.join(input_dir, "datasets.json"))
    cache_obj = _read_json(os.path.join(input_dir, "cache_view.json"))
    job_obj = _read_json(os.path.join(input_dir, "job.json"))
    clock_obj = _read_json(os.path.join(input_dir, "clock_steps.json"))
    injection_obj = _read_json(os.path.join(input_dir, "injections.json"))

    datasets = _parse_datasets(datasets_obj)
    dataset_map = {spec.name: spec for spec in datasets}
    job = _parse_job(job_obj, list(dataset_map))
    cache_view = _parse_cache_view(cache_obj, dataset_map)
    clock_steps = _parse_clock(clock_obj)
    injections = _parse_injections(injection_obj, dataset_map)
    tolerance = _non_negative_int(
        clock_obj[0].get("rollback_tolerance_ms", 0) if isinstance(clock_obj[0], dict) else 0,
        "rollback_tolerance_ms",
    )

    return Scenario(
        datasets=datasets,
        cache_view=cache_view,
        job=job,
        clock_steps=clock_steps,
        injections=injections,
        clock_rollback_tolerance_ms=tolerance,
        base_dir=os.path.abspath(input_dir),
    )
