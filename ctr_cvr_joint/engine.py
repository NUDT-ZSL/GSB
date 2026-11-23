"""Deterministic orchestration for assembly, scoring, caches and invalidation."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .assembly import (
    AssembledRow,
    AssemblyResult,
    assemble_features,
    assembly_feature_names,
    encode_matrix,
)
from .cache import CacheKey, ContentAddressedCache, StageLock, make_cache_key
from .errors import (
    E_CACHE_CORRUPT,
    E_FORMAT_INVALID,
    E_INVALID_ARGUMENT,
    E_LINEAGE_BROKEN,
    E_RESOURCE_LIMIT,
    fail,
)
from .hashing import (
    atomic_write_bytes,
    atomic_write_json,
    sha256_bytes,
    sha256_obj,
)
from .models import FeatureSpec, Sample, ThresholdConfig
from .samples import order_samples, samples_checksum
from .scoring import (
    SCORING_PROTOCOL,
    compare_probability,
    recompute_score_row,
    score_samples,
)
from .snapshots import SnapshotRegistry
from .specs import LoadedSpec, normalize_spec
from .statistics import (
    LoadedStatistics,
    accumulate_statistics,
    changed_statistics_paths,
    statistics_dependency_projection,
)


ASSEMBLY_PROTOCOL = "deterministic_assembly_v1"
ACCUMULATION_PROTOCOL = "online_statistics_accumulation_v1"


@dataclass(frozen=True)
class EngineConfig:
    seed: int = 42
    threshold: ThresholdConfig = field(default_factory=ThresholdConfig)
    joint_click_scale: float = 0.25
    max_feature_matrix_bytes: int = 64 * 1024 * 1024
    max_parallel_nodes: int = 4
    parallel_nodes: int = 1
    degrade_on_parallel_limit: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            fail(E_INVALID_ARGUMENT, "seed must be an integer", field="seed")
        if (
            isinstance(self.max_feature_matrix_bytes, bool)
            or not isinstance(self.max_feature_matrix_bytes, int)
            or self.max_feature_matrix_bytes < 1
        ):
            fail(
                E_RESOURCE_LIMIT,
                "max_feature_matrix_bytes must be a positive integer",
                field="max_feature_matrix_bytes",
            )
        if (
            isinstance(self.max_parallel_nodes, bool)
            or not isinstance(self.max_parallel_nodes, int)
            or self.max_parallel_nodes < 1
        ):
            fail(
                E_RESOURCE_LIMIT,
                "max_parallel_nodes must be a positive integer",
                field="max_parallel_nodes",
            )
        if (
            isinstance(self.parallel_nodes, bool)
            or not isinstance(self.parallel_nodes, int)
            or self.parallel_nodes < 1
        ):
            fail(
                E_RESOURCE_LIMIT,
                "parallel_nodes must be a positive integer",
                field="parallel_nodes",
            )
        if (
            not math.isfinite(self.joint_click_scale)
            or not 0.0 <= self.joint_click_scale <= 1.0
        ):
            fail(
                E_INVALID_ARGUMENT,
                "joint_click_scale must be finite and in [0, 1]",
                field="joint_click_scale",
            )

    def scoring_config(self) -> dict[str, Any]:
        return {
            "protocol": SCORING_PROTOCOL,
            "joint_click_scale": float(self.joint_click_scale),
            "threshold": self.threshold.to_dict(),
        }

    @property
    def scoring_config_checksum(self) -> str:
        return sha256_obj(self.scoring_config())


@dataclass(frozen=True)
class RunReport:
    data: dict[str, Any]
    matrix_bytes: bytes | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.data

    @property
    def checksum(self) -> str:
        return sha256_obj(self.data)

    def write(self, directory: str | Path, *, name: str = "report") -> dict[str, str]:
        root = Path(directory)
        report_path = root / f"{name}.json"
        atomic_write_json(report_path, self.data)
        result = {"report_path": str(report_path), "report_checksum": self.checksum}
        if self.matrix_bytes is not None:
            matrix_path = root / f"{name}.matrix"
            atomic_write_bytes(matrix_path, self.matrix_bytes)
            result["matrix_path"] = str(matrix_path)
            result["matrix_sha256"] = sha256_bytes(self.matrix_bytes)
        return result


class JointCTREngine:
    """In-memory orchestration with durable snapshots and content caches."""

    def __init__(self, workspace: str | Path, config: EngineConfig | None = None) -> None:
        self.workspace = Path(workspace)
        self.config = config or EngineConfig()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.snapshots = SnapshotRegistry(self.workspace / "snapshots")
        self.cache = ContentAddressedCache(self.workspace / "cache")
        self._state: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Public run entry points
    # ------------------------------------------------------------------
    def run_full(
        self,
        samples: Iterable[Sample],
        spec: LoadedSpec | FeatureSpec,
        stats: LoadedStatistics,
        *,
        spec_snapshot_version: int | None = None,
        run_label: str = "full",
    ) -> RunReport:
        return self._run(
            samples,
            spec,
            stats,
            spec_snapshot_version=spec_snapshot_version,
            change_kind="full",
            run_label=run_label,
        )

    def run_incremental(
        self,
        samples: Iterable[Sample],
        spec: LoadedSpec | FeatureSpec,
        stats: LoadedStatistics,
        *,
        change_kind: str = "auto",
        spec_snapshot_version: int | None = None,
        run_label: str = "incremental",
    ) -> RunReport:
        if change_kind not in {"auto", "threshold", "stats", "spec", "labels"}:
            fail(
                E_INVALID_ARGUMENT,
                "change_kind must be auto, threshold, stats, spec or labels",
                field="change_kind",
                value=change_kind,
            )
        with StageLock(self.workspace):
            return self._run(
                samples,
                spec,
                stats,
                spec_snapshot_version=spec_snapshot_version,
                change_kind=change_kind,
                run_label=run_label,
            )

    def _run(
        self,
        samples: Iterable[Sample],
        spec_input: LoadedSpec | FeatureSpec,
        stats: LoadedStatistics,
        *,
        spec_snapshot_version: int | None,
        change_kind: str,
        run_label: str,
    ) -> RunReport:
        ordered_samples = order_samples(samples)
        if not ordered_samples:
            fail(E_FORMAT_INVALID, "at least one sample is required", field="samples")
        spec = spec_input.spec if isinstance(spec_input, LoadedSpec) else spec_input
        normalized = normalize_spec(spec)
        source_spec_checksum = (
            spec_input.checksum if isinstance(spec_input, LoadedSpec) else spec.checksum
        )
        spec_version = (
            int(spec_snapshot_version)
            if spec_snapshot_version is not None
            else int(spec.source_version)
        )
        spec_snapshot_payload = {
            "format_version": spec.source_version,
            "internal_checksum": normalized.checksum,
            "source_checksum": source_spec_checksum,
            "spec": spec.to_dict(),
        }
        spec_info = self.snapshots.commit(
            "feature_spec", spec_version, spec_snapshot_payload
        )
        stats_info = self.snapshots.commit(
            "statistics", stats.version, stats.payload
        )
        previous = self._state
        affected = self._plan_invalidation(
            previous,
            ordered_samples,
            normalized,
            stats,
            spec_version,
            change_kind,
        )
        degraded = self._parallelism_degradation()
        feature_names = assembly_feature_names(normalized)
        estimated_bytes = len(ordered_samples) * len(feature_names) * 8
        if estimated_bytes > self.config.max_feature_matrix_bytes:
            fail(
                E_RESOURCE_LIMIT,
                "assembled feature matrix exceeds the configured memory limit",
                resource="feature_matrix_bytes",
                estimated_bytes=estimated_bytes,
                limit_bytes=self.config.max_feature_matrix_bytes,
                rows=len(ordered_samples),
                columns=len(feature_names),
            )

        node_runs: dict[str, dict[str, Any]] = {
            "assembly": self._empty_node_run(),
            "scoring": self._empty_node_run(),
            "accumulation": self._empty_node_run(),
        }
        node_products: dict[str, dict[str, dict[str, Any]]] = {
            "assembly": {},
            "scoring": {},
            "accumulation": {},
        }

        # Assembly node.
        assembled_rows: list[AssembledRow] = []
        for sample in ordered_samples:
            key = self._assembly_cache_key(sample, normalized)
            envelope = self.cache.get("assembly", key)
            if envelope is None:
                single = assemble_features(
                    [sample],
                    normalized,
                    seed=self.config.seed,
                    max_matrix_bytes=self.config.max_feature_matrix_bytes,
                )
                product = {
                    "protocol": ASSEMBLY_PROTOCOL,
                    "request_id": sample.request_id,
                    "row": single.rows[0].to_dict(),
                    "dependency": {
                        "spec_assembly_checksum": self._spec_projection(
                            normalized, "assembly"
                        )["checksum"],
                        "spec_format_version": normalized.source_version,
                        "seed": self.config.seed,
                    },
                }
                envelope = self.cache.put(
                    "assembly",
                    key,
                    product=product,
                    request_id=sample.request_id,
                    node_path=("assembly",),
                )
                node_runs["assembly"]["recomputed"].append(sample.request_id)
            else:
                node_runs["assembly"]["reused"].append(sample.request_id)
            product = envelope["product"]
            row = self._assembled_row_from_product(product)
            if row.feature_names != feature_names:
                fail(
                    E_CACHE_CORRUPT,
                    "cached assembly row feature layout does not match the current spec",
                    request_id=sample.request_id,
                    field="feature_names",
                )
            assembled_rows.append(row)
            node_products["assembly"][sample.request_id] = {
                "key": key,
                "product_hash": envelope["product_hash"],
                "product": product,
            }
        matrix_bytes = encode_matrix(
            feature_names, [row.values for row in assembled_rows]
        )
        matrix_sha = sha256_bytes(matrix_bytes)
        assembly_summary = {
            "protocol": ASSEMBLY_PROTOCOL,
            "sort": ["request_id:asc", "timestamp_ms:asc", "request_id:asc"],
            "seed": self.config.seed,
            "spec_version": normalized.source_version,
            "spec_snapshot_version": spec_version,
            "spec_checksum": source_spec_checksum,
            "feature_names": list(feature_names),
            "shape": [len(assembled_rows), len(feature_names)],
            "matrix_dtype": "float64-le",
            "matrix_sha256": matrix_sha,
            "rows": [
                {
                    "request_id": row.request_id,
                    "timestamp_ms": row.timestamp_ms,
                    "row_hash": row.row_hash,
                }
                for row in assembled_rows
            ],
        }
        assembly_summary["summary_sha256"] = sha256_obj(assembly_summary)

        # Scoring node.
        score_rows: list[dict[str, Any]] = []
        for sample, assembled in zip(ordered_samples, assembled_rows):
            assembly_info = node_products["assembly"][sample.request_id]
            key, stats_projection = self._score_cache_key(
                sample, normalized, stats, assembly_info["product_hash"]
            )
            envelope = self.cache.get("scoring", key)
            if envelope is None:
                mini = self._mini_assembly(
                    assembled, normalized, source_spec_checksum, spec_version
                )
                scored = score_samples(
                    mini,
                    stats,
                    normalized,
                    self.config.threshold,
                    joint_click_scale=self.config.joint_click_scale,
                ).rows[0]
                product = {
                    "protocol": SCORING_PROTOCOL,
                    "request_id": sample.request_id,
                    "row": scored,
                    "assembly_product_hash": assembly_info["product_hash"],
                    "statistics_projection": stats_projection,
                }
                envelope = self.cache.put(
                    "scoring",
                    key,
                    product=product,
                    request_id=sample.request_id,
                    node_path=("assembly", "scoring"),
                )
                node_runs["scoring"]["recomputed"].append(sample.request_id)
            else:
                node_runs["scoring"]["reused"].append(sample.request_id)
            scored_row = dict(envelope["product"]["row"])
            score_rows.append(scored_row)
            node_products["scoring"][sample.request_id] = {
                "key": key,
                "product_hash": envelope["product_hash"],
                "product": envelope["product"],
            }
        probability_rows = [
            {
                "click": row["click"]["probability"],
                "convert": row["convert"]["probability"],
                "expected_conversion": row["expected_conversion"],
            }
            for row in score_rows
        ]
        score_hash = sha256_obj(score_rows)
        probability_hash = sha256_obj(probability_rows)
        scoring_config = self.config.scoring_config()
        score_summary = {
            "protocol": SCORING_PROTOCOL,
            "score_hash": score_hash,
            "probability_hash": probability_hash,
            "statistics_version": stats.version,
            "statistics_checksum": stats.checksum,
            "scoring_config": scoring_config,
        }
        score_summary["summary_sha256"] = sha256_obj(score_summary)

        # Accumulation node (per-request products, deterministic merge).
        for sample, assembled, scored in zip(
            ordered_samples, assembled_rows, score_rows
        ):
            score_info = node_products["scoring"][sample.request_id]
            key = self._accumulation_cache_key(
                sample,
                normalized,
                stats,
                score_info["product_hash"],
                scored["probability_hash"],
            )
            envelope = self.cache.get("accumulation", key)
            if envelope is None:
                single_payload = accumulate_statistics(
                    [assembled],
                    [scored],
                    [sample],
                )
                product = {
                    "protocol": ACCUMULATION_PROTOCOL,
                    "request_id": sample.request_id,
                    "label_hash": sample.label_hash,
                    "score_probability_hash": scored["probability_hash"],
                    "accumulation": single_payload,
                }
                envelope = self.cache.put(
                    "accumulation",
                    key,
                    product=product,
                    request_id=sample.request_id,
                    node_path=("assembly", "scoring", "accumulation"),
                )
                node_runs["accumulation"]["recomputed"].append(sample.request_id)
            else:
                node_runs["accumulation"]["reused"].append(sample.request_id)
            node_products["accumulation"][sample.request_id] = {
                "key": key,
                "product_hash": envelope["product_hash"],
                "product": envelope["product"],
            }
        accumulation_payload = self._merge_accumulation_products(
            [
                node_products["accumulation"][sample.request_id]["product"][
                    "accumulation"
                ]
                for sample in ordered_samples
            ]
        )
        accumulation_summary = {
            "protocol": ACCUMULATION_PROTOCOL,
            "checksum": sha256_obj(accumulation_payload),
            "payload": accumulation_payload,
        }

        traces = self._build_traces(
            ordered_samples,
            normalized,
            stats,
            spec_version,
            source_spec_checksum,
            node_products,
        )
        nodes_report = self._nodes_report(ordered_samples, node_runs, affected)
        invalidation = self._invalidation_report(
            affected,
            change_kind,
            node_runs,
            ordered_samples,
        )
        next_result_version = (
            self.snapshots.current_info("score_results").version + 1
            if self.snapshots.current_info("score_results") is not None
            else 1
        )
        report_data: dict[str, Any] = {
            "schema_version": 1,
            "business_code": "OK",
            "business_codes": [],
            "run_label": run_label,
            "samples": {
                "count": len(ordered_samples),
                "checksum": samples_checksum(ordered_samples),
                "request_ids": [sample.request_id for sample in ordered_samples],
            },
            "feature_spec": {
                "source_version": normalized.source_version,
                "snapshot_version": spec_version,
                "source_checksum": source_spec_checksum,
                "internal_checksum": normalized.checksum,
                "snapshot_checksum": spec_info.checksum,
            },
            "statistics": {
                "snapshot_version": stats.version,
                "checksum": stats.checksum,
                "snapshot_checksum": stats_info.checksum,
            },
            "threshold": self.config.threshold.to_dict(),
            "assembly_summary": assembly_summary,
            "score_summary": score_summary,
            "accumulation_summary": accumulation_summary,
            "contributions": [
                {
                    "request_id": row["request_id"],
                    "click": {
                        "probability": row["click"]["probability"],
                        "decision": row["click"]["decision"],
                        "comparison": row["click"]["comparison"],
                        "contributions": row["click"]["contributions"],
                    },
                    "convert": {
                        "probability": row["convert"]["probability"],
                        "decision": row["convert"]["decision"],
                        "comparison": row["convert"]["comparison"],
                        "contributions": row["convert"]["contributions"],
                    },
                    "expected_conversion": row["expected_conversion"],
                }
                for row in score_rows
            ],
            "cache": {"nodes": nodes_report},
            "invalidation": invalidation,
            "traces": traces,
            "lineage_index": {
                trace["request_id"]: {
                    "score_cache_key": trace["cache_key"],
                    "node_path": trace["node_path"],
                    "assembly_product_hash": trace["assembly_product_hash"],
                    "score_product_hash": trace["score_product_hash"],
                    "accumulation_product_hash": trace[
                        "accumulation_product_hash"
                    ],
                }
                for trace in traces
            },
            "degraded": degraded,
            "score_result_snapshot_version": next_result_version,
        }
        self_check = self._self_check_data(
            report_data,
            ordered_samples,
            node_products,
            node_runs,
            traces,
            normalized,
            stats,
        )
        report_data["self_check"] = self_check
        snapshot_summary = {
            "run_label": run_label,
            "samples_checksum": report_data["samples"]["checksum"],
            "feature_spec": report_data["feature_spec"],
            "statistics": report_data["statistics"],
            "assembly_summary": assembly_summary,
            "score_summary": score_summary,
            "accumulation_summary": {
                "protocol": accumulation_summary["protocol"],
                "checksum": accumulation_summary["checksum"],
            },
            "request_ids": report_data["samples"]["request_ids"],
            "self_check_ok": self_check["ok"],
        }
        result_info = self.snapshots.commit(
            "score_results", next_result_version, snapshot_summary
        )
        report_data["score_result_snapshot"] = result_info.to_dict()
        report = RunReport(data=report_data, matrix_bytes=matrix_bytes)

        self._state = {
            "samples": ordered_samples,
            "samples_by_id": {sample.request_id: sample for sample in ordered_samples},
            "spec": normalized,
            "spec_version": spec_version,
            "source_spec_checksum": source_spec_checksum,
            "stats": stats,
            "config": self.config,
            "report": report,
            "node_products": node_products,
            "node_runs": node_runs,
            "affected": affected,
            "traces": {trace["request_id"]: trace for trace in traces},
            "matrix_bytes": matrix_bytes,
        }
        return report

    # ------------------------------------------------------------------
    # Cache-key and product helpers
    # ------------------------------------------------------------------
    def _assembly_cache_key(self, sample: Sample, spec: FeatureSpec) -> CacheKey:
        projection = self._spec_projection(spec, "assembly")
        components = {
            "node": "assembly",
            "sample_content_hash": sample.feature_content_hash,
            "internal_spec_version": projection["version"],
            "spec_checksum": projection["checksum"],
            "statistics_snapshot_version": "not_applicable",
            "upstream_product_hash": "root",
            "seed": self.config.seed,
            "protocol": ASSEMBLY_PROTOCOL,
        }
        return make_cache_key(components)

    def _score_cache_key(
        self,
        sample: Sample,
        spec: FeatureSpec,
        stats: LoadedStatistics,
        assembly_product_hash: str,
    ) -> tuple[CacheKey, dict[str, Any]]:
        projection = self._spec_projection(spec, "scoring")
        stats_versions, stats_fingerprint = statistics_dependency_projection(
            stats, spec, sample
        )
        components = {
            "node": "scoring",
            "sample_content_hash": sample.feature_content_hash,
            "internal_spec_version": projection["version"],
            "spec_checksum": projection["checksum"],
            "statistics_snapshot_version": stats_versions,
            "statistics_checksum": stats_fingerprint,
            "upstream_product_hash": assembly_product_hash,
            "seed": self.config.seed,
            "scoring_config_checksum": self.config.scoring_config_checksum,
            "protocol": SCORING_PROTOCOL,
        }
        return make_cache_key(components), {
            "versions": stats_versions,
            "fingerprint": stats_fingerprint,
        }

    def _accumulation_cache_key(
        self,
        sample: Sample,
        spec: FeatureSpec,
        stats: LoadedStatistics,
        score_product_hash: str,
        score_probability_hash: str,
    ) -> CacheKey:
        projection = self._spec_projection(spec, "assembly")
        stats_versions, stats_fingerprint = statistics_dependency_projection(
            stats, spec, sample
        )
        components = {
            "node": "accumulation",
            "sample_content_hash": sample.feature_content_hash,
            "internal_spec_version": projection["version"],
            "spec_checksum": projection["checksum"],
            "statistics_snapshot_version": stats_versions,
            "statistics_checksum": stats_fingerprint,
            "upstream_product_hash": score_probability_hash,
            "label_hash": sample.label_hash,
            "protocol": ACCUMULATION_PROTOCOL,
        }
        return make_cache_key(components)

    def _spec_projection(self, spec: FeatureSpec, mode: str) -> dict[str, Any]:
        normalized = normalize_spec(spec)
        if mode == "assembly":
            payload: dict[str, Any] = {
                "format_version": normalized.source_version,
                "numeric": [
                    {
                        "name": item.name,
                        "mean": item.mean,
                        "std": item.std,
                        "missing_strategy": item.missing_strategy,
                        "default": item.default,
                    }
                    for item in normalized.numeric
                ],
                "categorical": [
                    {
                        "name": item.name,
                        "buckets": item.buckets,
                        "missing_strategy": item.missing_strategy,
                        "default": item.default,
                    }
                    for item in normalized.categorical
                ],
                "cross": [
                    {
                        "name": item.name,
                        "left": item.left,
                        "right": item.right,
                        "slots": list(item.slots),
                        "missing_strategy": item.missing_strategy,
                    }
                    for item in normalized.cross
                ],
            }
        elif mode == "scoring":
            payload = {
                "format_version": normalized.source_version,
                "numeric": [
                    {
                        "name": item.name,
                        "mean": item.mean,
                        "std": item.std,
                        "weight": item.weight,
                        "missing_strategy": item.missing_strategy,
                        "default": item.default,
                    }
                    for item in normalized.numeric
                ],
                "categorical": [
                    {
                        "name": item.name,
                        "buckets": item.buckets,
                        "weight": item.weight,
                        "missing_strategy": item.missing_strategy,
                        "default": item.default,
                    }
                    for item in normalized.categorical
                ],
                "cross": [
                    {
                        "name": item.name,
                        "left": item.left,
                        "right": item.right,
                        "weight": item.weight,
                        "slots": list(item.slots),
                        "missing_strategy": item.missing_strategy,
                    }
                    for item in normalized.cross
                ],
                "statistics": [item.to_dict() for item in normalized.statistics],
            }
        else:
            fail(E_INVALID_ARGUMENT, "unknown spec projection mode", field="mode")
        checksum = sha256_obj(payload)
        return {
            "version": f"v{normalized.source_version}:{checksum[:16]}",
            "checksum": checksum,
            "payload": payload,
        }

    def _assembled_row_from_product(self, product: dict[str, Any]) -> AssembledRow:
        row = product["row"]
        return AssembledRow(
            request_id=row["request_id"],
            timestamp_ms=int(row["timestamp_ms"]),
            feature_names=tuple(row["feature_names"]),
            values=tuple(float(value) for value in row["values"]),
            details=tuple(row["details"]),
            row_hash=row["row_hash"],
        )

    def _mini_assembly(
        self,
        row: AssembledRow,
        spec: FeatureSpec,
        spec_checksum: str,
        spec_version: int,
    ) -> AssemblyResult:
        matrix = encode_matrix(row.feature_names, [row.values])
        summary = {
            "request_id": row.request_id,
            "matrix_sha256": sha256_bytes(matrix),
            "shape": [1, len(row.feature_names)],
        }
        return AssemblyResult(
            spec_version=spec_version,
            spec_checksum=spec_checksum,
            seed=self.config.seed,
            feature_names=row.feature_names,
            rows=(row,),
            matrix_bytes=matrix,
            matrix_sha256=summary["matrix_sha256"],
            summary=summary,
            summary_sha256=sha256_obj(summary),
        )

    # ------------------------------------------------------------------
    # Invalidation planning
    # ------------------------------------------------------------------
    def _plan_invalidation(
        self,
        previous: dict[str, Any] | None,
        samples: list[Sample],
        spec: FeatureSpec,
        stats: LoadedStatistics,
        spec_version: int,
        change_kind: str,
    ) -> dict[str, Any]:
        all_ids = {sample.request_id for sample in samples}
        if previous is None:
            all_features = sorted(
                [f"numeric.{item.name}" for item in spec.numeric]
                + [f"categorical.{item.name}" for item in spec.categorical]
                + [
                    f"cross.{item.name}.{slot}"
                    for item in spec.cross
                    for slot in item.slots
                ]
            )
            return {
                "change_kind": change_kind,
                "detected_changes": ["initial_run"],
                "changed_spec_paths": [],
                "changed_statistics_paths": [],
                "affected_features": all_features,
                "affected_derived_features": all_features,
                "assembly": sorted(all_ids),
                "scoring": sorted(all_ids),
                "accumulation": sorted(all_ids),
                "added": sorted(all_ids),
                "removed": [],
                "labels": sorted(all_ids),
                "spec_assembly_changed": True,
                "spec_scoring_changed": True,
                "stats_changed": True,
                "threshold_changed": True,
            }
        old_samples = previous["samples_by_id"]
        new_samples = {sample.request_id: sample for sample in samples}
        old_ids = set(old_samples)
        added = all_ids - old_ids
        removed = old_ids - all_ids
        common = all_ids & old_ids
        feature_changed = {
            request_id
            for request_id in common
            if old_samples[request_id].feature_content_hash
            != new_samples[request_id].feature_content_hash
        }
        label_changed = {
            request_id
            for request_id in common
            if old_samples[request_id].label_hash
            != new_samples[request_id].label_hash
        }
        old_spec_projection_assembly = self._spec_projection(
            previous["spec"], "assembly"
        )
        new_spec_projection_assembly = self._spec_projection(spec, "assembly")
        old_spec_projection_scoring = self._spec_projection(
            previous["spec"], "scoring"
        )
        new_spec_projection_scoring = self._spec_projection(spec, "scoring")
        spec_assembly_changed = (
            old_spec_projection_assembly["checksum"]
            != new_spec_projection_assembly["checksum"]
            or int(previous["spec_version"]) != int(spec_version)
        )
        spec_scoring_changed = (
            old_spec_projection_scoring["checksum"]
            != new_spec_projection_scoring["checksum"]
            or spec_assembly_changed
        )
        old_stats = previous["stats"]
        stats_changed = old_stats.checksum != stats.checksum
        threshold_changed = (
            previous["config"].scoring_config_checksum
            != self.config.scoring_config_checksum
        )
        stats_affected: set[str] = set()
        if stats_changed:
            for sample in samples:
                old_versions, old_fp = statistics_dependency_projection(
                    old_stats, previous["spec"], sample
                )
                new_versions, new_fp = statistics_dependency_projection(
                    stats, spec, sample
                )
                if old_fp != new_fp or old_versions != new_versions:
                    stats_affected.add(sample.request_id)
        assembly_affected = set(added) | feature_changed
        if spec_assembly_changed:
            assembly_affected |= all_ids
        scoring_affected = (
            set(assembly_affected)
            | set(added)
            | (
                all_ids
                if (spec_scoring_changed or threshold_changed)
                else set()
            )
            | stats_affected
        )
        probability_affected = (
            set(assembly_affected)
            | set(stats_affected)
            | (all_ids if spec_scoring_changed else set())
            | set(added)
        )
        accumulation_affected = (
            probability_affected
            | set(label_changed)
            | (all_ids if spec_assembly_changed else set())
            | set(removed)
        )
        detected: list[str] = []
        if spec_assembly_changed or spec_scoring_changed:
            detected.append("feature_spec")
        if stats_changed:
            detected.append("statistics")
        if threshold_changed:
            detected.append("threshold")
        if label_changed:
            detected.append("labels")
        if added or removed:
            detected.append("sample_set")
        if change_kind != "auto" and change_kind not in detected:
            detected.append(f"declared_{change_kind}")
        changed_spec_paths: list[str] = []
        if spec_assembly_changed or spec_scoring_changed:
            changed_spec_paths = _changed_flat_paths(
                old_spec_projection_scoring["payload"],
                new_spec_projection_scoring["payload"],
            )
        changed_stats_paths = (
            changed_statistics_paths(old_stats, stats) if stats_changed else []
        )
        affected_features = _spec_affected_features(changed_spec_paths, spec)
        affected_derived_features = _derived_feature_names(affected_features, spec)
        if stats_changed:
            stats_features = _statistics_affected_features(changed_stats_paths)
            affected_features = sorted(
                set(affected_features) | set(stats_features)
            )
            affected_derived_features = sorted(
                set(affected_derived_features)
                | set(_derived_feature_names(stats_features, spec))
            )
        return {
            "change_kind": change_kind,
            "detected_changes": detected,
            "changed_spec_paths": changed_spec_paths,
            "changed_statistics_paths": changed_stats_paths,
            "affected_features": sorted(affected_features),
            "affected_derived_features": affected_derived_features,
            "assembly": sorted(assembly_affected),
            "scoring": sorted(scoring_affected),
            "accumulation": sorted(accumulation_affected),
            "added": sorted(added),
            "removed": sorted(removed),
            "labels": sorted(label_changed),
            "spec_assembly_changed": bool(spec_assembly_changed),
            "spec_scoring_changed": bool(spec_scoring_changed),
            "stats_changed": bool(stats_changed),
            "threshold_changed": bool(threshold_changed),
        }

    def _parallelism_degradation(self) -> list[str]:
        if self.config.parallel_nodes <= self.config.max_parallel_nodes:
            return []
        if self.config.degrade_on_parallel_limit:
            return ["parallelism_capped"]
        fail(
            E_RESOURCE_LIMIT,
            "requested parallel node count exceeds the configured limit",
            resource="parallel_nodes",
            requested=self.config.parallel_nodes,
            limit=self.config.max_parallel_nodes,
        )

    # ------------------------------------------------------------------
    # Reporting and traceability
    # ------------------------------------------------------------------
    def _empty_node_run(self) -> dict[str, list[str]]:
        return {"recomputed": [], "reused": []}

    def _nodes_report(
        self,
        samples: list[Sample],
        node_runs: dict[str, dict[str, list[str]]],
        affected: dict[str, Any],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        current_ids = {sample.request_id for sample in samples}
        for node in ("assembly", "scoring", "accumulation"):
            recomputed = sorted(node_runs[node]["recomputed"])
            reused = sorted(node_runs[node]["reused"])
            result[node] = {
                "affected_samples": sorted(set(affected[node]) & current_ids),
                "recomputed_samples": recomputed,
                "reused_samples": reused,
                "recomputed_count": len(recomputed),
                "reused_count": len(reused),
            }
        return result

    def _invalidation_report(
        self,
        affected: dict[str, Any],
        change_kind: str,
        node_runs: dict[str, dict[str, list[str]]],
        samples: list[Sample],
    ) -> dict[str, Any]:
        recomputed_nodes = [
            node
            for node in ("assembly", "scoring", "accumulation")
            if node_runs[node]["recomputed"]
        ]
        affected_nodes = [
            node
            for node in ("assembly", "scoring", "accumulation")
            if affected[node]
        ]
        recomputed_ids = sorted(
            {
                request_id
                for node in recomputed_nodes
                for request_id in node_runs[node]["recomputed"]
            }
        )
        all_ids = {sample.request_id for sample in samples}
        return {
            "change_kind": change_kind,
            "detected_changes": affected["detected_changes"],
            "spec_assembly_changed": affected["spec_assembly_changed"],
            "spec_scoring_changed": affected["spec_scoring_changed"],
            "statistics_changed": affected["stats_changed"],
            "threshold_changed": affected["threshold_changed"],
            "changed_spec_paths": affected["changed_spec_paths"],
            "changed_statistics_paths": affected["changed_statistics_paths"],
            "affected_features": affected["affected_features"],
            "affected_derived_features": affected["affected_derived_features"],
            "affected_nodes": affected_nodes,
            "recomputed_nodes": recomputed_nodes,
            "affected_request_ids": sorted(
                set(affected["assembly"])
                | set(affected["scoring"])
                | set(affected["accumulation"])
            ),
            "affected_request_ids_by_node": {
                node: sorted(affected[node])
                for node in ("assembly", "scoring", "accumulation")
            },
            "recomputed_request_ids": recomputed_ids,
            "reused_request_ids": sorted(all_ids - set(recomputed_ids)),
            "added_request_ids": affected["added"],
            "removed_request_ids": affected["removed"],
            "label_corrected_request_ids": affected["labels"],
        }

    def _build_traces(
        self,
        samples: list[Sample],
        spec: FeatureSpec,
        stats: LoadedStatistics,
        spec_version: int,
        source_spec_checksum: str,
        node_products: dict[str, dict[str, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        traces: list[dict[str, Any]] = []
        spec_projection = self._spec_projection(spec, "scoring")
        for sample in samples:
            score_info = node_products["scoring"][sample.request_id]
            scored = score_info["product"]["row"]
            stats_versions, stats_fingerprint = statistics_dependency_projection(
                stats, spec, sample
            )
            trace = {
                "request_id": sample.request_id,
                "cache_key": score_info["key"].key,
                "cache_key_components": score_info["key"].components,
                "feature_spec_version": spec.source_version,
                "feature_spec_snapshot_version": spec_version,
                "feature_spec_checksum": source_spec_checksum,
                "internal_spec_dependency_checksum": spec_projection["checksum"],
                "statistics_version": stats.version,
                "statistics_checksum": stats.checksum,
                "statistics_projection": {
                    "versions": stats_versions,
                    "fingerprint": stats_fingerprint,
                },
                "threshold_checksum": self.config.scoring_config_checksum,
                "node_path": [
                    f"assembly:{node_products['assembly'][sample.request_id]['key'].key}",
                    f"scoring:{score_info['key'].key}",
                    f"accumulation:{node_products['accumulation'][sample.request_id]['key'].key}",
                ],
                "assembly_product_hash": node_products["assembly"][sample.request_id][
                    "product_hash"
                ],
                "score_product_hash": score_info["product_hash"],
                "score_probability_hash": scored["probability_hash"],
                "accumulation_product_hash": node_products["accumulation"][
                    sample.request_id
                ]["product_hash"],
                "assembly_cache_key": node_products["assembly"][sample.request_id][
                    "key"
                ].key,
                "scoring_cache_key": score_info["key"].key,
                "accumulation_cache_key": node_products["accumulation"][
                    sample.request_id
                ]["key"].key,
                "threshold_digits": self.config.threshold.stable_digits,
                "score": scored,
            }
            trace["trace_hash"] = sha256_obj(trace)
            traces.append(trace)
        return traces

    def _self_check_data(
        self,
        report_data: dict[str, Any],
        samples: list[Sample],
        node_products: dict[str, dict[str, dict[str, Any]]],
        node_runs: dict[str, dict[str, list[str]]],
        traces: list[dict[str, Any]],
        spec: FeatureSpec,
        stats: LoadedStatistics,
    ) -> dict[str, Any]:
        errors: list[str] = []
        all_ids = {sample.request_id for sample in samples}
        node_ids: dict[str, set[str]] = {}
        checks: dict[str, Any] = {}
        for node in ("assembly", "scoring", "accumulation"):
            ids = set(node_products[node])
            node_ids[node] = ids
            if ids != all_ids:
                errors.append(
                    f"{node} request_id set differs from the sample set"
                )
            recomputed = set(node_runs[node]["recomputed"])
            reused = set(node_runs[node]["reused"])
            if recomputed & reused:
                errors.append(f"{node} recomputed and reused sets overlap")
            if recomputed | reused != all_ids:
                errors.append(
                    f"{node} recomputed and reused sets do not cover all requests"
                )
            affected = set(report_data["cache"]["nodes"][node]["affected_samples"])
            if not affected <= (recomputed | reused):
                errors.append(f"{node} affected samples are not covered")
            for request_id, info in node_products[node].items():
                key: CacheKey = info["key"]
                if sha256_obj(key.components) != key.key:
                    errors.append(f"{node} cache key hash mismatch for {request_id}")
                envelope = self.cache.get(node, key)
                if envelope is None:
                    errors.append(f"{node} cache entry is missing for {request_id}")
                elif envelope["product_hash"] != info["product_hash"]:
                    errors.append(
                        f"{node} product hash mismatch for {request_id}"
                    )
            checks[node] = {
                "request_id_set_matches": ids == all_ids,
                "recomputed_reused_disjoint": not (recomputed & reused),
                "cover_all_requests": recomputed | reused == all_ids,
                "affected_covered": affected <= (recomputed | reused),
            }
        if node_ids["assembly"] != node_ids["scoring"] or node_ids[
            "scoring"
        ] != node_ids["accumulation"]:
            errors.append("cross-node request_id sets differ")
        for trace in traces:
            try:
                self.replay_trace(
                    trace,
                    spec=spec,
                    stats=stats,
                    sample=next(
                        sample
                        for sample in samples
                        if sample.request_id == trace["request_id"]
                    ),
                    require_current=False,
                )
            except Exception as exc:
                errors.append(
                    f"trace replay failed for {trace['request_id']}: {exc}"
                )
        feature_names = tuple(report_data["assembly_summary"]["feature_names"])
        assembled_rows = [
            node_products["assembly"][sample.request_id]["product"]["row"]
            for sample in samples
        ]
        rebuilt_matrix = encode_matrix(
            feature_names,
            [
                tuple(float(value) for value in row["values"])
                for row in assembled_rows
            ],
        )
        rebuilt_matrix_sha = sha256_bytes(rebuilt_matrix)
        if rebuilt_matrix_sha != report_data["assembly_summary"]["matrix_sha256"]:
            errors.append("rebuilt feature matrix hash mismatch")
        rebuilt_score_rows = [
            node_products["scoring"][sample.request_id]["product"]["row"]
            for sample in samples
        ]
        rebuilt_score_hash = sha256_obj(rebuilt_score_rows)
        if rebuilt_score_hash != report_data["score_summary"]["score_hash"]:
            errors.append("rebuilt score hash mismatch")
        rebuilt_accumulation = self._merge_accumulation_products(
            [
                node_products["accumulation"][sample.request_id]["product"][
                    "accumulation"
                ]
                for sample in samples
            ]
        )
        rebuilt_accumulation_checksum = sha256_obj(rebuilt_accumulation)
        if (
            rebuilt_accumulation_checksum
            != report_data["accumulation_summary"]["checksum"]
        ):
            errors.append("rebuilt accumulation checksum mismatch")
        checks["trace_replay"] = {
            "checked": len(traces),
            "failed": sum(1 for error in errors if "trace replay" in error),
        }
        checks["matrix_sha256"] = rebuilt_matrix_sha
        checks["score_hash"] = rebuilt_score_hash
        checks["accumulation_checksum"] = rebuilt_accumulation_checksum
        return {"ok": not errors, "errors": errors, "checks": checks}

    def explain_request(self, request_id: str) -> dict[str, Any]:
        if self._state is None:
            fail(
                E_INVALID_ARGUMENT,
                "no completed run is available",
                field="request_id",
            )
        if request_id not in self._state["node_products"]["assembly"]:
            fail(
                E_INVALID_ARGUMENT,
                "unknown request_id",
                field="request_id",
                request_id=request_id,
            )
        nodes = []
        for node in ("assembly", "scoring", "accumulation"):
            info = self._state["node_products"][node][request_id]
            envelope = self.cache.get(node, info["key"])
            nodes.append(
                {
                    "node": node,
                    "cache_key": info["key"].key,
                    "key_components": info["key"].components,
                    "product_hash": info["product_hash"],
                    "node_path": envelope.get("node_path", []) if envelope else [],
                }
            )
        return {
            "request_id": request_id,
            "feature_spec_snapshot_version": self._state["spec_version"],
            "statistics_snapshot_version": self._state["stats"].version,
            "nodes": nodes,
        }

    def replay_trace(
        self,
        trace: dict[str, Any],
        *,
        spec: LoadedSpec | FeatureSpec | None = None,
        stats: LoadedStatistics | None = None,
        sample: Sample | None = None,
        require_current: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(trace, dict) or "score" not in trace or "request_id" not in trace:
            fail(
                E_INVALID_ARGUMENT,
                "a complete score trace is required",
                field="trace",
            )
        if trace.get("trace_hash"):
            actual_trace_hash = sha256_obj(
                {key: value for key, value in trace.items() if key != "trace_hash"}
            )
            if actual_trace_hash != trace["trace_hash"]:
                fail(
                    E_CACHE_CORRUPT,
                    "score trace checksum mismatch",
                    request_id=trace.get("request_id"),
                    field="trace_hash",
                )
        persisted_score_hash = trace["score"].get("score_hash")
        if persisted_score_hash:
            actual_score_hash = sha256_obj(
                {
                    key: value
                    for key, value in trace["score"].items()
                    if key != "score_hash"
                }
            )
            if actual_score_hash != persisted_score_hash:
                fail(
                    E_CACHE_CORRUPT,
                    "persisted score row checksum mismatch",
                    request_id=trace.get("request_id"),
                    field="score_hash",
                )
        request_id = trace["request_id"]
        current_spec = (
            spec.spec
            if isinstance(spec, LoadedSpec)
            else spec
            if spec is not None
            else self._state["spec"]
            if self._state is not None
            else None
        )
        current_stats = (
            stats
            if stats is not None
            else self._state["stats"]
            if self._state is not None
            else None
        )
        if sample is None and self._state is not None:
            sample = self._state["samples_by_id"].get(request_id)
        if current_spec is None or current_stats is None or sample is None:
            fail(
                E_LINEAGE_BROKEN,
                "current lineage context is unavailable",
                request_id=request_id,
                invalid_versions=["feature_spec_or_statistics_or_sample"],
            )
        current_spec_projection = self._spec_projection(current_spec, "scoring")
        current_stats_versions, current_stats_fp = statistics_dependency_projection(
            current_stats, current_spec, sample
        )
        invalid_versions: list[dict[str, Any]] = []
        if (
            current_spec_projection["checksum"]
            != trace.get("internal_spec_dependency_checksum")
        ):
            invalid_versions.append(
                {
                    "dependency": "feature_spec",
                    "trace_checksum": trace.get(
                        "internal_spec_dependency_checksum"
                    ),
                    "current_checksum": current_spec_projection["checksum"],
                }
            )
        if (
            current_stats_fp
            != trace.get("statistics_projection", {}).get("fingerprint")
            or current_stats_versions
            != trace.get("statistics_projection", {}).get("versions")
        ):
            invalid_versions.append(
                {
                    "dependency": "statistics",
                    "trace_versions": trace.get("statistics_projection", {}).get(
                        "versions"
                    ),
                    "current_versions": current_stats_versions,
                }
            )
        if self.config.scoring_config_checksum != trace.get("threshold_checksum"):
            invalid_versions.append(
                {
                    "dependency": "scoring_config",
                    "trace_checksum": trace.get("threshold_checksum"),
                    "current_checksum": self.config.scoring_config_checksum,
                }
            )
        if require_current and invalid_versions:
            fail(
                E_LINEAGE_BROKEN,
                "score result refers to dependencies that have changed",
                request_id=request_id,
                invalid_versions=invalid_versions,
            )
        recomputed = recompute_score_row(trace["score"])
        matches = (
            recomputed["click"]["probability"]
            == trace["score"]["click"]["probability"]
            and recomputed["convert"]["probability"]
            == trace["score"]["convert"]["probability"]
            and recomputed["expected_conversion"]
            == trace["score"]["expected_conversion"]
        )
        if not matches:
            fail(
                E_CACHE_CORRUPT,
                "trace contribution replay does not reproduce the persisted probability",
                request_id=request_id,
                replayed=recomputed,
                persisted={
                    "click": trace["score"]["click"]["probability"],
                    "convert": trace["score"]["convert"]["probability"],
                    "expected_conversion": trace["score"]["expected_conversion"],
                },
            )
        threshold = ThresholdConfig(
            policy=trace["score"]["click"]["threshold_policy"],
            click_threshold=trace["score"]["click"]["threshold"],
            cvr_threshold=trace["score"]["convert"]["threshold"],
            tolerance=trace["score"]["click"]["tolerance"],
            stable_digits=trace["score"]["threshold_digits"],
        )
        click_decision = compare_probability(
            recomputed["click"]["probability"],
            threshold.click_threshold,
            policy=threshold.policy,
            tolerance=threshold.tolerance,
            stable_digits=threshold.stable_digits,
        )
        cvr_decision = compare_probability(
            recomputed["convert"]["probability"],
            threshold.cvr_threshold,
            policy=threshold.policy,
            tolerance=threshold.tolerance,
            stable_digits=threshold.stable_digits,
        )
        return {
            "request_id": request_id,
            "matches": True,
            "lineage_valid": not invalid_versions,
            "invalid_versions": invalid_versions,
            "replayed": recomputed,
            "contributions": {
                "click": trace["score"]["click"]["contributions"],
                "convert": trace["score"]["convert"]["contributions"],
            },
            "decisions": {
                "click": click_decision,
                "convert": cvr_decision,
            },
            "lineage": {
                "cache_key": trace["cache_key"],
                "feature_spec_version": trace["feature_spec_version"],
                "feature_spec_snapshot_version": trace[
                    "feature_spec_snapshot_version"
                ],
                "feature_spec_checksum": trace["feature_spec_checksum"],
                "statistics_version": trace["statistics_version"],
                "statistics_checksum": trace["statistics_checksum"],
                "node_path": trace["node_path"],
            },
        }

    def replay_latest(self, request_id: str) -> dict[str, Any]:
        if self._state is None:
            fail(E_INVALID_ARGUMENT, "no completed run is available")
        trace = self._state["traces"].get(request_id)
        if trace is None:
            fail(
                E_LINEAGE_BROKEN,
                "request_id has no current score trace",
                request_id=request_id,
            )
        return self.replay_trace(trace)

    def self_check(self, report: RunReport | dict[str, Any] | None = None) -> dict[str, Any]:
        if self._state is None:
            fail(E_INVALID_ARGUMENT, "no completed run is available")
        if report is None:
            data = self._state["report"].data
        elif isinstance(report, RunReport):
            data = report.data
        else:
            data = report
        result = self._self_check_data(
            data,
            self._state["samples"],
            self._state["node_products"],
            self._state["node_runs"],
            list(self._state["traces"].values()),
            self._state["spec"],
            self._state["stats"],
        )
        if not result["ok"]:
            fail(
                E_CACHE_CORRUPT,
                "cross-module self-check failed",
                errors=result["errors"],
                checks=result["checks"],
            )
        return result

    # ------------------------------------------------------------------
    # Deterministic accumulation merge
    # ------------------------------------------------------------------
    def _merge_accumulation_products(
        self, products: list[dict[str, Any]]
    ) -> dict[str, Any]:
        numeric: dict[str, dict[str, Any]] = {}
        categorical: dict[str, dict[str, Any]] = {}
        cross: dict[str, dict[str, Any]] = {}
        for product in products:
            for name, entry in product.get("numeric", {}).items():
                target = numeric.setdefault(
                    name, {"seen": 0, "missing_count": 0, "value_sum": []}
                )
                target["seen"] += int(entry["seen"])
                target["missing_count"] += int(entry["missing_count"])
                target["value_sum"].append(float(entry["value_sum"]))
            for name, entry in product.get("categorical", {}).items():
                target = categorical.setdefault(
                    name, {"seen": 0, "missing_count": 0, "bucket_sum": 0}
                )
                target["seen"] += int(entry["seen"])
                target["missing_count"] += int(entry["missing_count"])
                target["bucket_sum"] += int(entry["bucket_sum"])
            for name, entry in product.get("cross", {}).items():
                target = cross.setdefault(
                    name, {"seen": 0, "missing_count": 0, "value_sum": []}
                )
                target["seen"] += int(entry["seen"])
                target["missing_count"] += int(entry["missing_count"])
                target["value_sum"].append(float(entry["value_sum"]))
        return {
            "protocol": ACCUMULATION_PROTOCOL,
            "row_count": sum(int(product["row_count"]) for product in products),
            "label_click_sum": sum(
                int(product["label_click_sum"]) for product in products
            ),
            "label_convert_sum": sum(
                int(product["label_convert_sum"]) for product in products
            ),
            "click_probability_sum": math.fsum(
                float(product["click_probability_sum"]) for product in products
            ),
            "cvr_probability_sum": math.fsum(
                float(product["cvr_probability_sum"]) for product in products
            ),
            "expected_conversion_sum": math.fsum(
                float(product["expected_conversion_sum"]) for product in products
            ),
            "numeric": {
                name: {
                    "seen": entry["seen"],
                    "missing_count": entry["missing_count"],
                    "value_sum": math.fsum(entry["value_sum"]),
                }
                for name, entry in sorted(numeric.items())
            },
            "categorical": {
                name: {
                    "seen": entry["seen"],
                    "missing_count": entry["missing_count"],
                    "bucket_sum": entry["bucket_sum"],
                }
                for name, entry in sorted(categorical.items())
            },
            "cross": {
                name: {
                    "seen": entry["seen"],
                    "missing_count": entry["missing_count"],
                    "value_sum": math.fsum(entry["value_sum"]),
                }
                for name, entry in sorted(cross.items())
            },
        }


def _changed_flat_paths(old: Any, new: Any) -> list[str]:
    old_flat = _flatten(old)
    new_flat = _flatten(new)
    return sorted(
        path for path in set(old_flat) | set(new_flat) if old_flat.get(path) != new_flat.get(path)
    )


def _spec_affected_features(paths: list[str], spec: FeatureSpec) -> list[str]:
    changed: set[str] = set()
    for path in paths:
        for section, collection in (
            ("numeric", spec.numeric),
            ("categorical", spec.categorical),
            ("cross", spec.cross),
        ):
            prefix = f"{section}["
            if not path.startswith(prefix):
                continue
            try:
                index = int(path[len(prefix) :].split("]", 1)[0])
                item = collection[index]
            except (ValueError, IndexError):
                continue
            if section == "cross":
                changed.add(f"cross.{item.name}")
            else:
                changed.add(f"{section}.{item.name}")
        if path.startswith("statistics["):
            changed.add("statistics")
    return sorted(changed)


def _statistics_affected_features(paths: list[str]) -> list[str]:
    changed: set[str] = set()
    for path in paths:
        if path.startswith("global"):
            changed.add("global")
        elif path.startswith(("numeric.", "categorical.", "cross.")):
            parts = path.split(".")
            if len(parts) >= 2:
                changed.add(f"{parts[0]}.{parts[1]}")
    return sorted(changed)


def _derived_feature_names(
    affected: Iterable[str], spec: FeatureSpec
) -> list[str]:
    base_names = {
        value.split(".", 1)[1]
        for value in affected
        if value.startswith(("numeric.", "categorical."))
    }
    if "global" in affected or "statistics" in affected:
        base_names |= {
            item.name for item in [*spec.numeric, *spec.categorical]
        }
    derived: set[str] = set(affected)
    for item in spec.cross:
        if (
            f"cross.{item.name}" in affected
            or item.left in base_names
            or item.right in base_names
        ):
            derived.update(f"cross.{item.name}.{slot}" for slot in item.slots)
    return sorted(derived)


def _flatten(value: Any, path: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            result.update(_flatten(item, child))
        return result
    if isinstance(value, list):
        result = {}
        for index, item in enumerate(value):
            child = f"{path}[{index}]"
            result.update(_flatten(item, child))
        return result
    return {path: value}


__all__ = [
    "ASSEMBLY_PROTOCOL",
    "ACCUMULATION_PROTOCOL",
    "EngineConfig",
    "RunReport",
    "JointCTREngine",
]

