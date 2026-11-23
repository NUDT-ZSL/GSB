from __future__ import annotations

import contextlib
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np

from .cache import CacheAction, ContentAddressedCache
from .canonical import sha256_obj, write_json_atomic
from .config import WorkbenchConfig
from .data import (
    dataset_hash,
    ensure_workspace as _ensure_workspace,
    load_samples,
    sample_chunks,
    sample_index,
    samples_to_matrix,
    validate_samples,
    write_samples,
)
from .errors import (
    E_CONFIG_INVALID,
    E_INCONSISTENT,
    E_LATENCY_BUDGET,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    BusinessError,
)
from .gmm import (
    GMMFit,
    GMMModel,
    EStepChunk,
    e_step_chunk,
    fit_gmm,
    responsibility_confidence,
)
from .kde import (
    density_confidence,
    kde_chunk_contributions,
    reduce_kde_chunks,
)
from .precision import PrecisionOps
from .snapshots import SnapshotStore
from .spectrum import build_error_spectrum, recompute_stage
from .standardize import (
    compute_standardization,
    transform_many,
    transform_one,
)


@dataclass
class AnalysisState:
    stats_payload: dict[str, Any]
    normalized_values: np.ndarray
    normalized_hash: str
    model_payload: dict[str, Any]
    batch_payload: dict[str, Any]
    config: WorkbenchConfig
    ops: PrecisionOps


def ensure_workspace(
    root: str | Path,
    *,
    force: bool = False,
    n_samples: int = 48,
    dimensions: int = 3,
) -> dict[str, Path]:
    return _ensure_workspace(
        root,
        force=force,
        n_samples=n_samples,
        dimensions=dimensions,
    )


def _seal(payload: dict[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    body.pop("checksum", None)
    body["checksum"] = sha256_obj(body)
    return body


def _row_checksum(row: dict[str, Any]) -> str:
    return sha256_obj(
        {
            "sample_id": row["sample_id"],
            "precision": row["precision"],
            "model_family": row["model_family"],
            "density": row["density"],
            "responsibilities": row["responsibilities"],
            "component": row["component"],
            "stats_hash": row["stats_hash"],
            "model_hash": row["model_hash"],
        }
    )


class Workbench:
    """Versioned deterministic analysis workbench."""

    def __init__(
        self,
        root: str | Path = ".",
        *,
        samples_path: str | Path | None = None,
        config_path: str | Path | None = None,
        cache_dir: str | Path | None = None,
        snapshot_dir: str | Path | None = None,
        artifact_dir: str | Path | None = None,
        config: WorkbenchConfig | dict[str, Any] | None = None,
        samples: list[dict[str, Any]] | None = None,
    ):
        self.root = Path(root).resolve()
        if samples is None or config is None:
            paths = _ensure_workspace(self.root)
        else:
            paths = {
                "samples": self.root / "samples.jsonl",
                "config": self.root / "config.json",
                "cache_dir": self.root / "cache",
                "snapshot_dir": self.root / "snapshots",
                "artifact_dir": self.root / "artifacts",
            }
            for key in ("cache_dir", "snapshot_dir", "artifact_dir"):
                paths[key].mkdir(parents=True, exist_ok=True)
        self.samples_path = Path(samples_path) if samples_path else paths["samples"]
        self.config_path = Path(config_path) if config_path else paths["config"]
        self.cache_dir = Path(cache_dir) if cache_dir else paths["cache_dir"]
        self.snapshot_dir = (
            Path(snapshot_dir) if snapshot_dir else paths["snapshot_dir"]
        )
        self.artifact_dir = (
            Path(artifact_dir) if artifact_dir else paths["artifact_dir"]
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        if config is None:
            self.config = WorkbenchConfig.from_json_file(str(self.config_path))
        elif isinstance(config, WorkbenchConfig):
            self.config = config
        else:
            self.config = WorkbenchConfig.from_dict(config)
        if samples is None:
            self.samples = load_samples(self.samples_path)
        else:
            validate_samples(samples)
            self.samples = [
                {
                    "sample_id": item["sample_id"],
                    "feature": list(item["feature"]),
                    "group": item.get("group", ""),
                }
                for item in samples
            ]
        self.sample_ids, self.groups, self.raw_features = samples_to_matrix(
            self.samples
        )
        self.dataset_hash = dataset_hash(self.samples)
        self.config_hash = self.config.semantic_hash()
        self.ops = PrecisionOps(self.config)
        self.cache = ContentAddressedCache(self.cache_dir)
        self.snapshots = SnapshotStore(self.snapshot_dir)
        self.chunks = sample_chunks(self.sample_ids, self.config.chunk_size)
        self._query_lock = threading.Lock()
        self._active_queries = 0
        self.state: AnalysisState | None = None
        self.query_index: dict[str, dict[str, Any]] = {}
        self.current_bundle: dict[str, Any] | None = None
        self.last_invalidation: dict[str, Any] | None = None
        self.last_spectrum: dict[str, Any] | None = None
        self.last_cache_actions: list[dict[str, Any]] = []
        self._load_current_snapshot_if_matching()

    def _refresh_dataset(self) -> None:
        self.sample_ids, self.groups, self.raw_features = samples_to_matrix(
            self.samples
        )
        self.dataset_hash = dataset_hash(self.samples)
        self.chunks = sample_chunks(self.sample_ids, self.config.chunk_size)
        self.state = None
        self.query_index = {}
        self.last_invalidation = None
        self.last_spectrum = None

    def reconfigure(
        self,
        config: WorkbenchConfig | dict[str, Any] | None = None,
        **changes: Any,
    ) -> "Workbench":
        if config is None:
            merged = {**self.config.to_dict(), **changes}
            new_config = WorkbenchConfig.from_dict(merged)
        elif isinstance(config, WorkbenchConfig):
            new_config = config
        else:
            new_config = WorkbenchConfig.from_dict(config)
        self.config = new_config
        self.config_hash = new_config.semantic_hash()
        self.ops = PrecisionOps(new_config)
        self.chunks = sample_chunks(self.sample_ids, new_config.chunk_size)
        self.state = None
        self.query_index = {}
        self.last_invalidation = None
        self.last_spectrum = None
        return self

    def reload_samples(self) -> "Workbench":
        self.samples = load_samples(self.samples_path)
        self._refresh_dataset()
        return self

    def append_samples(
        self,
        records: list[dict[str, Any]],
        *,
        persist: bool = True,
    ) -> "Workbench":
        merged = [dict(item) for item in self.samples] + [
            {
                "sample_id": item["sample_id"],
                "feature": list(item["feature"]),
                "group": item.get("group", ""),
            }
            for item in records
        ]
        validate_samples(merged)
        self.samples = merged
        if persist:
            write_samples(self.samples_path, merged)
        self._refresh_dataset()
        return self

    def upsert_sample(
        self,
        record: dict[str, Any],
        *,
        persist: bool = True,
    ) -> "Workbench":
        candidate = {
            "sample_id": record["sample_id"],
            "feature": list(record["feature"]),
            "group": record.get("group", ""),
        }
        replaced = False
        merged: list[dict[str, Any]] = []
        for item in self.samples:
            if item["sample_id"] == candidate["sample_id"]:
                merged.append(candidate)
                replaced = True
            else:
                merged.append(item)
        if not replaced:
            merged.append(candidate)
        validate_samples(merged)
        self.samples = merged
        if persist:
            write_samples(self.samples_path, merged)
        self._refresh_dataset()
        return self

    # ------------------------------------------------------------------
    # Snapshot and state helpers
    # ------------------------------------------------------------------
    def _load_current_snapshot_if_matching(self) -> None:
        bundle = self.snapshots.load_bundle()
        if bundle is None:
            return
        payloads = {
            category: record["payload"]
            for category, record in bundle["categories"].items()
        }
        batch = payloads["batch"]
        if (
            batch.get("dataset_hash") == self.dataset_hash
            and batch.get("config_hash") == self.config_hash
        ):
            self._hydrate(bundle)
            self.last_invalidation = {
                "all_samples": list(self.sample_ids),
                "affected_samples": list(self.sample_ids),
                "recomputed_samples": [],
                "reused_samples": list(self.sample_ids),
                "changed_samples": [],
                "removed_samples": [],
                "stage_reuse": {},
                "snapshot_action": "reused",
                "manifest": self.snapshots.status(),
                "cache": {"hit_count": 0, "miss_count": 0, "actions": []},
                "reason": "restart_snapshot_match",
            }
        else:
            self.current_bundle = bundle

    def _hydrate(self, bundle: dict[str, Any]) -> None:
        payloads = {
            category: record["payload"]
            for category, record in bundle["categories"].items()
        }
        stats_payload = payloads["statistics"]
        model_payload = payloads["model"]
        batch_payload = payloads["batch"]
        expected = batch_payload.get("checksum")
        body = dict(batch_payload)
        body.pop("checksum", None)
        if expected != sha256_obj(body):
            raise BusinessError(
                code=E_SNAPSHOT_CORRUPT,
                message="batch snapshot payload checksum mismatch",
                details={
                    "category": "batch",
                    "version": bundle["bundle_version"],
                    "expected_checksum": expected,
                    "actual_checksum": sha256_obj(body),
                },
            )
        normalized_values = transform_many(
            self.raw_features, stats_payload["stats"], self.ops
        )
        normalized_hash = sha256_obj(
            {
                "stats_hash": stats_payload["stats_hash"],
                "values": normalized_values.tolist(),
                "precision": self.ops.metadata.to_dict(),
            }
        )
        self.state = AnalysisState(
            stats_payload=stats_payload,
            normalized_values=normalized_values,
            normalized_hash=normalized_hash,
            model_payload=model_payload,
            batch_payload=batch_payload,
            config=self.config,
            ops=self.ops,
        )
        self.query_index = {
            row["sample_id"]: row for row in batch_payload["rows"]
        }
        self.current_bundle = bundle

    def _state_matches_current(self) -> bool:
        if self.state is None:
            return False
        batch = self.state.batch_payload
        return (
            batch.get("dataset_hash") == self.dataset_hash
            and batch.get("config_hash") == self.config_hash
        )

    def _ensure_analysis(self) -> None:
        if self._state_matches_current():
            return
        bundle = self.snapshots.load_bundle()
        if bundle is not None:
            batch = bundle["categories"]["batch"]["payload"]
            if (
                batch.get("dataset_hash") == self.dataset_hash
                and batch.get("config_hash") == self.config_hash
            ):
                self._hydrate(bundle)
                self.last_invalidation = {
                    "all_samples": list(self.sample_ids),
                    "affected_samples": list(self.sample_ids),
                    "recomputed_samples": [],
                    "reused_samples": list(self.sample_ids),
                    "changed_samples": [],
                    "snapshot_action": "reused",
                    "manifest": self.snapshots.status(),
                    "cache": {"hit_count": 0, "miss_count": 0, "actions": []},
                    "reason": "restart_snapshot_match",
                }
                return
        self.analyze()

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------
    def _check_batch_resources(self) -> None:
        n_samples = len(self.sample_ids)
        if n_samples > self.config.max_batch_samples:
            raise BusinessError(
                code=E_RESOURCE_LIMIT,
                message="batch sample count exceeds configured limit",
                details={
                    "category": "max_batch_samples",
                    "limit": self.config.max_batch_samples,
                    "observed": n_samples,
                },
            )
        if self.config.model_family == "gmm":
            bytes_needed = (
                n_samples
                * self.config.n_components
                * np.dtype(np.float64).itemsize
            )
            limit_bytes = (
                self.config.max_responsibility_memory_mb * 1024.0 * 1024.0
            )
            if bytes_needed > limit_bytes:
                raise BusinessError(
                    code=E_RESOURCE_LIMIT,
                    message="responsibility table exceeds memory limit",
                    details={
                        "category": "max_responsibility_memory_mb",
                        "limit_mb": self.config.max_responsibility_memory_mb,
                        "observed_mb": bytes_needed / (1024.0 * 1024.0),
                        "estimated_elements": n_samples
                        * self.config.n_components,
                    },
                )

    @contextlib.contextmanager
    def _query_slot(self) -> Iterator[None]:
        with self._query_lock:
            if self._active_queries >= self.config.max_parallel_queries:
                raise BusinessError(
                    code=E_RESOURCE_LIMIT,
                    message="parallel query limit reached",
                    details={
                        "category": "max_parallel_queries",
                        "limit": self.config.max_parallel_queries,
                        "active": self._active_queries,
                    },
                )
            self._active_queries += 1
        try:
            yield
        finally:
            with self._query_lock:
                self._active_queries -= 1

    # ------------------------------------------------------------------
    # Analysis pipeline
    # ------------------------------------------------------------------
    def _cache_call(
        self,
        *,
        stage: str,
        dependencies: dict[str, Any],
        builder: Callable[[], Any],
        sample_ids: list[str],
        artifact: str,
        force: bool,
    ) -> tuple[Any, CacheAction]:
        key = self.cache.make_key(stage, dependencies)
        return self.cache.get_or_compute(
            key,
            builder,
            stage=stage,
            dependencies=dependencies,
            sample_ids=sample_ids,
            artifact=artifact,
            force=force,
        )

    def _compute_analysis(
        self,
        config: WorkbenchConfig,
        *,
        use_cache: bool,
        force: bool = False,
    ) -> AnalysisState:
        ops = PrecisionOps(config)
        common_dependencies = {
            "sample_hash": self.dataset_hash,
            "chunk_size": config.chunk_size,
            "precision": ops.metadata.to_dict(),
            "seed": config.seed,
            "config_hash": config.semantic_hash(),
        }
        all_ids = list(self.sample_ids)
        cache = self.cache if use_cache else None

        if cache is None:
            standard = compute_standardization(self.raw_features, config, ops)
            normalized_values = standard.values
            stats_hash = standard.stats_hash
            stats_payload = {
                "stats": standard.stats,
                "stats_hash": standard.stats_hash,
                "values_hash": standard.values_hash,
                "sample_hash": self.dataset_hash,
                "precision": ops.metadata.to_dict(),
            }
            normalized_hash = standard.values_hash
        else:
            stats_dependencies = {
                **common_dependencies,
                "normalization": config.normalization,
                "variance_floor": config.variance_floor,
                "upstream": {"sample_hash": self.dataset_hash},
            }

            def build_stats() -> dict[str, Any]:
                standard_local = compute_standardization(
                    self.raw_features, config, ops
                )
                return {
                    "stats": standard_local.stats,
                    "stats_hash": standard_local.stats_hash,
                    "values_hash": standard_local.values_hash,
                    "sample_hash": self.dataset_hash,
                    "precision": ops.metadata.to_dict(),
                }

            stats_payload, _ = self._cache_call(
                stage="standardization_stats",
                dependencies=stats_dependencies,
                builder=build_stats,
                sample_ids=all_ids,
                artifact="standardization_statistics",
                force=force,
            )
            stats_hash = str(stats_payload["stats_hash"])
            normalized_dependencies = {
                **common_dependencies,
                "upstream": {"stats_hash": stats_hash},
                "stats_hash": stats_hash,
            }

            def build_normalized() -> dict[str, Any]:
                values = transform_many(
                    self.raw_features, stats_payload["stats"], ops
                )
                return {
                    "values": values.tolist(),
                    "stats_hash": stats_hash,
                    "values_hash": sha256_obj(
                        {
                            "stats_hash": stats_hash,
                            "values": values.tolist(),
                            "precision": ops.metadata.to_dict(),
                        }
                    ),
                }

            normalized_payload, _ = self._cache_call(
                stage="standardized_values",
                dependencies=normalized_dependencies,
                builder=build_normalized,
                sample_ids=all_ids,
                artifact="standardized_values",
                force=force,
            )
            normalized_values = ops.cast(
                np.asarray(normalized_payload["values"], dtype=np.float64)
            )
            normalized_hash = str(normalized_payload["values_hash"])

        model_dependencies = {
            **common_dependencies,
            "stats_hash": stats_hash,
            "normalized_hash": normalized_hash,
            "upstream": {
                "sample_hash": self.dataset_hash,
                "stats_hash": stats_hash,
                "normalized_hash": normalized_hash,
            },
        }

        if config.model_family == "gmm":
            if cache is None:
                fit = fit_gmm(
                    normalized_values,
                    config,
                    ops,
                    chunk_bounds=[
                        (chunk["start"], chunk["stop"])
                        for chunk in sample_chunks(all_ids, config.chunk_size)
                    ],
                )
                model_payload = self._fit_to_payload(fit)
            else:

                def build_gmm_model() -> dict[str, Any]:
                    fit_local = fit_gmm(
                        normalized_values,
                        config,
                        ops,
                        chunk_bounds=[
                            (chunk["start"], chunk["stop"])
                            for chunk in sample_chunks(all_ids, config.chunk_size)
                        ],
                        e_step_callback=self._make_e_step_cache_callback(
                            config, ops, stats_hash, normalized_hash, force
                        ),
                    )
                    return self._fit_to_payload(fit_local)

                model_payload, _ = self._cache_call(
                    stage="gmm_fit",
                    dependencies=model_dependencies,
                    builder=build_gmm_model,
                    sample_ids=all_ids,
                    artifact="gmm_model_and_trace",
                    force=force,
                )
            fit_arrays = self._fit_arrays_from_payload(model_payload)
            model_hash = str(model_payload["model_hash"])
        else:
            model_payload = {
                "kind": "kde",
                "bandwidth": float(config.bandwidth),
                "precision": ops.metadata.to_dict(),
            }
            model_hash = sha256_obj(model_payload)
            model_payload = {**model_payload, "model_hash": model_hash}
            fit_arrays = {}

        batch_dependencies = {
            **common_dependencies,
            "stats_hash": stats_hash,
            "normalized_hash": normalized_hash,
            "model_hash": model_hash,
            "upstream": {
                "sample_hash": self.dataset_hash,
                "stats_hash": stats_hash,
                "normalized_hash": normalized_hash,
                "model_hash": model_hash,
            },
        }

        if cache is None:
            batch_payload = self._build_batch_payload(
                config=config,
                ops=ops,
                normalized_values=normalized_values,
                stats_hash=stats_hash,
                normalized_hash=normalized_hash,
                model_payload=model_payload,
                model_hash=model_hash,
                fit_arrays=fit_arrays,
                use_cache=False,
                force=force,
            )
        else:
            batch_payload, _ = self._cache_call(
                stage="batch_result",
                dependencies=batch_dependencies,
                builder=lambda: self._build_batch_payload(
                    config=config,
                    ops=ops,
                    normalized_values=normalized_values,
                    stats_hash=stats_hash,
                    normalized_hash=normalized_hash,
                    model_payload=model_payload,
                    model_hash=model_hash,
                    fit_arrays=fit_arrays,
                    use_cache=True,
                    force=force,
                ),
                sample_ids=all_ids,
                artifact="batch_results",
                force=force,
            )
        return AnalysisState(
            stats_payload=stats_payload,
            normalized_values=normalized_values,
            normalized_hash=normalized_hash,
            model_payload=model_payload,
            batch_payload=batch_payload,
            config=config,
            ops=ops,
        )

    def _make_e_step_cache_callback(
        self,
        config: WorkbenchConfig,
        ops: PrecisionOps,
        stats_hash: str,
        normalized_hash: str,
        force: bool,
    ) -> Callable[[int, int, np.ndarray, GMMModel], EStepChunk]:
        def callback(
            iteration: int,
            chunk_number: int,
            chunk_values: np.ndarray,
            model: GMMModel,
        ) -> EStepChunk:
            start = chunk_number * config.chunk_size
            stop = start + chunk_values.shape[0]
            chunk = self.chunks[chunk_number]
            dependencies = {
                "sample_hash": self.dataset_hash,
                "chunk_id": chunk_number,
                "chunk_bounds": [start, stop],
                "chunk_sample_hash": chunk["sample_hash"],
                "stats_hash": stats_hash,
                "normalized_hash": normalized_hash,
                "model_hash": model.payload_hash,
                "chunk_size": config.chunk_size,
                "precision": ops.metadata.to_dict(),
                "seed": config.seed,
                "upstream": {
                    "stats_hash": stats_hash,
                    "normalized_hash": normalized_hash,
                    "model_hash": model.payload_hash,
                },
            }

            def build() -> dict[str, Any]:
                result = e_step_chunk(chunk_values, model, config, ops)
                return {
                    "responsibilities": result.responsibilities.tolist(),
                    "log_probabilities": result.log_probabilities.tolist(),
                    "log_norm": result.log_norm.tolist(),
                    "density": result.density.tolist(),
                    "log_likelihood": result.log_likelihood,
                    "component_evaluations": result.component_evaluations,
                }

            payload, _ = self._cache_call(
                stage="gmm_responsibility_chunk",
                dependencies=dependencies,
                builder=build,
                sample_ids=chunk["sample_ids"],
                artifact="responsibility_chunk",
                force=force,
            )
            return EStepChunk(
                responsibilities=ops.cast(
                    np.asarray(payload["responsibilities"], dtype=np.float64)
                ),
                log_probabilities=ops.cast(
                    np.asarray(payload["log_probabilities"], dtype=np.float64)
                ),
                log_norm=ops.cast(
                    np.asarray(payload["log_norm"], dtype=np.float64)
                ),
                density=ops.cast(np.asarray(payload["density"], dtype=np.float64)),
                log_likelihood=float(payload["log_likelihood"]),
                component_evaluations=int(payload["component_evaluations"]),
            )

        return callback

    @staticmethod
    def _fit_to_payload(fit: GMMFit) -> dict[str, Any]:
        return {
            "kind": "gmm",
            "model": fit.model.to_payload(),
            "model_hash": fit.model.payload_hash,
            "responsibilities": fit.responsibilities.tolist(),
            "log_probabilities": fit.log_probabilities.tolist(),
            "log_norm": fit.log_norm.tolist(),
            "densities": fit.densities.tolist(),
            "log_likelihood": float(fit.log_likelihood),
            "trace": fit.trace,
            "convergence": fit.convergence,
            "protected_rounds": fit.protected_rounds,
            "skipped_records": fit.skipped_records,
            "chunk_summaries": fit.chunk_summaries,
        }

    @staticmethod
    def _fit_arrays_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "responsibilities": np.asarray(
                payload["responsibilities"], dtype=np.float64
            ),
            "log_probabilities": np.asarray(
                payload["log_probabilities"], dtype=np.float64
            ),
            "log_norm": np.asarray(payload["log_norm"], dtype=np.float64),
            "densities": np.asarray(payload["densities"], dtype=np.float64),
        }

    def _build_batch_payload(
        self,
        *,
        config: WorkbenchConfig,
        ops: PrecisionOps,
        normalized_values: np.ndarray,
        stats_hash: str,
        normalized_hash: str,
        model_payload: dict[str, Any],
        model_hash: str,
        fit_arrays: dict[str, Any],
        use_cache: bool,
        force: bool,
    ) -> dict[str, Any]:
        chunk_bounds = [
            [chunk["start"], chunk["stop"]]
            for chunk in sample_chunks(self.sample_ids, config.chunk_size)
        ]
        if config.model_family == "gmm":
            rows, extra = self._build_gmm_rows(
                config,
                ops,
                normalized_values,
                stats_hash,
                model_hash,
                fit_arrays,
            )
            extra["ll_trace"] = model_payload.get("trace", [])
            extra["convergence"] = model_payload.get("convergence", {})
            extra["protected_rounds"] = model_payload.get(
                "protected_rounds", []
            )
            extra["skipped_records"] = model_payload.get(
                "skipped_records", []
            )
        else:
            rows, extra = self._build_kde_rows(
                config,
                ops,
                normalized_values,
                stats_hash,
                normalized_hash,
                model_hash,
                use_cache=use_cache,
                force=force,
            )
        payload = {
            "schema_version": 1,
            "dataset_hash": self.dataset_hash,
            "config_hash": config.semantic_hash(),
            "semantic_config": config.semantic_dict(),
            "precision": ops.metadata.to_dict(),
            "chunk_size": config.chunk_size,
            "seed": config.seed,
            "stats_hash": stats_hash,
            "normalized_hash": normalized_hash,
            "model_hash": model_hash,
            "chunk_boundaries": chunk_bounds,
            "chunk_ids": [chunk["chunk_id"] for chunk in self.chunks],
            "rows": rows,
            **extra,
        }
        return _seal(payload)

    def _build_gmm_rows(
        self,
        config: WorkbenchConfig,
        ops: PrecisionOps,
        normalized_values: np.ndarray,
        stats_hash: str,
        model_hash: str,
        fit_arrays: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        responsibilities = np.asarray(
            fit_arrays["responsibilities"], dtype=np.float64
        )
        log_probabilities = np.asarray(
            fit_arrays["log_probabilities"], dtype=np.float64
        )
        log_norm = np.asarray(fit_arrays["log_norm"], dtype=np.float64)
        densities = np.asarray(fit_arrays["densities"], dtype=np.float64)
        rows: list[dict[str, Any]] = []
        for index, sample_id in enumerate(self.sample_ids):
            responsibility = responsibilities[index]
            confidence = responsibility_confidence(responsibility)
            component = int(np.argmax(responsibility))
            row = {
                "sample_id": sample_id,
                "group": self.groups[index],
                "feature": self.samples[index]["feature"],
                "standardized_feature": normalized_values[index].tolist(),
                "precision": ops.mode,
                "model_family": "gmm",
                "density": float(densities[index]),
                "log_density": float(log_norm[index]),
                "responsibilities": [float(value) for value in responsibility],
                "component": component,
                "confidence": confidence,
                "stats_hash": stats_hash,
                "model_hash": model_hash,
                "chunk_ids": [index // config.chunk_size],
                "log_probabilities": [
                    float(value) for value in log_probabilities[index]
                ],
                "log_norm": float(log_norm[index]),
            }
            row["result_checksum"] = _row_checksum(row)
            rows.append(row)
        extra = {
            "model_family": "gmm",
            "n_components": config.n_components,
            "covariance_type": config.covariance_type,
            "ll_trace": [],
            "convergence": {},
            "protected_rounds": [],
            "skipped_records": [],
        }
        return rows, extra

    def _build_kde_rows(
        self,
        config: WorkbenchConfig,
        ops: PrecisionOps,
        normalized_values: np.ndarray,
        stats_hash: str,
        normalized_hash: str,
        model_hash: str,
        *,
        use_cache: bool,
        force: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        source_chunks = sample_chunks(self.sample_ids, config.chunk_size)
        contributions: list[np.ndarray] = []
        evaluation_count = 0
        for chunk in source_chunks:
            sources = normalized_values[chunk["start"] : chunk["stop"]]
            if use_cache:
                dependencies = {
                    "sample_hash": self.dataset_hash,
                    "chunk_id": chunk["chunk_id"],
                    "chunk_sample_hash": chunk["sample_hash"],
                    "normalized_hash": normalized_hash,
                    "stats_hash": stats_hash,
                    "bandwidth": config.bandwidth,
                    "chunk_size": config.chunk_size,
                    "precision": ops.metadata.to_dict(),
                    "seed": config.seed,
                    "upstream": {
                        "normalized_hash": normalized_hash,
                        "stats_hash": stats_hash,
                    },
                }

                def build() -> dict[str, Any]:
                    result = kde_chunk_contributions(
                        normalized_values, sources, config, ops
                    )
                    return {
                        "raw_sum": result.raw_sum.tolist(),
                        "kernel_evaluations": result.kernel_evaluations,
                    }

                payload, _ = self._cache_call(
                    stage="kde_density_sum_chunk",
                    dependencies=dependencies,
                    builder=build,
                    sample_ids=chunk["sample_ids"],
                    artifact="density_chunk_contributions",
                    force=force,
                )
                contribution = ops.cast(
                    np.asarray(payload["raw_sum"], dtype=np.float64)
                )
                evaluation_count += int(payload["kernel_evaluations"])
            else:
                result = kde_chunk_contributions(
                    normalized_values, sources, config, ops
                )
                contribution = result.raw_sum
                evaluation_count += result.kernel_evaluations
            contributions.append(contribution)
        raw_sum, densities = reduce_kde_chunks(
            contributions,
            len(self.sample_ids),
            self.raw_features.shape[1],
            config.bandwidth,
            ops,
        )
        rows: list[dict[str, Any]] = []
        for index, sample_id in enumerate(self.sample_ids):
            row = {
                "sample_id": sample_id,
                "group": self.groups[index],
                "feature": self.samples[index]["feature"],
                "standardized_feature": normalized_values[index].tolist(),
                "precision": ops.mode,
                "model_family": "kde",
                "density": float(densities[index]),
                "log_density": float(
                    density_confidence(
                        float(densities[index]),
                        config.bandwidth,
                        len(self.sample_ids),
                        self.raw_features.shape[1],
                    )["log_density"]
                ),
                "responsibilities": [],
                "component": None,
                "confidence": density_confidence(
                    float(densities[index]),
                    config.bandwidth,
                    len(self.sample_ids),
                    self.raw_features.shape[1],
                ),
                "stats_hash": stats_hash,
                "model_hash": model_hash,
                "chunk_ids": [chunk["chunk_id"] for chunk in source_chunks],
                "raw_chunk_sum": float(raw_sum[index]),
                "chunk_contributions": [
                    float(value[index]) for value in contributions
                ],
            }
            row["result_checksum"] = _row_checksum(row)
            rows.append(row)
        extra = {
            "model_family": "kde",
            "bandwidth": config.bandwidth,
            "kernel_evaluations": evaluation_count,
            "ll_trace": [],
            "convergence": {},
            "protected_rounds": [],
            "skipped_records": [],
        }
        return rows, extra

    def _old_snapshot_rows(self) -> dict[str, dict[str, Any]]:
        try:
            bundle = self.snapshots.load_bundle()
        except BusinessError:
            raise
        if bundle is None:
            return {}
        batch = bundle["categories"]["batch"]["payload"]
        return {row["sample_id"]: row for row in batch.get("rows", [])}

    def _invalidation_report(
        self,
        *,
        old_rows: dict[str, dict[str, Any]],
        new_state: AnalysisState,
        batch_action: CacheAction | None,
        snapshot_action: str,
        manifest: dict[str, Any] | None,
    ) -> dict[str, Any]:
        all_ids = list(self.sample_ids)
        if batch_action is not None and batch_action.hit:
            recomputed: list[str] = []
            reused = list(all_ids)
        else:
            recomputed = list(all_ids)
            reused = []
        changed: list[str] = []
        new_rows = {
            row["sample_id"]: row for row in new_state.batch_payload["rows"]
        }
        for sample_id in all_ids:
            old = old_rows.get(sample_id)
            new = new_rows.get(sample_id)
            if old is None or new is None:
                changed.append(sample_id)
            elif old.get("result_checksum") != new.get("result_checksum"):
                changed.append(sample_id)
        removed = sorted(set(old_rows) - set(all_ids))
        stage_reuse: dict[str, list[str]] = {}
        for action in self.cache.actions:
            if action.hit:
                stage_reuse.setdefault(action.stage, [])
                for sample_id in action.sample_ids:
                    if sample_id not in stage_reuse[action.stage]:
                        stage_reuse[action.stage].append(sample_id)
        affected = sorted(set(recomputed) | set(reused))
        return {
            "all_samples": all_ids,
            "affected_samples": affected,
            "recomputed_samples": sorted(recomputed),
            "reused_samples": sorted(reused),
            "changed_samples": sorted(changed),
            "removed_samples": removed,
            "stage_reuse": stage_reuse,
            "snapshot_action": snapshot_action,
            "manifest": manifest,
            "cache": self.cache.action_report(),
        }

    def analyze(self, *, force: bool = False) -> dict[str, Any]:
        self._check_batch_resources()
        old_rows = self._old_snapshot_rows()
        self.cache.reset_actions()
        new_state = self._compute_analysis(
            self.config, use_cache=True, force=force
        )
        self.state = new_state
        self.query_index = {
            row["sample_id"]: row for row in new_state.batch_payload["rows"]
        }
        batch_action = next(
            (
                action
                for action in self.cache.actions
                if action.stage == "batch_result"
            ),
            None,
        )
        current = self.snapshots.load_bundle()
        same_current = False
        if current is not None:
            same_current = all(
                record["checksum"] == sha256_obj(payload)
                for category, payload in {
                    "statistics": new_state.stats_payload,
                    "model": new_state.model_payload,
                    "batch": new_state.batch_payload,
                }.items()
                for record in [current["categories"][category]]
            )
        if same_current:
            manifest = self.snapshots.status()
            snapshot_action = "reused"
            self.current_bundle = current
        else:
            manifest = self.snapshots.commit_bundle(
                {
                    "statistics": new_state.stats_payload,
                    "model": new_state.model_payload,
                    "batch": new_state.batch_payload,
                }
            )
            snapshot_action = "committed"
            self.current_bundle = self.snapshots.load_bundle()
        self.last_invalidation = self._invalidation_report(
            old_rows=old_rows,
            new_state=new_state,
            batch_action=batch_action,
            snapshot_action=snapshot_action,
            manifest=manifest,
        )
        self.last_cache_actions = self.cache.action_report()["actions"]
        self._write_artifacts()
        return {
            "ok": True,
            "dataset_hash": self.dataset_hash,
            "config_hash": self.config_hash,
            "sample_count": len(self.sample_ids),
            "chunk_count": len(self.chunks),
            "precision": self.config.precision,
            "model_family": self.config.model_family,
            "batch_checksum": new_state.batch_payload["checksum"],
            "snapshot": manifest,
            "invalidation": self.last_invalidation,
            "cache": self.cache.action_report(),
        }

    # ------------------------------------------------------------------
    # Query path
    # ------------------------------------------------------------------
    def query(
        self,
        sample_id: str,
        *,
        latency_budget_ms: float | None = None,
    ) -> dict[str, Any]:
        query_start_ns = time.perf_counter_ns()
        with self._query_slot():
            self._ensure_analysis()
            if self.config.model_family == "gmm":
                return self._query_gmm(
                    sample_id, latency_budget_ms, query_start_ns
                )
            return self._query_kde(
                sample_id, latency_budget_ms, query_start_ns
            )

    query_sample = query

    def _latency_failure(
        self,
        *,
        start_ns: int,
        budget_ms: float,
        completed_chunks: int,
        total_chunks: int,
        estimate: dict[str, Any],
        partial: dict[str, Any],
    ) -> None:
        elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        if elapsed_ms <= budget_ms:
            return
        raise BusinessError(
            code=E_LATENCY_BUDGET,
            message="interactive query exceeded its latency budget",
            details={
                "sample_id": partial.get("sample_id"),
                "latency_budget_ms": budget_ms,
                "consumed_budget_ms": elapsed_ms,
                "completed_chunks": completed_chunks,
                "total_chunks": total_chunks,
                "completed_chunk_ids": partial.get("completed_chunk_ids", []),
                "estimate": estimate,
                "partial_result": partial,
                "timing_source": (
                    "time.perf_counter_ns around actual query path "
                    "(state resolution and compute loops)"
                ),
            },
        )

    def _query_gmm(
        self,
        sample_id: str,
        latency_budget_ms: float | None,
        start_ns: int,
    ) -> dict[str, Any]:
        budget_ms = float(
            self.config.latency_budget_ms
            if latency_budget_ms is None
            else latency_budget_ms
        )
        if not math.isfinite(budget_ms) or budget_ms <= 0.0:
            raise BusinessError(
                code=E_CONFIG_INVALID,
                message="latency_budget_ms must be a positive finite number",
                details={"field": "latency_budget_ms", "value": budget_ms},
            )
        index = sample_index(self.sample_ids, sample_id)
        batch_row = self.query_index[sample_id]
        chunk_id = index // self.config.chunk_size
        chunk = self.chunks[chunk_id]
        start, stop = chunk["start"], chunk["stop"]
        model_payload = self.state.model_payload
        model = GMMModel.from_payload(model_payload["model"])
        query_values = transform_many(
            self.raw_features[start:stop], self.state.stats_payload["stats"], self.ops
        )
        estimate = {
            "sample_count": stop - start,
            "chunk_count": 1,
            "chunk_size": self.config.chunk_size,
            "kernel_evaluations": (stop - start)
            * self.config.n_components,
            "gaussian_component_evaluations": (stop - start)
            * self.config.n_components,
        }
        partial: dict[str, Any] = {
            "sample_id": sample_id,
            "completed_chunk_ids": [],
        }
        result = e_step_chunk(query_values, model, self.config, self.ops)
        partial["completed_chunk_ids"] = [chunk_id]
        self._latency_failure(
            start_ns=start_ns,
            budget_ms=budget_ms,
            completed_chunks=1,
            total_chunks=1,
            estimate=estimate,
            partial=partial,
        )
        position = index - start
        responsibilities = result.responsibilities[position]
        density = float(result.density[position])
        component = int(np.argmax(responsibilities))
        confidence = responsibility_confidence(responsibilities)
        consistency = self._assert_consistency(
            sample_id=sample_id,
            query={
                "responsibilities": responsibilities.tolist(),
                "component": component,
                "density": density,
                "log_norm": float(result.log_norm[position]),
                "log_probabilities": result.log_probabilities[position].tolist(),
                "chunk_ids": [chunk_id],
            },
            batch_row=batch_row,
            query_chunk_ids=[chunk_id],
            batch_index=index,
        )
        elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        estimate["measured_elapsed_ms_precheck"] = elapsed_ms
        self._latency_failure(
            start_ns=start_ns,
            budget_ms=budget_ms,
            completed_chunks=1,
            total_chunks=1,
            estimate=estimate,
            partial={
                **partial,
                "responsibilities": responsibilities.tolist(),
                "density": density,
            },
        )
        elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        query_checksum = _row_checksum(
            {
                "sample_id": sample_id,
                "precision": self.config.precision,
                "model_family": "gmm",
                "density": density,
                "responsibilities": responsibilities.tolist(),
                "component": component,
                "stats_hash": self.state.stats_payload["stats_hash"],
                "model_hash": self.state.model_payload["model_hash"],
            }
        )
        return {
            "ok": True,
            "sample_id": sample_id,
            "group": self.groups[index],
            "model_family": "gmm",
            "precision": self.config.precision,
            "responsibilities": responsibilities.tolist(),
            "component": component,
            "density": density,
            "log_density": float(result.log_norm[position]),
            "confidence_summary": confidence,
            "stats_hash": self.state.stats_payload["stats_hash"],
            "model_hash": self.state.model_payload["model_hash"],
            "result_checksum": query_checksum,
            "consistency": consistency,
            "latency": {
                "elapsed_ms": elapsed_ms,
                "budget_ms": budget_ms,
                "within_budget": elapsed_ms <= budget_ms,
                "timing_source": "time.perf_counter_ns",
            },
            "estimate": estimate,
        }

    def _query_kde(
        self,
        sample_id: str,
        latency_budget_ms: float | None,
        start_ns: int,
    ) -> dict[str, Any]:
        budget_ms = float(
            self.config.latency_budget_ms
            if latency_budget_ms is None
            else latency_budget_ms
        )
        if not math.isfinite(budget_ms) or budget_ms <= 0.0:
            raise BusinessError(
                code=E_CONFIG_INVALID,
                message="latency_budget_ms must be a positive finite number",
                details={"field": "latency_budget_ms", "value": budget_ms},
            )
        index = sample_index(self.sample_ids, sample_id)
        batch_row = self.query_index[sample_id]
        target = transform_one(
            self.samples[index]["feature"],
            self.state.stats_payload["stats"],
            self.ops,
        )
        normalized_values = self.state.normalized_values
        source_chunks = sample_chunks(self.sample_ids, self.config.chunk_size)
        contributions: list[np.ndarray] = []
        completed_ids: list[int] = []
        estimate = {
            "sample_count": len(self.sample_ids),
            "chunk_count": len(source_chunks),
            "chunk_size": self.config.chunk_size,
            "kernel_evaluations": 0,
            "gaussian_component_evaluations": 0,
        }
        for chunk in source_chunks:
            sources = normalized_values[chunk["start"] : chunk["stop"]]
            result = kde_chunk_contributions(
                target, sources, self.config, self.ops
            )
            contributions.append(result.raw_sum[0])
            estimate["kernel_evaluations"] += int(result.kernel_evaluations)
            completed_ids.append(chunk["chunk_id"])
            partial = {
                "sample_id": sample_id,
                "completed_chunk_ids": list(completed_ids),
                "raw_chunk_sum_so_far": float(
                    self.ops.sum(np.asarray(contributions))
                ),
            }
            self._latency_failure(
                start_ns=start_ns,
                budget_ms=budget_ms,
                completed_chunks=len(completed_ids),
                total_chunks=len(source_chunks),
                estimate=estimate,
                partial=partial,
            )
        raw_sum_array, density_array = reduce_kde_chunks(
            contributions,
            len(self.sample_ids),
            self.raw_features.shape[1],
            self.config.bandwidth,
            self.ops,
        )
        density = float(density_array[0])
        consistency = self._assert_consistency(
            sample_id=sample_id,
            query={
                "responsibilities": [],
                "component": None,
                "density": density,
                "raw_chunk_sum": float(raw_sum_array[0]),
                "chunk_contributions": [
                    float(value) for value in contributions
                ],
                "chunk_ids": [chunk["chunk_id"] for chunk in source_chunks],
            },
            batch_row=batch_row,
            query_chunk_ids=[chunk["chunk_id"] for chunk in source_chunks],
            batch_index=index,
        )
        confidence = density_confidence(
            density,
            self.config.bandwidth,
            len(self.sample_ids),
            self.raw_features.shape[1],
        )
        self._latency_failure(
            start_ns=start_ns,
            budget_ms=budget_ms,
            completed_chunks=len(completed_ids),
            total_chunks=len(source_chunks),
            estimate=estimate,
            partial={
                "sample_id": sample_id,
                "completed_chunk_ids": completed_ids,
                "density": density,
                "raw_chunk_sum": float(raw_sum_array[0]),
            },
        )
        elapsed_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
        query_checksum = _row_checksum(
            {
                "sample_id": sample_id,
                "precision": self.config.precision,
                "model_family": "kde",
                "density": density,
                "responsibilities": [],
                "component": None,
                "stats_hash": self.state.stats_payload["stats_hash"],
                "model_hash": self.state.model_payload["model_hash"],
            }
        )
        return {
            "ok": True,
            "sample_id": sample_id,
            "group": self.groups[index],
            "model_family": "kde",
            "precision": self.config.precision,
            "responsibilities": [],
            "component": None,
            "density": density,
            "log_density": confidence["log_density"],
            "confidence_summary": confidence,
            "stats_hash": self.state.stats_payload["stats_hash"],
            "model_hash": self.state.model_payload["model_hash"],
            "result_checksum": query_checksum,
            "consistency": consistency,
            "latency": {
                "elapsed_ms": elapsed_ms,
                "budget_ms": budget_ms,
                "within_budget": elapsed_ms <= budget_ms,
                "timing_source": "time.perf_counter_ns",
            },
            "estimate": estimate,
        }

    def _assert_consistency(
        self,
        *,
        sample_id: str,
        query: dict[str, Any],
        batch_row: dict[str, Any],
        query_chunk_ids: list[int],
        batch_index: int,
    ) -> dict[str, Any]:
        tolerance = self.config.declared_consistency_tolerance()
        metadata_errors: list[dict[str, Any]] = []
        if self.state.stats_payload["stats_hash"] != batch_row.get("stats_hash"):
            metadata_errors.append(
                {
                    "path": "stats_hash",
                    "query": self.state.stats_payload["stats_hash"],
                    "batch": batch_row.get("stats_hash"),
                }
            )
        if self.config.precision != batch_row.get("precision"):
            metadata_errors.append(
                {
                    "path": "precision",
                    "query": self.config.precision,
                    "batch": batch_row.get("precision"),
                }
            )
        if query_chunk_ids != batch_row.get("chunk_ids"):
            metadata_errors.append(
                {
                    "path": "chunk_ids",
                    "query": query_chunk_ids,
                    "batch": batch_row.get("chunk_ids"),
                }
            )
        if metadata_errors:
            raise BusinessError(
                code=E_INCONSISTENT,
                message="query and batch metadata differ",
                details={
                    "sample_id": sample_id,
                    "first_deviation_position": 0,
                    "metadata_deviations": metadata_errors,
                    "query_path": f"query:{sample_id}",
                    "batch_path": f"batch:rows[{batch_index}]",
                    "query_intermediate": {
                        "stats_hash": self.state.stats_payload["stats_hash"],
                        "precision": self.config.precision,
                        "chunk_ids": query_chunk_ids,
                    },
                    "batch_intermediate": {
                        "stats_hash": batch_row.get("stats_hash"),
                        "precision": batch_row.get("precision"),
                        "chunk_ids": batch_row.get("chunk_ids"),
                    },
                },
            )

        comparisons: list[tuple[str, Any, Any]] = [
            ("component", query["component"], batch_row.get("component")),
            ("density", query["density"], batch_row.get("density")),
        ]
        query_resp = query.get("responsibilities", [])
        batch_resp = batch_row.get("responsibilities", [])
        if len(query_resp) != len(batch_resp):
            comparisons.append(
                ("responsibilities.length", len(query_resp), len(batch_resp))
            )
        else:
            comparisons.extend(
                (
                    f"responsibilities[{position}]",
                    value,
                    batch_resp[position],
                )
                for position, value in enumerate(query_resp)
            )
        for key in ("raw_chunk_sum", "log_norm"):
            if key in query:
                comparisons.append((key, query[key], batch_row.get(key)))
        query_chunk_contributions = query.get("chunk_contributions")
        batch_chunk_contributions = batch_row.get("chunk_contributions")
        if query_chunk_contributions is not None:
            if len(query_chunk_contributions) != len(batch_chunk_contributions or []):
                comparisons.append(
                    (
                        "chunk_contributions.length",
                        len(query_chunk_contributions),
                        len(batch_chunk_contributions or []),
                    )
                )
            else:
                comparisons.extend(
                    (
                        f"chunk_contributions[{position}]",
                        value,
                        batch_chunk_contributions[position],
                    )
                    for position, value in enumerate(query_chunk_contributions)
                )

        max_deviation = 0.0
        first: dict[str, Any] | None = None
        for path, query_value, batch_value in comparisons:
            if query_value is None and batch_value is None:
                continue
            if isinstance(query_value, (int, float)) and isinstance(
                batch_value, (int, float)
            ):
                absolute = abs(float(query_value) - float(batch_value))
                scaled_tolerance = tolerance * max(1.0, abs(float(batch_value)))
                max_deviation = max(max_deviation, absolute)
                if absolute > scaled_tolerance and first is None:
                    first = {
                        "path": path,
                        "query_value": query_value,
                        "batch_value": batch_value,
                        "absolute_deviation": absolute,
                        "tolerance": tolerance,
                        "scaled_tolerance": scaled_tolerance,
                        "query_intermediate": {
                            "log_probabilities": query.get("log_probabilities"),
                            "log_norm": query.get("log_norm"),
                            "raw_chunk_sum": query.get("raw_chunk_sum"),
                        },
                        "batch_intermediate": {
                            "log_probabilities": batch_row.get("log_probabilities"),
                            "log_norm": batch_row.get("log_norm"),
                            "raw_chunk_sum": batch_row.get("raw_chunk_sum"),
                        },
                    }
            elif query_value != batch_value and first is None:
                first = {
                    "path": path,
                    "query_value": query_value,
                    "batch_value": batch_value,
                    "absolute_deviation": None,
                    "tolerance": tolerance,
                    "query_intermediate": query,
                    "batch_intermediate": batch_row,
                }
        if first is not None:
            raise BusinessError(
                code=E_INCONSISTENT,
                message="interactive query disagrees with batch analysis",
                details={
                    "sample_id": sample_id,
                    "first_deviation_position": first["path"],
                    "first_deviation": first,
                    "query_path": f"query:{sample_id}",
                    "batch_path": f"batch:rows[{batch_index}]",
                    "query_intermediate": first["query_intermediate"],
                    "batch_intermediate": first["batch_intermediate"],
                    "precision": self.config.precision,
                    "stats_hash": self.state.stats_payload["stats_hash"],
                    "chunk_ids": query_chunk_ids,
                },
            )
        return {
            "consistent": True,
            "tolerance": tolerance,
            "max_absolute_deviation": max_deviation,
            "precision": self.config.precision,
            "stats_hash": self.state.stats_payload["stats_hash"],
            "chunk_ids": query_chunk_ids,
            "query_path": f"query:{sample_id}",
            "batch_path": f"batch:rows[{batch_index}]",
        }

    # ------------------------------------------------------------------
    # Error spectrum and self-check
    # ------------------------------------------------------------------
    def error_spectrum(self) -> dict[str, Any]:
        self._ensure_analysis()
        reference_config = self.config.with_precision("float64")
        reference = self._compute_analysis(
            reference_config, use_cache=False, force=True
        )
        candidate = self.state
        if candidate is None:
            raise RuntimeError("analysis state missing")
        spectrum = build_error_spectrum(
            candidate_config=self.config,
            candidate_ops=self.ops,
            candidate_normalized=candidate.normalized_values,
            candidate_batch=candidate.batch_payload,
            reference_config=reference_config,
            reference_ops=reference.ops,
            reference_normalized=reference.normalized_values,
            reference_batch=reference.batch_payload,
        )
        spectrum["candidate_batch_checksum"] = candidate.batch_payload["checksum"]
        spectrum["reference_batch_checksum"] = reference.batch_payload["checksum"]
        self.last_spectrum = spectrum
        write_json_atomic(
            self.artifact_dir / "error_spectrum.json", spectrum, indent=2
        )
        return spectrum

    def self_check(self) -> dict[str, Any]:
        self._ensure_analysis()
        checks: list[dict[str, Any]] = []
        batch = self.state.batch_payload
        rows = batch["rows"]
        index_ids = list(self.query_index)
        batch_ids = [row["sample_id"] for row in rows]
        index_ok = index_ids == batch_ids and all(
            self.query_index[row["sample_id"]]["result_checksum"]
            == row["result_checksum"]
            for row in rows
        )
        checks.append(
            {
                "name": "query_index_matches_batch",
                "ok": bool(index_ok),
                "details": {
                    "query_index_count": len(index_ids),
                    "batch_count": len(batch_ids),
                },
            }
        )
        row_checksum_ok = True
        for row in rows:
            if _row_checksum(row) != row.get("result_checksum"):
                row_checksum_ok = False
                break
        checks.append(
            {
                "name": "batch_row_checksums",
                "ok": row_checksum_ok,
                "details": {"rows": len(rows)},
            }
        )
        spectrum = self.last_spectrum
        if spectrum is None:
            spectrum = self.error_spectrum()
        stage_recompute: list[dict[str, Any]] = []
        for stage in spectrum["stages"]:
            recomputed = recompute_stage(stage)
            stage_recompute.append(
                {
                    "stage": stage["stage"],
                    "ok": recomputed == stage["summary"],
                    "recomputed": recomputed,
                    "stored": stage["summary"],
                }
            )
        checks.append(
            {
                "name": "error_spectrum_recomputable",
                "ok": all(item["ok"] for item in stage_recompute),
                "details": {"stages": stage_recompute},
            }
        )
        invalidation = self.last_invalidation or {
            "affected_samples": list(self.sample_ids),
            "recomputed_samples": list(self.sample_ids),
            "reused_samples": [],
        }
        recomputed = set(invalidation["recomputed_samples"])
        reused = set(invalidation["reused_samples"])
        affected = set(invalidation["affected_samples"])
        sets_ok = (
            not (recomputed & reused)
            and (recomputed | reused) == affected
            and affected == set(self.sample_ids)
        )
        checks.append(
            {
                "name": "invalidation_sets_disjoint_and_complete",
                "ok": sets_ok,
                "details": {
                    "recomputed": sorted(recomputed),
                    "reused": sorted(reused),
                    "affected": sorted(affected),
                    "intersection": sorted(recomputed & reused),
                },
            }
        )
        snapshot_status = self.snapshots.status()
        checks.append(
            {
                "name": "snapshot_integrity",
                "ok": snapshot_status is not None
                and snapshot_status["loaded"],
                "details": snapshot_status,
            }
        )
        cache_key_ok = True
        required_dependency_keys = {
            "sample_hash",
            "chunk_size",
            "precision",
            "seed",
            "upstream",
        }
        for action in self.cache.actions:
            if action.stage == "batch_result":
                missing = required_dependency_keys - set(action.dependencies)
                if missing:
                    cache_key_ok = False
                    break
        checks.append(
            {
                "name": "cache_keys_include_dependencies",
                "ok": cache_key_ok,
                "details": {
                    "required": sorted(required_dependency_keys),
                    "checked_batch_key": True,
                },
            }
        )
        ok = all(check["ok"] for check in checks)
        result = {
            "ok": ok,
            "checks": checks,
            "failed": [check["name"] for check in checks if not check["ok"]],
        }
        write_json_atomic(
            self.artifact_dir / "self_check.json", result, indent=2
        )
        return result

    def _write_artifacts(self) -> None:
        if self.state is None:
            return
        write_json_atomic(
            self.artifact_dir / "batch_results.json",
            self.state.batch_payload,
            indent=2,
        )
        write_json_atomic(
            self.artifact_dir / "analysis_report.json",
            {
                "dataset_hash": self.dataset_hash,
                "config_hash": self.config_hash,
                "sample_count": len(self.sample_ids),
                "chunk_count": len(self.chunks),
                "precision": self.config.precision,
                "model_family": self.config.model_family,
                "snapshot": self.snapshots.status(),
                "invalidation": self.last_invalidation,
                "cache": self.cache.action_report(),
            },
            indent=2,
        )

    def snapshot_status(self) -> dict[str, Any] | None:
        return self.snapshots.status()
