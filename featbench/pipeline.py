"""End-to-end orchestration: caching, snapshots, concurrency and fault isolation."""
from __future__ import annotations

import os

# Pin native BLAS/OpenMP to one thread before numpy/sklearn import side
# effects settle: thread-pool parallelism must not perturb numerics, so
# serial and concurrent runs stay field-for-field identical.
for _lib in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
             "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
             "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_lib, "1")
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field

from .cache import CacheLedger, CacheStore
from .combiner import build_crosses
from .config import Config, ScaleSpec
from .data import generate_dataset
from .encoder import encode_matrix
from .evaluation import evaluate
from .errors import BenchError, E_STAGE_FAILED
from .reduction import REDUCERS
from .report import assemble_report
from .snapshot import ShutdownFlag, SnapshotStore

try:
    from threadpoolctl import threadpool_limits
except ImportError:  # pragma: no cover
    threadpool_limits = None
from .strategy import compute_metrics, select_strategy


@dataclass
class TaskKey:
    sort_key: str
    scale_id: str
    path: str

    def as_dict(self) -> dict:
        return {"sort_key": self.sort_key, "scale_id": self.scale_id,
                "path": self.path}


@dataclass
class TaskOutcome:
    task: TaskKey
    ok: bool
    scale_result: dict | None = None
    failure: dict | None = None


def build_tasks(config: Config) -> list[TaskKey]:
    tasks: list[TaskKey] = []
    for scale_index, scale in enumerate(config.scales):
        for path_index, method in enumerate(config.reduction.methods):
            tasks.append(TaskKey(
                sort_key=f"{scale_index:04d}|{path_index:04d}|{scale.id}|{method}",
                scale_id=scale.id, path=method,
            ))
    return sorted(tasks, key=lambda item: item.sort_key)


def _summarize(obj) -> dict:
    if hasattr(obj, "__dataclass_fields__"):
        data = asdict(obj)
    else:
        data = obj
    summary: dict = {}
    for key, value in data.items():
        if key in {"matrix", "reduced", "codes", "target", "numeric",
                   "categorical"}:
            summary[key] = {"shape": list(getattr(obj, key).shape)}
        elif key in {"cross_columns", "vocabulary"}:
            summary[key] = f"<{type(value).__name__}>"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value
        elif isinstance(value, list) and len(value) <= 8:
            summary[key] = value
        else:
            summary[key] = f"<{type(value).__name__} len={len(getattr(value, '__len__', lambda: 0)())}>"
    return summary


def run_task(config: Config, scale: ScaleSpec, task: TaskKey,
             cache: CacheStore, snapshots: SnapshotStore,
             ledger: CacheLedger, shutdown: ShutdownFlag) -> TaskOutcome:
    try:
        return _run_task_inner(config, scale, task, cache, snapshots,
                               ledger, shutdown)
    except BenchError as exc:
        return TaskOutcome(task=task, ok=False,
                           failure=exc.to_failure(scale.id, "task",
                                                  type(exc).__name__))
    except Exception as exc:  # noqa: BLE001 - fault isolation boundary
        bench = BenchError(E_STAGE_FAILED, str(exc))
        return TaskOutcome(task=task, ok=False,
                           failure=bench.to_failure(scale.id, "task",
                                                    type(exc).__name__))


def _get_or_compute(cache: CacheStore, ledger: CacheLedger, key: str,
                    producer):
    with cache.lock_for(key):
        if cache.has(key):
            ledger.record_reuse(key)
            return cache.load(key), True
        artifact = producer()
        cache.store(key, artifact)
        ledger.record_recompute(key)
        return artifact, False


def _run_task_inner(config: Config, scale: ScaleSpec, task: TaskKey,
                    cache: CacheStore, snapshots: SnapshotStore,
                    ledger: CacheLedger,
                    shutdown: ShutdownFlag) -> TaskOutcome:
    method = task.path
    # Stage 0: deterministic dataset (regenerated each run; content fingerprint
    # feeds every downstream cache key, so scale changes invalidate downstream).
    dataset = generate_dataset(config, scale)
    snapshots.write("data", scale.id, method, cursor=dataset.samples,
                    artifact_summary={
                        "generation_fingerprint": dataset.generation_fingerprint,
                        "shape": [dataset.samples,
                                  len(dataset.columns)],
                    })

    cross_params = {"cross": config.cross.__dict__}
    cross_key = cache.stage_key("cross", scale.id, "shared",
                                [dataset.generation_fingerprint], cross_params)
    crosses, cross_reused = _get_or_compute(
        cache, ledger, cross_key,
        lambda: build_crosses(config, scale, dataset))
    snapshots.write("cross", scale.id, "shared",
                    cursor=len(crosses.cross_columns),
                    artifact_summary={
                        "combiner_fingerprint": crosses.combiner_fingerprint,
                        "active_crosses": len(crosses.cross_columns),
                        "total_raw_cardinality": crosses.total_raw_cardinality,
                        "total_final_cardinality":
                            crosses.total_final_cardinality,
                        "reused": cross_reused,
                    })
    if shutdown.requested:
        snapshots.write("cross", scale.id, "shared",
                        cursor=len(crosses.cross_columns),
                        artifact_summary={"shutdown": True,
                                          "combiner_fingerprint":
                                              crosses.combiner_fingerprint})
        raise BenchError(E_STAGE_FAILED,
                         f"graceful shutdown after cross stage for {scale.id}")

    encode_params = {"impute_missing": config.impute_missing}
    encode_key = cache.stage_key(
        "encode", scale.id, "shared",
        [dataset.generation_fingerprint, crosses.combiner_fingerprint],
        encode_params)
    encoded, encode_reused = _get_or_compute(
        cache, ledger, encode_key,
        lambda: encode_matrix(config, dataset, crosses, config.impute_missing))
    snapshots.write("encode", scale.id, "shared",
                    cursor=encoded.matrix.shape[1],
                    artifact_summary={
                        "encoder_fingerprint": encoded.encoder_fingerprint,
                        "shape": list(encoded.matrix.shape),
                        "missing_cells": encoded.missing_cells,
                        "constant_features":
                            len(encoded.constant_features),
                        "reused": encode_reused,
                    })

    metrics = compute_metrics(scale, encoded, crosses, config)
    decision = select_strategy(config, scale, metrics)

    reduce_params = {"method": method,
                     "target_dim": config.reduction.target_dim,
                     "variance_threshold":
                         config.reduction.variance_threshold,
                     "rp_seed_tag": config.reduction.rp_seed_tag}
    reduce_key = cache.stage_key(
        "reduce", scale.id, method,
        [encoded.encoder_fingerprint, config.config_fingerprint],
        reduce_params)

    def produce_reduction():
        reducer = REDUCERS[method]
        return reducer(config, encoded)

    reduced, reduce_reused = _get_or_compute(cache, ledger, reduce_key,
                                             produce_reduction)
    snapshots.write("reduce", scale.id, method,
                    cursor=reduced.retained_dim,
                    artifact_summary={
                        "reduction_fingerprint":
                            reduced.reduction_fingerprint,
                        "retained_dim": reduced.retained_dim,
                        "method": method,
                        "reused": reduce_reused,
                    })
    if shutdown.requested:
        snapshots.write("reduce", scale.id, method,
                        cursor=reduced.retained_dim,
                        artifact_summary={"shutdown": True,
                                          "reduction_fingerprint":
                                              reduced.reduction_fingerprint})
        raise BenchError(E_STAGE_FAILED,
                         f"graceful shutdown after reduce stage for "
                         f"{scale.id}/{method}")

    eval_params = {"model": "ridge", "alpha": 1.0,
                   "split": "deterministic_fifth"}
    eval_key = cache.stage_key(
        "evaluate", scale.id, method,
        [reduced.reduction_fingerprint, dataset.generation_fingerprint],
        eval_params)
    evaluation, eval_reused = _get_or_compute(
        cache, ledger, eval_key,
        lambda: evaluate(config, dataset, reduced))
    snapshots.write("evaluate", scale.id, method, cursor=1,
                    artifact_summary={
                        "evaluation_fingerprint":
                            evaluation.evaluation_fingerprint,
                        "metrics": evaluation.metrics,
                        "reused": eval_reused,
                    })

    scale_result = {
        "task": task.as_dict(),
        "scale_id": scale.id,
        "method": method,
        "metrics": evaluation.metrics,
        "retained_dim": reduced.retained_dim,
        "retained_features": reduced.retained_features,
        "reduction_summary": reduced.summary,
        "dropped_constant_features": reduced.dropped_constant_features,
        "decision": decision,
        "cross_audit": crosses.audit,
        "scale_metrics": metrics.as_dict(),
        "stage_reuse": {
            "cross": cross_reused,
            "encode": encode_reused,
            "reduce": reduce_reused,
            "evaluate": eval_reused,
        },
        "cache_keys": {
            "cross": cross_key,
            "encode": encode_key,
            "reduce": reduce_key,
            "evaluate": eval_key,
        },
    }
    return TaskOutcome(task=task, ok=True, scale_result=scale_result)


def run_pipeline(config: Config, shutdown: ShutdownFlag | None = None
                 ) -> dict:
    shutdown = shutdown or ShutdownFlag()
    cache = CacheStore(config.work_dir, config.config_fingerprint)
    snapshots = SnapshotStore(config.work_dir, config.config_fingerprint)
    ledger = CacheLedger()
    tasks = build_tasks(config)
    scale_lookup = {scale.id: scale for scale in config.scales}
    outcomes: list[TaskOutcome] = []

    limits_cm = threadpool_limits(limits=1) if threadpool_limits else _NullCm()
    with limits_cm:
        if config.workers > 1:
            with ThreadPoolExecutor(max_workers=config.workers,
                                    thread_name_prefix="bench") as executor:
                futures = {
                    executor.submit(run_task, config,
                                    scale_lookup[task.scale_id], task, cache,
                                    snapshots, ledger, shutdown): task
                    for task in tasks
                }
                for future in as_completed(futures):
                    outcomes.append(future.result())
        else:
            for task in tasks:
                outcomes.append(run_task(config, scale_lookup[task.scale_id],
                                         task, cache, snapshots, ledger,
                                         shutdown))

    outcomes.sort(key=lambda item: item.task.sort_key)
    live_keys = set()
    for outcome in outcomes:
        if outcome.ok and outcome.scale_result:
            live_keys.update(outcome.scale_result["cache_keys"].values())
    invalidated = cache.prune_orphans(live_keys)
    for key in invalidated:
        ledger.record_invalidation(key)

    scale_results = [item.scale_result for item in outcomes if item.ok]
    failures = [item.failure for item in outcomes if not item.ok]
    report = assemble_report(config, scale_results, failures, ledger,
                             snapshots)
    return report


class _NullCm:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
