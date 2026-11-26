"""Content-addressed cache keys and dependency-directed invalidation plans."""

from __future__ import annotations

import contextlib
import fcntl
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .canonical import atomic_write_json, read_json, sha256_json
from .errors import BusinessError
from .models import Batch, Task
from .snapshot import Snapshot


STAGES = ("normalize", "quantize", "aggregate", "spectrum")


def _base_key_fields(
    task: Task,
    batch: Batch,
    *,
    raw_sample_hash: str,
    snapshot: Snapshot,
) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "batch_id": batch.batch_id,
        "raw_sample_hash": raw_sample_hash,
        "quant_scale": task.quant_scale,
        "quant_zero_point": task.quant_zero_point,
        "quant_formula": task.quant_formula,
        "dequant_formula": task.dequant_formula,
        "rounding_mode": task.rounding_mode,
        "block_plan": task.block_plan,
        "reduction_rule": task.reduction_rule,
        "snapshot_version": snapshot.snapshot_version,
        "task_spec_digest": snapshot.task_spec_digest(task.feature_ids),
    }


def normalization_cache_key(
    task: Task,
    batch: Batch,
    *,
    raw_sample_hash: str,
    snapshot: Snapshot,
) -> str:
    return sha256_json(
        {
            "stage": "normalize",
            "task_id": task.task_id,
            "batch_id": batch.batch_id,
            "raw_sample_hash": raw_sample_hash,
            "snapshot_version": snapshot.snapshot_version,
            "normalization": task.normalization_digest(),
            "upstream_artifact_hash": raw_sample_hash,
        }
    )


def quantization_cache_key(
    task: Task,
    batch: Batch,
    *,
    raw_sample_hash: str,
    upstream_normalized_hash: str,
    snapshot: Snapshot,
) -> str:
    return sha256_json(
        {
            "stage": "quantize",
            "task_id": task.task_id,
            "batch_id": batch.batch_id,
            "raw_sample_hash": raw_sample_hash,
            "snapshot_version": snapshot.snapshot_version,
            "quant_params": {
                "scale": task.quant_scale,
                "zero_point": task.quant_zero_point,
                "quant_formula": task.quant_formula,
                "dequant_formula": task.dequant_formula,
                "rounding_mode": task.rounding_mode,
            },
            "normalization_digest": task.normalization_digest(),
            "upstream_artifact_hash": upstream_normalized_hash,
        }
    )


def aggregation_cache_key(
    task: Task,
    batch: Batch,
    *,
    raw_sample_hash: str,
    upstream_dequantized_hash: str,
    snapshot: Snapshot,
) -> str:
    fields = _base_key_fields(
        task, batch, raw_sample_hash=raw_sample_hash, snapshot=snapshot
    )
    return sha256_json(
        {
            "stage": "aggregate",
            "base": fields,
            "quant_params": {
                "scale": task.quant_scale,
                "zero_point": task.quant_zero_point,
            },
            "block_plan": task.block_plan,
            "reduction_rule": task.reduction_rule,
            "snapshot_version": snapshot.snapshot_version,
            "upstream_artifact_hash": upstream_dequantized_hash,
        }
    )


def spectrum_cache_key(
    task: Task,
    batch: Batch,
    *,
    raw_sample_hash: str,
    upstream_aggregate_hash: str,
    snapshot: Snapshot,
) -> str:
    fields = _base_key_fields(
        task, batch, raw_sample_hash=raw_sample_hash, snapshot=snapshot
    )
    return sha256_json(
        {
            "stage": "spectrum",
            "base": fields,
            "quant_params": {
                "scale": task.quant_scale,
                "zero_point": task.quant_zero_point,
            },
            "block_plan": task.block_plan,
            "snapshot_version": snapshot.snapshot_version,
            "revision": task.revision,
            "tolerance": task.tolerance.to_dict(),
            "upstream_artifact_hash": upstream_aggregate_hash,
        }
    )


def all_stage_keys(
    task: Task,
    batch: Batch,
    *,
    raw_sample_hash: str,
    normalized_hash: str,
    dequantized_hash: str,
    aggregate_hash: str,
    snapshot: Snapshot,
) -> dict[str, str]:
    return {
        "normalize": normalization_cache_key(
            task, batch, raw_sample_hash=raw_sample_hash, snapshot=snapshot
        ),
        "quantize": quantization_cache_key(
            task,
            batch,
            raw_sample_hash=raw_sample_hash,
            upstream_normalized_hash=normalized_hash,
            snapshot=snapshot,
        ),
        "aggregate": aggregation_cache_key(
            task,
            batch,
            raw_sample_hash=raw_sample_hash,
            upstream_dequantized_hash=dequantized_hash,
            snapshot=snapshot,
        ),
        "spectrum": spectrum_cache_key(
            task,
            batch,
            raw_sample_hash=raw_sample_hash,
            upstream_aggregate_hash=aggregate_hash,
            snapshot=snapshot,
        ),
    }


class CacheIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(".lock")

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = read_json(self.path)
        except Exception as exc:
            raise BusinessError("E_FORMAT_CORRUPT", f"cannot read cache index: {exc}") from exc
        if not isinstance(raw, dict):
            raise BusinessError("E_FORMAT_CORRUPT", "cache index must be an object")
        return raw

    @contextlib.contextmanager
    def _lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def publish(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        with self._lock():
            index = self.load()
            for record in records:
                index[record["key"]] = record
            atomic_write_json(self.path, index)


def _task_changed_reasons(
    old_task: Task,
    new_task: Task,
    *,
    old_snapshot: Snapshot,
    new_snapshot: Snapshot,
) -> list[str]:
    reasons: list[str] = []
    if new_task.revision > old_task.revision:
        reasons.append("revision_increased")
    if new_task.block_plan != old_task.block_plan:
        reasons.append("block_plan_changed")
    if (
        new_task.quant_scale != old_task.quant_scale
        or new_task.quant_zero_point != old_task.quant_zero_point
        or new_task.rounding_mode != old_task.rounding_mode
    ):
        reasons.append("quant_params_changed")
    if new_task.normalization_digest() != old_task.normalization_digest():
        reasons.append("normalization_changed")
    old_spec = old_snapshot.task_spec_digest(old_task.feature_ids)
    new_spec = new_snapshot.task_spec_digest(new_task.feature_ids)
    if new_spec != old_spec:
        old_map = old_snapshot.entry_map()
        new_map = new_snapshot.entry_map()
        old_tombstones = old_snapshot.tombstone_map()
        new_tombstones = new_snapshot.tombstone_map()
        generation_changed = {
            feature_id
            for feature_id in new_task.feature_ids
            if (
                (feature_id in old_map and feature_id in new_map and new_map[feature_id].generation > old_map[feature_id].generation)
                or (
                    feature_id in old_tombstones
                    and feature_id in new_tombstones
                    and new_tombstones[feature_id].generation > old_tombstones[feature_id].generation
                )
            )
        }
        reasons.append(
            "snapshot_generation_changed"
            if generation_changed
            else "snapshot_feature_spec_changed"
        )
    return reasons


def _spec_changed_features(
    old_snapshot: Snapshot, new_snapshot: Snapshot
) -> set[str]:
    changed: set[str] = set()
    old_entries = old_snapshot.entry_map()
    new_entries = new_snapshot.entry_map()
    old_tombstones = old_snapshot.tombstone_map()
    new_tombstones = new_snapshot.tombstone_map()
    for feature_id in set(old_entries) | set(new_entries):
        old = old_entries.get(feature_id)
        new = new_entries.get(feature_id)
        old_value = None if old is None else (old.effective_ms, old.generation, old.base_params)
        new_value = None if new is None else (new.effective_ms, new.generation, new.base_params)
        if old_value != new_value:
            changed.add(feature_id)
    for feature_id in set(old_tombstones) | set(new_tombstones):
        old = old_tombstones.get(feature_id)
        new = new_tombstones.get(feature_id)
        old_value = None if old is None else (old.effective_ms, old.generation, old.reason)
        new_value = None if new is None else (new.effective_ms, new.generation, new.reason)
        if old_value != new_value:
            changed.add(feature_id)
    return changed


@dataclass
class BaselineItem:
    task_id: str
    batch_id: str
    raw_sample_hash: str
    normalized_hash: str
    dequantized_hash: str
    aggregate_hash: str
    cache_keys: dict[str, str]
    artifact_paths: dict[str, str]


def build_invalidation_plan(
    *,
    old_tasks: dict[str, Task],
    new_tasks: dict[str, Task],
    batches: list[Batch],
    old_snapshot: Snapshot,
    new_snapshot: Snapshot,
    baseline_items: list[BaselineItem],
    cache_index: dict[str, Any],
) -> dict[str, Any]:
    baseline_map = {(item.task_id, item.batch_id): item for item in baseline_items}
    affected_tasks: list[str] = []
    affected_features: set[str] = set()
    reasons_by_task: dict[str, list[str]] = {}
    for task_id, new_task in new_tasks.items():
        old_task = old_tasks.get(task_id)
        if old_task is None:
            reasons = ["task_added"]
            affected_tasks.append(task_id)
            affected_features.update(new_task.feature_ids)
            reasons_by_task[task_id] = reasons
            continue
        reasons = _task_changed_reasons(
            old_task, new_task, old_snapshot=old_snapshot, new_snapshot=new_snapshot
        )
        if reasons:
            affected_tasks.append(task_id)
            affected_features.update(new_task.feature_ids)
            reasons_by_task[task_id] = reasons
    snapshot_changed_features = _spec_changed_features(old_snapshot, new_snapshot)
    for task_id, new_task in new_tasks.items():
        overlapping = set(new_task.feature_ids) & snapshot_changed_features
        if overlapping:
            if task_id not in affected_tasks:
                affected_tasks.append(task_id)
            reasons_by_task.setdefault(task_id, []).append("feature_spec_changed")
            affected_features.update(overlapping)
    affected_tasks = sorted(set(affected_tasks))

    task_plans: list[dict[str, Any]] = []
    reuse_artifacts: list[dict[str, Any]] = []
    recompute_artifacts: list[dict[str, Any]] = []
    for task_id in sorted(new_tasks):
        task = new_tasks[task_id]
        old_task = old_tasks.get(task_id, task)
        task_batches = [batch for batch in batches if batch.task_id == task_id]
        task_entry: dict[str, Any] = {
            "task_id": task_id,
            "reasons": reasons_by_task.get(task_id, []),
            "affected": task_id in affected_tasks,
            "feature_ids": list(task.feature_ids),
            "affected_features": sorted(
                set(task.feature_ids) & affected_features
                if task_id in affected_tasks
                else set()
            ),
            "batches": [],
        }
        for batch in task_batches:
            baseline = baseline_map.get((task_id, batch.batch_id))
            if baseline is None:
                stage_decisions = {
                    stage: {"decision": "recompute", "reason": "no_baseline"}
                    for stage in STAGES
                }
            else:
                snapshot_for_key = new_snapshot
                normalized_hash = baseline.normalized_hash
                dequantized_hash = baseline.dequantized_hash
                aggregate_hash = baseline.aggregate_hash
                keys: dict[str, str] = {}
                keys["normalize"] = normalization_cache_key(
                    task,
                    batch,
                    raw_sample_hash=baseline.raw_sample_hash,
                    snapshot=snapshot_for_key,
                )
                keys["quantize"] = quantization_cache_key(
                    task,
                    batch,
                    raw_sample_hash=baseline.raw_sample_hash,
                    upstream_normalized_hash=normalized_hash,
                    snapshot=snapshot_for_key,
                )
                keys["aggregate"] = aggregation_cache_key(
                    task,
                    batch,
                    raw_sample_hash=baseline.raw_sample_hash,
                    upstream_dequantized_hash=dequantized_hash,
                    snapshot=snapshot_for_key,
                )
                keys["spectrum"] = spectrum_cache_key(
                    task,
                    batch,
                    raw_sample_hash=baseline.raw_sample_hash,
                    upstream_aggregate_hash=aggregate_hash,
                    snapshot=snapshot_for_key,
                )
                stage_decisions = {}
                reuse_chain = True
                for stage in STAGES:
                    old_key = baseline.cache_keys.get(stage)
                    new_key = keys[stage]
                    available = (
                        old_key in cache_index
                        or stage in baseline.artifact_paths
                        or (old_key == new_key and baseline.artifact_paths.get(stage))
                    )
                    if reuse_chain and old_key == new_key and available:
                        stage_decisions[stage] = {
                            "decision": "reuse",
                            "key": old_key,
                            "artifact_path": baseline.artifact_paths.get(stage),
                            "reason": "content_addressed_key_unchanged",
                        }
                        reuse_artifacts.append(
                            {
                                "task_id": task_id,
                                "batch_id": batch.batch_id,
                                "stage": stage,
                                "key": old_key,
                                "path": baseline.artifact_paths.get(stage),
                            }
                        )
                    else:
                        reuse_chain = False
                        reason = "upstream_recomputed" if old_key == new_key else "cache_key_changed"
                        if stage == "spectrum" and "revision_increased" in reasons_by_task.get(task_id, []):
                            reason = "revision_ledger_refresh"
                        stage_decisions[stage] = {
                            "decision": "recompute",
                            "key": new_key,
                            "old_key": old_key,
                            "reason": reason,
                        }
                        recompute_artifacts.append(
                            {
                                "task_id": task_id,
                                "batch_id": batch.batch_id,
                                "stage": stage,
                                "key": new_key,
                                "old_key": old_key,
                            }
                        )
            task_entry["batches"].append(
                {"batch_id": batch.batch_id, "stages": stage_decisions}
            )
        task_plans.append(task_entry)

    return {
        "schema": "featurediag.invalidation-plan.v1",
        "old_snapshot": {
            "version": old_snapshot.snapshot_version,
            "checksum": old_snapshot.checksum(),
        },
        "new_snapshot": {
            "version": new_snapshot.snapshot_version,
            "checksum": new_snapshot.checksum(),
        },
        "snapshot_replacement": {
            "mode": "whole_snapshot_atomic_replace",
            "version_or_checksum_changed": (
                old_snapshot.snapshot_version != new_snapshot.snapshot_version
                or old_snapshot.checksum() != new_snapshot.checksum()
            ),
        },
        "affected_tasks": affected_tasks,
        "affected_features": sorted(affected_features),
        "recomputed_suffix": sorted(
            {
                f"{item['task_id']}:{item['batch_id']}:{item['stage']}"
                for item in recompute_artifacts
            }
        ),
        "tasks": task_plans,
        "reuse_manifest": reuse_artifacts,
        "recompute_manifest": recompute_artifacts,
        "ledger_action": {
            "mode": "replace_affected_entries_only",
            "affected_entries": sorted(
                {
                    f"{item['task_id']}:{item['batch_id']}"
                    for item in recompute_artifacts
                }
            ),
        },
    }
