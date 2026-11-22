"""Incremental OCR service, lineage replay, and cross-module self-check."""

from __future__ import annotations

import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .canonical import (
    canonical_bytes,
    derive_cache_key,
    digest_payloads,
    sha256_json,
    stable_float,
    verify_sealed,
)
from .cache import ArtifactCache, CacheEntry, FeatureMemory, ResourcePolicy
from .errors import (
    E_CACHE_CORRUPT,
    E_CONFIDENCE_CONFLICT,
    E_INCREMENTAL_MISMATCH,
    E_INCONSISTENT,
    E_INVALID_INPUT,
    E_LINEAGE_STALE,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    OK,
    BusinessError,
)
from .models import (
    CONTROLLED_LABELS,
    DistanceMetric,
    ImageSample,
    NormalizationConfig,
    PrototypeSet,
    RecognitionConfig,
    ThresholdConfig,
    validate_unique_samples,
)
from .pipeline import (
    DECISION_NODE,
    DISTANCE_NODE,
    FEATURE_NODE,
    METRICS_NODE,
    build_decision_artifact,
    build_distance_artifact,
    build_feature_artifact,
    calculate_prototype_features,
    compute_distance_value,
    compute_distance_worker,
    compute_feature_worker,
    compute_metrics_artifact,
    decide_from_distances,
    decision_key_parts,
    distance_key_parts,
    extract_feature_values,
    feature_key_parts,
    metrics_key_parts,
    validate_confidence_combination,
)
from .snapshots import SnapshotStore


def feature_node_id(image_id: str) -> str:
    return "feature:{0}".format(image_id)


def distance_node_id(image_id: str, class_id: int) -> str:
    return "distance:{0}:{1}".format(image_id, class_id)


def decision_node_id(image_id: str) -> str:
    return "decision:{0}".format(image_id)


METRICS_NODE_ID = "metrics"


@dataclass
class _ExecutionState:
    requested_workers: int
    allowed_workers: int
    actual_workers: int
    policy: ResourcePolicy
    parallel_used: bool = False
    categories: List[Dict[str, Any]] = field(default_factory=list)

    def degrade(self, category: str, details: Mapping[str, Any]) -> None:
        for existing in self.categories:
            if existing.get("category") == category:
                existing.update(dict(details))
                return
        item = {"category": category}
        item.update(dict(details))
        self.categories.append(item)

    def to_dict(self, memory_stats: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "requested_workers": self.requested_workers,
            "allowed_workers": self.allowed_workers,
            "actual_workers": self.actual_workers,
            "parallel_used": self.parallel_used,
            "degraded": bool(self.categories),
            "degradation_categories": [dict(item) for item in self.categories],
            "feature_memory": dict(memory_stats),
            "resource_policy": self.policy.to_dict(),
        }


@dataclass
class RunResult:
    config: RecognitionConfig
    samples: List[ImageSample]
    decisions: List[Dict[str, Any]]
    feature_artifacts: Dict[str, Dict[str, Any]]
    distance_artifacts: Dict[str, Dict[int, Dict[str, Any]]]
    decision_artifacts: Dict[str, Dict[str, Any]]
    metrics_artifact: Dict[str, Any]
    metrics_snapshot: Dict[str, Any]
    node_keys: Dict[str, Dict[str, Any]]
    affected_nodes: List[str]
    recomputed_nodes: List[str]
    reused_nodes: List[str]
    execution: Dict[str, Any]
    cache_entries: Dict[str, Dict[str, Any]]
    snapshot_metadata: Dict[str, Dict[str, Any]]
    calibration: Optional[Dict[str, Any]] = None

    def to_report(self) -> Dict[str, Any]:
        recomputed = set(self.recomputed_nodes)
        reused = set(self.reused_nodes)
        affected = set(self.affected_nodes)
        all_nodes = set(self.node_keys)
        summary = dict(self.metrics_artifact)
        summary.pop("content_hash", None)
        first_decision = self.decisions[0] if self.decisions else None
        report: Dict[str, Any] = {
            "business_code": OK,
            "degraded": bool(self.execution.get("degraded")),
            "degradation_categories": self.execution.get(
                "degradation_categories", []
            ),
            "labels": list(CONTROLLED_LABELS),
            "config": {
                "normalization": self.config.normalization.to_payload(),
                "prototypes": {
                    "version": self.config.prototypes.version,
                    "checksum": self.config.prototypes.checksum,
                    "member_hashes": self.config.prototypes.member_hashes(),
                },
                "thresholds": self.config.thresholds.to_payload(),
                "distance_metric": self.config.distance_metric.to_payload(),
                "config_checksum": self.config.checksum,
            },
            "execution": self.execution,
            "summary": summary,
            "metrics_snapshot": self.metrics_snapshot,
            "nodes": {
                "affected": sorted(affected),
                "recomputed": sorted(recomputed),
                "reused": sorted(reused),
                "invariants": {
                    "disjoint": not bool(recomputed & reused),
                    "affected_covered": affected <= (recomputed | reused),
                    "all_nodes_covered": all_nodes == (recomputed | reused),
                },
            },
            "cache": {
                "decision_key_example": (
                    {
                        "cache_key": first_decision["cache_key"],
                        "key_parts": first_decision["key_parts"],
                        "artifact_sha256": first_decision["artifact_sha256"],
                    }
                    if first_decision is not None
                    else None
                ),
                "entries_referenced": len(self.cache_entries),
            },
            "decisions": self.decisions,
            "snapshots": self.snapshot_metadata,
        }
        if self.calibration is not None:
            report["calibration"] = self.calibration
        return report


class DigitOCRService:
    """Incremental, content-addressed OCR component."""

    def __init__(
        self,
        config: RecognitionConfig,
        cache_dir: Path,
        resources: Optional[ResourcePolicy] = None,
    ) -> None:
        self.config = config
        self.cache_dir = Path(cache_dir)
        self.resources = resources or ResourcePolicy()
        self.cache = ArtifactCache(self.cache_dir / "nodes")
        self.snapshots = SnapshotStore(self.cache_dir / "snapshots")
        self.memory = FeatureMemory(self.resources)
        self._install_config(config)
        self._last_samples_by_id: Dict[str, ImageSample] = {}
        self.last_result: Optional[RunResult] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def recognize(
        self,
        sample: ImageSample,
        *,
        parallel_workers: int = 1,
    ) -> Dict[str, Any]:
        result = self.recognize_batch(
            [sample], parallel_workers=parallel_workers, update_metrics=True
        )
        return {
            "business_code": OK,
            "degraded": bool(result.execution.get("degraded")),
            "decision": result.decisions[0],
            "nodes": {
                "affected": result.affected_nodes,
                "recomputed": result.recomputed_nodes,
                "reused": result.reused_nodes,
            },
            "metrics_snapshot": result.metrics_snapshot,
        }

    # Stable aliases make the integration surface explicit for callers that
    # prefer the task-domain vocabulary.
    def single_recognize(
        self, sample: ImageSample, *, parallel_workers: int = 1
    ) -> Dict[str, Any]:
        return self.recognize(sample, parallel_workers=parallel_workers)

    def batch_recognize(
        self,
        samples: Sequence[ImageSample],
        *,
        parallel_workers: int = 1,
        update_metrics: bool = True,
    ) -> RunResult:
        return self.recognize_batch(
            samples,
            parallel_workers=parallel_workers,
            update_metrics=update_metrics,
        )

    def recognize_batch(
        self,
        samples: Sequence[ImageSample],
        *,
        parallel_workers: int = 1,
        update_metrics: bool = True,
    ) -> RunResult:
        ordered = validate_unique_samples(samples)
        result = self._run(
            ordered,
            affected=None,
            parallel_workers=parallel_workers,
            update_metrics=update_metrics,
        )
        self._commit_state(result)
        return result

    def calibrate(
        self,
        samples: Sequence[ImageSample],
        new_config: RecognitionConfig,
        *,
        parallel_workers: int = 1,
        verify_full_recompute: bool = True,
        update_metrics: bool = True,
    ) -> RunResult:
        ordered = validate_unique_samples(samples)
        affected = self._semantic_affected_nodes(
            self.config,
            new_config,
            self._last_samples_by_id,
            ordered,
        )
        self._install_config(new_config)
        result = self._run(
            ordered,
            affected=affected,
            parallel_workers=parallel_workers,
            update_metrics=update_metrics,
        )
        if verify_full_recompute:
            result.calibration = self._verify_against_full_recompute(ordered, result)
        self._commit_state(result)
        return result

    def incremental_calibrate(
        self,
        samples: Sequence[ImageSample],
        new_config: RecognitionConfig,
        *,
        parallel_workers: int = 1,
        verify_full_recompute: bool = True,
        update_metrics: bool = True,
    ) -> RunResult:
        return self.calibrate(
            samples,
            new_config,
            parallel_workers=parallel_workers,
            verify_full_recompute=verify_full_recompute,
            update_metrics=update_metrics,
        )

    def replay_decision(
        self,
        decision: Mapping[str, Any],
        *,
        allow_archived: bool = False,
    ) -> Dict[str, Any]:
        return self._replay_decision(decision, allow_archived=allow_archived)

    def replay(
        self,
        decision: Mapping[str, Any],
        *,
        allow_archived: bool = False,
    ) -> Dict[str, Any]:
        return self.replay_decision(decision, allow_archived=allow_archived)

    def self_check(self, result: Optional[RunResult] = None) -> Dict[str, Any]:
        target = result or self.last_result
        if target is None:
            raise BusinessError(
                E_INCONSISTENT,
                "there is no committed result to self-check",
                category="self_check_state",
            )
        return self._self_check(target)

    def run_self_check(self, result: Optional[RunResult] = None) -> Dict[str, Any]:
        return self.self_check(result)

    # ------------------------------------------------------------------
    # Snapshot installation
    # ------------------------------------------------------------------
    def _snapshot_entries(
        self, config: RecognitionConfig
    ) -> List[Tuple[str, int, Mapping[str, Any]]]:
        return [
            ("normalization", config.normalization.version, config.normalization.to_payload()),
            ("prototypes", config.prototypes.version, config.prototypes.to_payload()),
            ("thresholds", config.thresholds.version, config.thresholds.to_payload()),
            (
                "distance_metric",
                config.distance_metric.version,
                config.distance_metric.to_payload(),
            ),
        ]

    def _install_config(self, config: RecognitionConfig) -> None:
        entries = self._snapshot_entries(config)
        for name, version, payload in entries:
            self.snapshots.validate_replacement(name, version, payload)
        for name, version, payload in entries:
            self.snapshots.replace(name, version, payload)
        self.config = config

    def _record_metrics_snapshot(
        self, metrics_artifact: Mapping[str, Any]
    ) -> Dict[str, Any]:
        payload = {
            "kind": "metrics_snapshot",
            "metrics": dict(metrics_artifact),
        }
        current = self.snapshots.current("metrics")
        if current is not None and canonical_bytes(current.payload) == canonical_bytes(
            payload
        ):
            record = current
        else:
            record = self.snapshots.append("metrics", payload)
        return {
            "name": "metrics",
            "version": record.version,
            "checksum": record.checksum,
            "data": dict(metrics_artifact),
        }

    # ------------------------------------------------------------------
    # Execution plan
    # ------------------------------------------------------------------
    def _resolve_execution(self, requested_workers: int) -> _ExecutionState:
        if not isinstance(requested_workers, int) or requested_workers < 1:
            raise BusinessError(
                E_INVALID_INPUT,
                "parallel_workers must be an integer >= 1",
                category="parallelism",
            )
        actual = requested_workers
        categories: List[Dict[str, Any]] = []
        if requested_workers > self.resources.max_parallel_nodes:
            if not self.resources.allow_parallel_degrade:
                raise BusinessError(
                    E_RESOURCE_LIMIT,
                    "requested parallel nodes exceed the configured limit",
                    category="parallelism",
                    details={
                        "requested_workers": requested_workers,
                        "max_parallel_nodes": self.resources.max_parallel_nodes,
                    },
                )
            actual = 1
            categories.append(
                {
                    "category": "parallelism",
                    "requested_workers": requested_workers,
                    "max_parallel_nodes": self.resources.max_parallel_nodes,
                    "applied_mode": "serial",
                }
            )
        state = _ExecutionState(
            requested_workers=requested_workers,
            allowed_workers=self.resources.max_parallel_nodes,
            actual_workers=actual,
            policy=self.resources,
        )
        state.categories.extend(categories)
        return state

    def _execute_tasks(
        self,
        worker: Any,
        tasks: Sequence[Mapping[str, Any]],
        execution: _ExecutionState,
    ) -> List[Dict[str, Any]]:
        ordered_tasks = sorted(tasks, key=lambda task: str(task["cache_key"]))
        if not ordered_tasks:
            return []
        if execution.actual_workers > 1 and len(ordered_tasks) > 1:
            context = mp.get_context("spawn")
            with ProcessPoolExecutor(
                max_workers=execution.actual_workers, mp_context=context
            ) as executor:
                results = list(executor.map(worker, ordered_tasks))
            execution.parallel_used = True
            return results
        return [worker(task) for task in ordered_tasks]

    def _run(
        self,
        samples: Sequence[ImageSample],
        *,
        affected: Optional[Set[str]],
        parallel_workers: int,
        update_metrics: bool,
    ) -> RunResult:
        for sample in samples:
            if (
                sample.height != self.config.normalization.canvas_height
                or sample.width != self.config.normalization.canvas_width
            ):
                raise BusinessError(
                    E_INVALID_INPUT,
                    "sample canvas does not match normalization snapshot",
                    image_id=sample.image_id,
                    category="sample_canvas",
                )
        execution = self._resolve_execution(parallel_workers)
        memory_before = self.memory.stats()
        recomputed: Set[str] = set()
        reused: Set[str] = set()
        node_keys: Dict[str, Dict[str, Any]] = {}
        cache_entries: Dict[str, Dict[str, Any]] = {}
        feature_by_image: Dict[str, Dict[str, Any]] = {}
        distance_by_image: Dict[str, Dict[int, Dict[str, Any]]] = {}
        decision_by_image: Dict[str, Dict[str, Any]] = {}

        # Feature node -------------------------------------------------
        feature_tasks: List[Dict[str, Any]] = []
        feature_task_parts: Dict[str, Mapping[str, Any]] = {}
        for sample in samples:
            parts = feature_key_parts(sample, self.config.normalization)
            key = derive_cache_key(parts)
            ref = feature_node_id(sample.image_id)
            node_keys[ref] = {"cache_key": key, "key_parts": parts}
            artifact = self.memory.get(key)
            entry: Optional[CacheEntry] = None
            if artifact is None:
                entry = self.cache.load(FEATURE_NODE, key)
                if entry is not None:
                    artifact = entry.artifact
                    self.memory.put(key, artifact, image_id=sample.image_id)
            if artifact is not None:
                if entry is None:
                    cache_entries[ref] = {
                        "cache_key": key,
                        "key_parts": dict(parts),
                        "artifact_sha256": sha256_json(artifact),
                    }
                else:
                    cache_entries[ref] = {
                        "cache_key": key,
                        "key_parts": dict(parts),
                        "artifact_sha256": entry.artifact_sha256,
                    }
                feature_by_image[sample.image_id] = dict(artifact)
                reused.add(ref)
            else:
                task = {
                    "cache_key": key,
                    "image_id": sample.image_id,
                    "seed": sample.seed,
                    "label": sample.label,
                    "uncertain": sample.uncertain,
                    "pixels": sample.pixels,
                    "height": sample.height,
                    "width": sample.width,
                    "normalization": self.config.normalization.to_payload(),
                }
                feature_tasks.append(task)
                feature_task_parts[key] = parts
                recomputed.add(ref)

        for computed in self._execute_tasks(
            compute_feature_worker, feature_tasks, execution
        ):
            key = str(computed["cache_key"])
            artifact = computed["artifact"]
            parts = feature_task_parts[key]
            entry = self.cache.store(FEATURE_NODE, key, parts, artifact)
            self.memory.put(key, artifact, image_id=str(artifact["image_id"]))
            image_id = str(artifact["image_id"])
            ref = feature_node_id(image_id)
            feature_by_image[image_id] = dict(artifact)
            cache_entries[ref] = {
                "cache_key": key,
                "key_parts": dict(parts),
                "artifact_sha256": entry.artifact_sha256,
            }

        if any(sample.image_id not in feature_by_image for sample in samples):
            missing = next(
                sample.image_id
                for sample in samples
                if sample.image_id not in feature_by_image
            )
            raise BusinessError(
                E_INCONSISTENT,
                "feature node did not produce an artifact",
                image_id=missing,
                category="feature_coverage",
            )

        # Distance nodes ----------------------------------------------
        prototype_features = calculate_prototype_features(self.config)
        distance_tasks: List[Dict[str, Any]] = []
        distance_task_parts: Dict[str, Mapping[str, Any]] = {}
        for sample in samples:
            feature = feature_by_image[sample.image_id]
            feature_hash = str(feature["content_hash"])
            class_artifacts: Dict[int, Dict[str, Any]] = {}
            for class_id in CONTROLLED_LABELS:
                parts = distance_key_parts(
                    sample, self.config, class_id, feature_hash
                )
                key = derive_cache_key(parts)
                ref = distance_node_id(sample.image_id, class_id)
                node_keys[ref] = {"cache_key": key, "key_parts": parts}
                entry = self.cache.load(DISTANCE_NODE, key)
                if entry is not None:
                    artifact = dict(entry.artifact)
                    self._validate_distance_artifact(
                        artifact,
                        sample.image_id,
                        class_id,
                        feature_hash,
                        self.config.prototypes.member(class_id).checksum,
                    )
                    class_artifacts[class_id] = artifact
                    reused.add(ref)
                    cache_entries[ref] = {
                        "cache_key": key,
                        "key_parts": dict(parts),
                        "artifact_sha256": entry.artifact_sha256,
                    }
                else:
                    task = {
                        "cache_key": key,
                        "image_id": sample.image_id,
                        "seed": sample.seed,
                        "label": sample.label,
                        "uncertain": sample.uncertain,
                        "pixels": sample.pixels,
                        "height": sample.height,
                        "width": sample.width,
                        "normalization": self.config.normalization.to_payload(),
                        "prototype": self.config.prototypes.member(class_id).to_payload(),
                        "distance_metric": self.config.distance_metric.to_payload(),
                        "class_id": class_id,
                        "feature_values": list(feature["values"]),
                        "prototype_values": prototype_features[class_id],
                        "feature_content_hash": feature_hash,
                    }
                    distance_tasks.append(task)
                    distance_task_parts[key] = parts
                    recomputed.add(ref)
            distance_by_image[sample.image_id] = class_artifacts

        for computed in self._execute_tasks(
            compute_distance_worker, distance_tasks, execution
        ):
            key = str(computed["cache_key"])
            artifact = computed["artifact"]
            parts = distance_task_parts[key]
            entry = self.cache.store(DISTANCE_NODE, key, parts, artifact)
            image_id = str(artifact["image_id"])
            class_id = int(artifact["class_id"])
            ref = distance_node_id(image_id, class_id)
            distance_by_image[image_id][class_id] = dict(artifact)
            cache_entries[ref] = {
                "cache_key": key,
                "key_parts": dict(parts),
                "artifact_sha256": entry.artifact_sha256,
            }

        for sample in samples:
            if set(distance_by_image.get(sample.image_id, {})) != set(CONTROLLED_LABELS):
                raise BusinessError(
                    E_INCONSISTENT,
                    "distance node set is incomplete",
                    image_id=sample.image_id,
                    category="distance_coverage",
                )

        # Decision nodes ----------------------------------------------
        for sample in samples:
            feature = feature_by_image[sample.image_id]
            feature_hash = str(feature["content_hash"])
            distances = distance_by_image[sample.image_id]
            distance_hashes = {
                class_id: str(distances[class_id]["content_hash"])
                for class_id in CONTROLLED_LABELS
            }
            parts = decision_key_parts(
                sample, self.config, feature_hash, distance_hashes
            )
            key = derive_cache_key(parts)
            ref = decision_node_id(sample.image_id)
            node_keys[ref] = {"cache_key": key, "key_parts": parts}
            entry = self.cache.load(DECISION_NODE, key)
            if entry is not None:
                artifact = dict(entry.artifact)
                self._validate_decision_artifact(artifact, sample.image_id)
                decision_by_image[sample.image_id] = artifact
                reused.add(ref)
                cache_entries[ref] = {
                    "cache_key": key,
                    "key_parts": dict(parts),
                    "artifact_sha256": entry.artifact_sha256,
                }
            else:
                distance_keys = {
                    class_id: node_keys[
                        distance_node_id(sample.image_id, class_id)
                    ]["cache_key"]
                    for class_id in CONTROLLED_LABELS
                }
                feature_key = node_keys[feature_node_id(sample.image_id)][
                    "cache_key"
                ]
                artifact = build_decision_artifact(
                    sample,
                    self.config,
                    feature_hash,
                    distances,
                    key,
                    feature_key,
                    distance_keys,
                )
                self._validate_decision_artifact(artifact, sample.image_id)
                entry = self.cache.store(DECISION_NODE, key, parts, artifact)
                decision_by_image[sample.image_id] = dict(artifact)
                recomputed.add(ref)
                cache_entries[ref] = {
                    "cache_key": key,
                    "key_parts": dict(parts),
                    "artifact_sha256": entry.artifact_sha256,
                }

        # Metrics node ------------------------------------------------
        decision_list = [
            decision_by_image[sample.image_id] for sample in samples
        ]
        metrics_parts = metrics_key_parts(decision_list, samples)
        metrics_key = derive_cache_key(metrics_parts)
        node_keys[METRICS_NODE_ID] = {
            "cache_key": metrics_key,
            "key_parts": metrics_parts,
        }
        metrics_entry = self.cache.load(METRICS_NODE, metrics_key)
        if metrics_entry is not None:
            metrics_artifact = dict(metrics_entry.artifact)
            reused.add(METRICS_NODE_ID)
            cache_entries[METRICS_NODE_ID] = {
                "cache_key": metrics_key,
                "key_parts": dict(metrics_parts),
                "artifact_sha256": metrics_entry.artifact_sha256,
            }
        else:
            metrics_artifact = compute_metrics_artifact(decision_list, samples)
            metrics_entry = self.cache.store(
                METRICS_NODE, metrics_key, metrics_parts, metrics_artifact
            )
            recomputed.add(METRICS_NODE_ID)
            cache_entries[METRICS_NODE_ID] = {
                "cache_key": metrics_key,
                "key_parts": dict(metrics_parts),
                "artifact_sha256": metrics_entry.artifact_sha256,
            }

        if update_metrics:
            metrics_snapshot = self._record_metrics_snapshot(metrics_artifact)
        else:
            current = self.snapshots.current("metrics")
            metrics_snapshot = {
                "name": "metrics",
                "version": current.version if current is not None else 0,
                "checksum": current.checksum if current is not None else "",
                "data": dict(metrics_artifact),
            }

        memory_after = self.memory.stats()
        eviction_delta = int(memory_after["evictions"]) - int(
            memory_before["evictions"]
        )
        if eviction_delta > 0:
            execution.degrade(
                "feature_memory",
                {
                    "policy": self.resources.memory_policy,
                    "evictions": eviction_delta,
                    "max_feature_items": self.resources.max_feature_items,
                    "max_feature_bytes": self.resources.max_feature_bytes,
                },
            )
        run_memory_stats = dict(memory_after)
        run_memory_stats["evictions_this_run"] = eviction_delta
        execution_dict = execution.to_dict(run_memory_stats)
        if update_metrics:
            execution_dict["metrics_updated"] = True
        else:
            execution_dict["metrics_updated"] = False

        if recomputed & reused:
            raise BusinessError(
                E_INCONSISTENT,
                "a node was both recomputed and reused",
                category="node_partition",
                details={"intersection": sorted(recomputed & reused)[:3]},
            )
        all_node_ids = set(node_keys)
        if not all_node_ids <= (recomputed | reused):
            raise BusinessError(
                E_INCONSISTENT,
                "node partition does not cover every current node",
                category="node_coverage",
            )
        if affected is None:
            affected = set(all_node_ids)
        if not set(affected) <= (recomputed | reused):
            missing = sorted(set(affected) - (recomputed | reused))
            raise BusinessError(
                E_INCONSISTENT,
                "affected node coverage is incomplete",
                category="node_coverage",
                details={"missing": missing[:5]},
            )

        decision_reports = [
            self._decision_report(
                sample,
                decision_by_image[sample.image_id],
                node_keys[decision_node_id(sample.image_id)],
                cache_entries[decision_node_id(sample.image_id)],
            )
            for sample in samples
        ]
        result = RunResult(
            config=self.config,
            samples=list(samples),
            decisions=decision_reports,
            feature_artifacts=feature_by_image,
            distance_artifacts=distance_by_image,
            decision_artifacts=decision_by_image,
            metrics_artifact=dict(metrics_artifact),
            metrics_snapshot=metrics_snapshot,
            node_keys=node_keys,
            affected_nodes=sorted(affected),
            recomputed_nodes=sorted(recomputed),
            reused_nodes=sorted(reused),
            execution=execution_dict,
            cache_entries=cache_entries,
            snapshot_metadata=self.snapshots.all_current_metadata(),
        )
        self.last_result = result
        return result

    def _commit_state(self, result: RunResult) -> None:
        self._last_samples_by_id = {
            sample.image_id: sample for sample in result.samples
        }

    # ------------------------------------------------------------------
    # Artifact validation and report shaping
    # ------------------------------------------------------------------
    def _validate_distance_artifact(
        self,
        artifact: Mapping[str, Any],
        image_id: str,
        class_id: int,
        feature_hash: str,
        prototype_hash: str,
    ) -> None:
        if (
            artifact.get("image_id") != image_id
            or int(artifact.get("class_id", -1)) != class_id
            or artifact.get("feature_content_hash") != feature_hash
            or artifact.get("prototype_member_hash") != prototype_hash
            or artifact.get("kind") != DISTANCE_NODE
        ):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "distance artifact lineage does not match its inputs",
                image_id=image_id,
                category="distance_lineage",
                details={"class_id": class_id},
            )

    def _validate_decision_artifact(
        self, artifact: Mapping[str, Any], image_id: str
    ) -> None:
        if artifact.get("image_id") != image_id or artifact.get("kind") != DECISION_NODE:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "decision artifact identity is invalid",
                image_id=image_id,
                category="decision_identity",
            )
        predicted = artifact.get("predicted_class")
        if predicted not in CONTROLLED_LABELS:
            raise BusinessError(
                E_INCONSISTENT,
                "decision prediction is outside controlled labels",
                image_id=image_id,
                category="label_domain",
            )
        validate_confidence_combination(
            rejected=bool(artifact.get("rejected")),
            confidence=str(artifact.get("confidence")),
            image_id=image_id,
        )

    def _decision_report(
        self,
        sample: ImageSample,
        artifact: Mapping[str, Any],
        key_record: Mapping[str, Any],
        cache_entry: Mapping[str, Any],
    ) -> Dict[str, Any]:
        report = dict(artifact)
        report["label"] = sample.label
        report["uncertain"] = sample.uncertain
        report["correct"] = int(artifact["predicted_class"]) == sample.label
        report["business_code"] = OK
        report["cache_key"] = key_record["cache_key"]
        report["key_parts"] = dict(key_record["key_parts"])
        report["artifact_sha256"] = cache_entry["artifact_sha256"]
        report["distance_cache_keys"] = dict(artifact["node_path"]["distances"])
        report["lineage"] = {
            "feature_version": artifact["feature_version"],
            "feature_cache_key": artifact["feature_cache_key"],
            "normalization": {
                "version": artifact["normalization_version"],
                "checksum": artifact["normalization_checksum"],
            },
            "prototype_set": {
                "version": artifact["prototype_set_version"],
                "checksum": artifact["prototype_set_checksum"],
            },
            "threshold": {
                "version": artifact["threshold_version"],
                "checksum": artifact["threshold_checksum"],
            },
            "distance_metric": dict(artifact["distance_metric"]),
            "distance_versions": dict(artifact["distance_content_hashes"]),
            "node_path": dict(artifact["node_path"]),
        }
        return report

    # ------------------------------------------------------------------
    # Incremental dependency propagation
    # ------------------------------------------------------------------
    def _semantic_affected_nodes(
        self,
        old_config: RecognitionConfig,
        new_config: RecognitionConfig,
        old_samples_by_id: Mapping[str, ImageSample],
        samples: Sequence[ImageSample],
    ) -> Set[str]:
        affected: Set[str] = set()
        normalization_changed = (
            old_config.normalization.checksum != new_config.normalization.checksum
        )
        metric_changed = (
            old_config.distance_metric.checksum != new_config.distance_metric.checksum
        )
        threshold_changed = (
            old_config.thresholds.checksum != new_config.thresholds.checksum
        )
        prototype_set_changed = (
            old_config.prototypes.version != new_config.prototypes.version
            or old_config.prototypes.checksum != new_config.prototypes.checksum
        )
        old_member_hashes = old_config.prototypes.member_hashes()
        new_member_hashes = new_config.prototypes.member_hashes()
        changed_prototype_classes = {
            class_id
            for class_id in CONTROLLED_LABELS
            if old_member_hashes[str(class_id)] != new_member_hashes[str(class_id)]
        }
        metrics_affected = set(old_samples_by_id) != {
            sample.image_id for sample in samples
        }
        for sample in samples:
            old_sample = old_samples_by_id.get(sample.image_id)
            sample_feature_changed = (
                old_sample is None
                or old_sample.image_sha256 != sample.image_sha256
                or old_sample.seed != sample.seed
                or old_sample.height != sample.height
                or old_sample.width != sample.width
            )
            feature_changed = normalization_changed or sample_feature_changed
            if feature_changed:
                affected.add(feature_node_id(sample.image_id))
                for class_id in CONTROLLED_LABELS:
                    affected.add(distance_node_id(sample.image_id, class_id))
                affected.add(decision_node_id(sample.image_id))
                metrics_affected = True
                continue
            distance_changed = metric_changed or bool(changed_prototype_classes)
            if metric_changed:
                for class_id in CONTROLLED_LABELS:
                    affected.add(distance_node_id(sample.image_id, class_id))
            else:
                for class_id in changed_prototype_classes:
                    affected.add(distance_node_id(sample.image_id, class_id))
            decision_changed = (
                metric_changed
                or threshold_changed
                or prototype_set_changed
                or distance_changed
            )
            if decision_changed:
                affected.add(decision_node_id(sample.image_id))
                metrics_affected = True
            if old_sample is not None:
                if (
                    old_sample.label != sample.label
                    or old_sample.uncertain != sample.uncertain
                ):
                    metrics_affected = True
        if metrics_affected:
            affected.add(METRICS_NODE_ID)
        return affected

    # ------------------------------------------------------------------
    # Full by-reference recomputation
    # ------------------------------------------------------------------
    def _scratch_decisions(
        self, samples: Sequence[ImageSample], config: RecognitionConfig
    ) -> Dict[str, Dict[str, Any]]:
        prototype_features = calculate_prototype_features(config)
        result: Dict[str, Dict[str, Any]] = {}
        for sample in samples:
            feature_values = extract_feature_values(sample, config.normalization)
            feature_artifact = build_feature_artifact(
                sample, config.normalization, feature_values
            )
            feature_key = derive_cache_key(
                feature_key_parts(sample, config.normalization)
            )
            feature_hash = str(feature_artifact["content_hash"])
            distance_artifacts: Dict[int, Dict[str, Any]] = {}
            distance_keys: Dict[int, str] = {}
            for class_id in CONTROLLED_LABELS:
                prototype = config.prototypes.member(class_id)
                distance = compute_distance_value(
                    feature_values,
                    prototype_features[class_id],
                    config.distance_metric,
                )
                distance_artifact = build_distance_artifact(
                    sample,
                    config.normalization,
                    prototype,
                    config.distance_metric,
                    feature_hash,
                    distance,
                )
                distance_artifacts[class_id] = distance_artifact
                distance_keys[class_id] = derive_cache_key(
                    distance_key_parts(sample, config, class_id, feature_hash)
                )
            distance_hashes = {
                class_id: str(distance_artifacts[class_id]["content_hash"])
                for class_id in CONTROLLED_LABELS
            }
            decision_key = derive_cache_key(
                decision_key_parts(sample, config, feature_hash, distance_hashes)
            )
            result[sample.image_id] = build_decision_artifact(
                sample,
                config,
                feature_hash,
                distance_artifacts,
                decision_key,
                feature_key,
                distance_keys,
            )
        return result

    def _verify_against_full_recompute(
        self, samples: Sequence[ImageSample], result: RunResult
    ) -> Dict[str, Any]:
        scratch = self._scratch_decisions(samples, result.config)
        first_mismatch: Optional[str] = None
        all_image_ids = set(result.decision_artifacts) | set(scratch)
        for image_id in sorted(all_image_ids):
            incremental = result.decision_artifacts.get(image_id)
            full = scratch.get(image_id)
            if incremental is None or full is None:
                first_mismatch = image_id
                break
            if canonical_bytes(incremental) != canonical_bytes(full):
                first_mismatch = image_id
                break
        incremental_digest = digest_payloads(result.decision_artifacts)
        full_digest = digest_payloads(scratch)
        if first_mismatch is not None or incremental_digest != full_digest:
            raise BusinessError(
                E_INCREMENTAL_MISMATCH,
                "incremental decisions differ from full recomputation",
                image_id=first_mismatch,
                category="incremental_vs_full",
                details={
                    "incremental_digest": incremental_digest,
                    "full_digest": full_digest,
                },
            )
        return {
            "business_code": OK,
            "mode": "full_recompute_by_reference",
            "byte_identical": True,
            "first_mismatch_image_id": None,
            "incremental_digest": incremental_digest,
            "full_digest": full_digest,
        }

    # ------------------------------------------------------------------
    # Lineage replay
    # ------------------------------------------------------------------
    def _replay_decision(
        self, decision: Mapping[str, Any], *, allow_archived: bool
    ) -> Dict[str, Any]:
        image_id = str(decision.get("image_id", ""))
        if not image_id:
            raise BusinessError(
                E_INCONSISTENT,
                "decision record has no image_id",
                category="replay_record",
            )
        try:
            feature_key = str(decision["feature_cache_key"])
            feature_version = str(decision["feature_version"])
            norm_version = int(decision["normalization_version"])
            norm_checksum = str(decision["normalization_checksum"])
            proto_version = int(decision["prototype_set_version"])
            proto_checksum = str(decision["prototype_set_checksum"])
            threshold_version = int(decision["threshold_version"])
            threshold_checksum = str(decision["threshold_checksum"])
            metric_record = dict(decision["distance_metric"])
            recorded_distances = {
                int(key): float(value)
                for key, value in dict(decision["distances"]).items()
            }
            recorded_hashes = {
                int(key): str(value)
                for key, value in dict(decision["distance_content_hashes"]).items()
            }
            node_path = dict(decision["node_path"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BusinessError(
                E_INCONSISTENT,
                "decision record is missing replay fields: {0}".format(exc),
                image_id=image_id,
                category="replay_record",
            )

        current_versions = {
            "normalization": (
                self.config.normalization.version,
                self.config.normalization.checksum,
            ),
            "prototype_set": (
                self.config.prototypes.version,
                self.config.prototypes.checksum,
            ),
            "threshold": (
                self.config.thresholds.version,
                self.config.thresholds.checksum,
            ),
            "distance_metric": (
                self.config.distance_metric.version,
                self.config.distance_metric.checksum,
            ),
        }
        recorded_versions = {
            "normalization": (norm_version, norm_checksum),
            "prototype_set": (proto_version, proto_checksum),
            "threshold": (threshold_version, threshold_checksum),
            "distance_metric": (
                int(metric_record["version"]),
                str(metric_record["checksum"]),
            ),
        }
        stale_versions: Dict[str, Dict[str, Any]] = {}
        for name in current_versions:
            recorded_version, recorded_checksum = recorded_versions[name]
            current_version, current_checksum = current_versions[name]
            if (
                recorded_version != current_version
                or recorded_checksum != current_checksum
            ):
                stale_versions[name] = {
                    "recorded_version": recorded_version,
                    "recorded_checksum": recorded_checksum,
                    "current_version": current_version,
                    "current_checksum": current_checksum,
                }
        if stale_versions and not allow_archived:
            raise BusinessError(
                E_LINEAGE_STALE,
                "decision references stale dependency versions",
                image_id=image_id,
                category="lineage",
                details={"stale_versions": stale_versions},
            )

        if stale_versions:
            config = self._archived_config(
                norm_version,
                norm_checksum,
                proto_version,
                proto_checksum,
                threshold_version,
                threshold_checksum,
                int(metric_record["version"]),
                str(metric_record["checksum"]),
                image_id,
            )
        else:
            config = self.config

        feature_entry = self.cache.load(FEATURE_NODE, feature_key)
        if feature_entry is None:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "archived feature needed for replay is unavailable",
                image_id=image_id,
                category="replay_feature",
            )
        feature = feature_entry.artifact
        if (
            feature.get("image_id") != image_id
            or feature.get("content_hash") != feature_version
        ):
            raise BusinessError(
                E_INCONSISTENT,
                "replay feature lineage does not match the decision",
                image_id=image_id,
                category="replay_feature",
            )

        prototype_features = calculate_prototype_features(config)
        distance_values: Dict[int, float] = {}
        for class_id in CONTROLLED_LABELS:
            distance_values[class_id] = compute_distance_value(
                feature["values"],
                prototype_features[class_id],
                config.distance_metric,
            )
            recorded = stable_float(recorded_distances[class_id])
            if distance_values[class_id] != recorded:
                raise BusinessError(
                    E_INCONSISTENT,
                    "replayed distance differs from the recorded value",
                    image_id=image_id,
                    category="replay_distance",
                    details={"class_id": class_id},
                )
            distance_key = str(
                node_path.get("distances", {}).get(str(class_id), "")
            )
            distance_entry = self.cache.load(DISTANCE_NODE, distance_key)
            if (
                distance_entry is None
                or distance_entry.artifact.get("content_hash")
                != recorded_hashes[class_id]
            ):
                raise BusinessError(
                    E_INCONSISTENT,
                    "replayed distance cache hash differs from the lineage",
                    image_id=image_id,
                    category="replay_distance_hash",
                    details={"class_id": class_id},
                )
            if stable_float(distance_entry.artifact["distance"]) != distance_values[class_id]:
                raise BusinessError(
                    E_INCONSISTENT,
                    "distance cache value differs from independent replay",
                    image_id=image_id,
                    category="replay_distance",
                    details={"class_id": class_id},
                )

        replayed = decide_from_distances(
            distance_values, config.thresholds, image_id=image_id
        )
        comparisons = {
            "predicted_class": int(decision["predicted_class"]),
            "rejected": bool(decision["rejected"]),
            "confidence": str(decision["confidence"]),
        }
        for field_name in ("predicted_class", "rejected", "confidence"):
            if replayed[field_name] != comparisons[field_name]:
                raise BusinessError(
                    E_INCONSISTENT,
                    "replay changes the recorded decision outcome",
                    image_id=image_id,
                    category="replay_decision",
                    details={
                        "field": field_name,
                        "recorded": comparisons[field_name],
                        "replayed": replayed[field_name],
                    },
                )
        return {
            "business_code": OK,
            "image_id": image_id,
            "replayed": True,
            "used_archived": bool(stale_versions),
            "lineage_matches_current": not bool(stale_versions),
            "stale_versions": stale_versions,
            "predicted_class": replayed["predicted_class"],
            "rejected": replayed["rejected"],
            "confidence": replayed["confidence"],
            "best_distance": replayed["best_distance"],
            "source_decision_cache_key": decision.get("cache_key"),
            "verified_node_path": node_path,
        }

    def _archived_config(
        self,
        norm_version: int,
        norm_checksum: str,
        proto_version: int,
        proto_checksum: str,
        threshold_version: int,
        threshold_checksum: str,
        metric_version: int,
        metric_checksum: str,
        image_id: str,
    ) -> RecognitionConfig:
        try:
            normalization = NormalizationConfig.from_payload(
                self.snapshots.load("normalization", norm_version).payload
            )
            prototypes = PrototypeSet.from_payload(
                self.snapshots.load("prototypes", proto_version).payload
            )
            thresholds = ThresholdConfig.from_payload(
                self.snapshots.load("thresholds", threshold_version).payload
            )
            metric = DistanceMetric.from_payload(
                self.snapshots.load("distance_metric", metric_version).payload
            )
        except BusinessError as exc:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "an archived snapshot needed for replay is unavailable or corrupt",
                image_id=image_id,
                snapshot_name=exc.snapshot_name,
                category="replay_snapshot",
            )
        if (
            normalization.checksum != norm_checksum
            or prototypes.checksum != proto_checksum
            or thresholds.checksum != threshold_checksum
            or metric.checksum != metric_checksum
        ):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "archived snapshot content does not match recorded checksums",
                image_id=image_id,
                category="replay_snapshot_checksum",
            )
        return RecognitionConfig(
            normalization=normalization,
            prototypes=prototypes,
            thresholds=thresholds,
            distance_metric=metric,
        )

    # ------------------------------------------------------------------
    # Cross-module self-check
    # ------------------------------------------------------------------
    def _self_check(self, result: RunResult) -> Dict[str, Any]:
        try:
            self._self_check_impl(result)
            cache_stats = self.cache.verify_all()
        except BusinessError as exc:
            if exc.code == E_INCONSISTENT:
                raise
            if exc.code == E_CONFIDENCE_CONFLICT:
                raise BusinessError(
                    E_INCONSISTENT,
                    "confidence validation failed during self-check",
                    image_id=exc.image_id,
                    category="confidence_combination",
                    details={"source_code": exc.code},
                )
            raise BusinessError(
                E_INCONSISTENT,
                "self-check dependency failed: {0}".format(exc.message),
                image_id=exc.image_id,
                category="cache_integrity",
                details={"source_code": exc.code},
            )
        return {
            "business_code": OK,
            "checks": {
                "image_id_sets": True,
                "controlled_label_domain": True,
                "distance_recompute": True,
                "decision_recompute": True,
                "confidence_combinations": True,
                "node_partition": True,
                "affected_node_coverage": True,
                "metrics_recompute": True,
                "cache_integrity": True,
            },
            "cache_verified_entries": cache_stats["verified_entries"],
        }

    def _inconsistent(
        self,
        category: str,
        message: str,
        *,
        image_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        raise BusinessError(
            E_INCONSISTENT,
            message,
            image_id=image_id,
            category=category,
            details=details,
        )

    def _self_check_impl(self, result: RunResult) -> None:
        sample_ids = {sample.image_id for sample in result.samples}
        feature_ids = set(result.feature_artifacts)
        distance_ids = set(result.distance_artifacts)
        if len(result.decisions) != len(
            {str(item["image_id"]) for item in result.decisions}
        ):
            self._inconsistent(
                "decision_duplicate",
                "decision reports contain duplicate image_id values",
            )
        decision_ids = {str(item["image_id"]) for item in result.decisions}
        if not (sample_ids == feature_ids == distance_ids == decision_ids):
            union = sample_ids | feature_ids | distance_ids | decision_ids
            offender = next(
                (
                    image_id
                    for image_id in sorted(union)
                    if not (
                        image_id in sample_ids
                        and image_id in feature_ids
                        and image_id in distance_ids
                        and image_id in decision_ids
                    )
                ),
                next(iter(sorted(union)), None),
            )
            self._inconsistent(
                "image_id_sets",
                "pipeline nodes do not process the same image_id set",
                image_id=offender,
            )
        if not verify_sealed(result.metrics_artifact):
            self._inconsistent("metrics_hash", "metrics artifact is not sealed")

        decision_reports = {
            str(item["image_id"]): item for item in result.decisions
        }
        prototype_features = calculate_prototype_features(result.config)
        for sample in result.samples:
            image_id = sample.image_id
            feature = result.feature_artifacts[image_id]
            if not verify_sealed(feature) or feature.get("image_id") != image_id:
                self._inconsistent(
                    "feature_identity",
                    "feature artifact identity or hash is invalid",
                    image_id=image_id,
                )
            distances = result.distance_artifacts[image_id]
            if set(distances) != set(CONTROLLED_LABELS):
                self._inconsistent(
                    "distance_labels",
                    "distance artifacts do not cover the controlled label set",
                    image_id=image_id,
                )
            independent_distances: Dict[int, float] = {}
            for class_id in CONTROLLED_LABELS:
                artifact = distances[class_id]
                if not verify_sealed(artifact):
                    self._inconsistent(
                        "distance_hash",
                        "distance artifact is not sealed",
                        image_id=image_id,
                        details={"class_id": class_id},
                    )
                if (
                    artifact.get("image_id") != image_id
                    or int(artifact.get("class_id", -1)) != class_id
                    or artifact.get("feature_content_hash")
                    != feature.get("content_hash")
                    or artifact.get("prototype_member_hash")
                    != result.config.prototypes.member(class_id).checksum
                ):
                    self._inconsistent(
                        "distance_lineage",
                        "distance artifact lineage is inconsistent",
                        image_id=image_id,
                        details={"class_id": class_id},
                    )
                independently = compute_distance_value(
                    feature["values"],
                    prototype_features[class_id],
                    result.config.distance_metric,
                )
                if independently != stable_float(artifact["distance"]):
                    self._inconsistent(
                        "distance_recompute",
                        "distance cannot be reproduced from feature and prototype",
                        image_id=image_id,
                        details={"class_id": class_id},
                    )
                independent_distances[class_id] = independently

            decision = result.decision_artifacts[image_id]
            report = decision_reports[image_id]
            if not verify_sealed(decision):
                self._inconsistent(
                    "decision_hash", "decision artifact is not sealed", image_id=image_id
                )
            predicted = decision.get("predicted_class")
            if predicted not in CONTROLLED_LABELS:
                self._inconsistent(
                    "label_domain",
                    "prediction is outside the controlled label set",
                    image_id=image_id,
                )
            validate_confidence_combination(
                rejected=bool(decision.get("rejected")),
                confidence=str(decision.get("confidence")),
                image_id=image_id,
            )
            expected = decide_from_distances(
                independent_distances,
                result.config.thresholds,
                image_id=image_id,
            )
            for field_name in (
                "predicted_class",
                "rejected",
                "confidence",
                "best_distance",
            ):
                if decision.get(field_name) != expected[field_name]:
                    self._inconsistent(
                        "decision_recompute",
                        "decision cannot be reproduced from distances and thresholds",
                        image_id=image_id,
                        details={"field": field_name},
                    )
                if report.get(field_name) != decision.get(field_name):
                    self._inconsistent(
                        "decision_report",
                        "decision report differs from its artifact",
                        image_id=image_id,
                        details={"field": field_name},
                    )
            if dict(decision.get("distances", {})) != {
                str(class_id): stable_float(independent_distances[class_id])
                for class_id in CONTROLLED_LABELS
            }:
                self._inconsistent(
                    "decision_distances",
                    "decision distance vector cannot be reproduced",
                    image_id=image_id,
                )
            recorded_hashes = decision.get("distance_content_hashes", {})
            for class_id in CONTROLLED_LABELS:
                if recorded_hashes.get(str(class_id)) != distances[class_id].get(
                    "content_hash"
                ):
                    self._inconsistent(
                        "decision_distance_hash",
                        "decision distance hash lineage is inconsistent",
                        image_id=image_id,
                        details={"class_id": class_id},
                    )

        recomputed = set(result.recomputed_nodes)
        reused = set(result.reused_nodes)
        affected = set(result.affected_nodes)
        all_nodes = set(result.node_keys)
        if recomputed & reused:
            self._inconsistent(
                "node_partition",
                "recomputed and reused node sets overlap",
                details={"intersection": sorted(recomputed & reused)[:5]},
            )
        if not affected <= (recomputed | reused):
            missing = sorted(affected - (recomputed | reused))
            self._inconsistent(
                "affected_node_coverage",
                "affected nodes are not covered by recompute/reuse",
                details={"missing": missing[:5]},
            )
        if all_nodes != (recomputed | reused):
            missing = sorted(all_nodes - (recomputed | reused))
            extra = sorted((recomputed | reused) - all_nodes)
            self._inconsistent(
                "node_partition",
                "node partition does not exactly cover current nodes",
                details={"missing": missing[:5], "extra": extra[:5]},
            )

        independently_metrics = compute_metrics_artifact(
            [result.decision_artifacts[sample.image_id] for sample in result.samples],
            result.samples,
        )
        if canonical_bytes(independently_metrics) != canonical_bytes(
            result.metrics_artifact
        ):
            self._inconsistent(
                "metrics_recompute",
                "metrics cannot be reproduced from decisions and labels",
            )


__all__ = [
    "DigitOCRService",
    "RunResult",
    "feature_node_id",
    "distance_node_id",
    "decision_node_id",
    "METRICS_NODE_ID",
]
