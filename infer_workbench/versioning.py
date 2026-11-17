"""Whole-version comparison without incremental state merging."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .batching import BatchPlan
from .config import BatchConfig


@dataclass(frozen=True)
class VersionArchive:
    config: BatchConfig
    data_digest: str
    parameter_digest: str
    plans: tuple[BatchPlan, ...]
    batches: tuple[dict[str, Any], ...]
    complete: bool

    @property
    def config_version(self) -> str:
        return self.config.config_version


def _direction(new_value: float, old_value: float, tolerance: float = 1e-15) -> str:
    if new_value > old_value + tolerance:
        return "higher"
    if new_value < old_value - tolerance:
        return "lower"
    return "unchanged"


def _sample_map(batch: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(sample["sample_id"]): sample for sample in batch.get("samples", [])}


def _layer_map(sample: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(layer["layer"]): layer
        for layer in sample.get("diagnostics", {}).get("layers", [])
    }


def _max_abs_delta(left: Sequence[float], right: Sequence[float]) -> float:
    length = min(len(left), len(right))
    if length == 0:
        return 0.0
    return max(abs(float(right[index]) - float(left[index])) for index in range(length))


def compare_versions(
    old: VersionArchive,
    new_config: BatchConfig,
    new_plans: Sequence[BatchPlan],
    new_batches: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    old_by_index = {int(batch["batch_index"]): batch for batch in old.batches}
    new_by_index = {int(batch["batch_index"]): batch for batch in new_batches}
    max_index = max(
        [*old_by_index.keys(), *new_by_index.keys()], default=0
    )
    batch_diffs: list[dict[str, Any]] = []
    changed_layer_names: set[str] = set()
    changed_batch_indices: list[int] = []
    drift_directions: list[str] = []

    for index in range(1, max_index + 1):
        old_batch = old_by_index.get(index)
        new_batch = new_by_index.get(index)
        if old_batch is None:
            record = {
                "batch_index": index,
                "batch_id": new_batch["batch_id"],
                "kind": "added",
                "changed": True,
                "old_sample_ids": [],
                "new_sample_ids": list(new_batch.get("sample_ids", [])),
                "output_change_direction": "higher",
                "layers": [],
            }
            batch_diffs.append(record)
            changed_batch_indices.append(index)
            continue
        if new_batch is None:
            record = {
                "batch_index": index,
                "batch_id": old_batch["batch_id"],
                "kind": "removed",
                "changed": True,
                "old_sample_ids": list(old_batch.get("sample_ids", [])),
                "new_sample_ids": [],
                "output_change_direction": "lower",
                "layers": [],
            }
            batch_diffs.append(record)
            changed_batch_indices.append(index)
            continue

        old_ids = list(old_batch.get("sample_ids", []))
        new_ids = list(new_batch.get("sample_ids", []))
        old_samples = _sample_map(old_batch)
        new_samples = _sample_map(new_batch)
        common_ids = sorted(set(old_samples) & set(new_samples), key=new_ids.index)
        added_ids = [sample_id for sample_id in new_ids if sample_id not in old_samples]
        removed_ids = [sample_id for sample_id in old_ids if sample_id not in new_samples]

        output_delta = 0.0
        for sample_id in common_ids:
            output_delta = max(
                output_delta,
                _max_abs_delta(
                    old_samples[sample_id].get("output", []),
                    new_samples[sample_id].get("output", []),
                ),
            )

        layer_records: list[dict[str, Any]] = []
        all_layers: list[str] = []
        for sample_id in common_ids:
            for layer_name in _layer_map(new_samples[sample_id]):
                if layer_name not in all_layers:
                    all_layers.append(layer_name)
        for layer_name in all_layers:
            old_bounds: list[float] = []
            new_bounds: list[float] = []
            old_relative: list[float] = []
            new_relative: list[float] = []
            for sample_id in common_ids:
                old_layer = _layer_map(old_samples[sample_id]).get(layer_name)
                new_layer = _layer_map(new_samples[sample_id]).get(layer_name)
                if old_layer is None or new_layer is None:
                    continue
                old_bounds.append(float(old_layer.get("cumulative_error_bound_grid", 0.0)))
                new_bounds.append(float(new_layer.get("cumulative_error_bound_grid", 0.0)))
                old_relative.append(float(old_layer.get("relative_error", 0.0)))
                new_relative.append(float(new_layer.get("relative_error", 0.0)))
            if not old_bounds:
                continue
            old_bound_max = max(old_bounds)
            new_bound_max = max(new_bounds)
            old_relative_max = max(old_relative)
            new_relative_max = max(new_relative)
            bound_direction = _direction(new_bound_max, old_bound_max)
            deviation_direction = _direction(new_relative_max, old_relative_max)
            changed = bound_direction != "unchanged" or deviation_direction != "unchanged"
            if changed:
                changed_layer_names.add(layer_name)
            layer_records.append(
                {
                    "layer": layer_name,
                    "old_bound_max_grid": old_bound_max,
                    "new_bound_max_grid": new_bound_max,
                    "bound_direction": bound_direction,
                    "old_relative_error_max": old_relative_max,
                    "new_relative_error_max": new_relative_max,
                    "deviation_direction": deviation_direction,
                    "changed": changed,
                }
            )

        plan_changed = old_ids != new_ids
        drift_old = bool(old_batch.get("drift", False))
        drift_new = bool(new_batch.get("drift", False))
        drift_direction = "higher" if drift_new and not drift_old else "lower" if drift_old and not drift_new else "unchanged"
        if drift_direction != "unchanged":
            drift_directions.append(drift_direction)
        output_direction = "higher" if output_delta > 0.0 else "unchanged"
        changed = bool(
            plan_changed
            or added_ids
            or removed_ids
            or output_delta > 0.0
            or any(layer["changed"] for layer in layer_records)
            or drift_direction != "unchanged"
        )
        record = {
            "batch_index": index,
            "batch_id": new_batch["batch_id"],
            "kind": "plan_changed" if plan_changed else "same_plan",
            "changed": changed,
            "old_sample_ids": old_ids,
            "new_sample_ids": new_ids,
            "sample_ids_added": added_ids,
            "sample_ids_removed": removed_ids,
            "output_max_abs_delta": output_delta,
            "output_change_direction": output_direction,
            "old_drift": drift_old,
            "new_drift": drift_new,
            "drift_direction": drift_direction,
            "layers": layer_records,
        }
        batch_diffs.append(record)
        if changed:
            changed_batch_indices.append(index)

    if any(direction == "higher" for direction in drift_directions):
        overall_drift_direction = "higher"
    elif any(direction == "lower" for direction in drift_directions):
        overall_drift_direction = "lower"
    else:
        overall_drift_direction = "unchanged"

    return {
        "old_config_version": old.config_version,
        "new_config_version": new_config.config_version,
        "changed_parameters": old.config.changed_fields(new_config),
        "old_completed_batches": len(old.batches),
        "new_completed_batches": len(new_batches),
        "old_batch_count": len(old.plans),
        "new_batch_count": len(new_plans),
        "changed_batch_indices": changed_batch_indices,
        "changed_layers": sorted(changed_layer_names),
        "drift_direction": overall_drift_direction,
        "batches": batch_diffs,
    }
