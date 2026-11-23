from __future__ import annotations

import dataclasses
import math
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from .canonical import (
    canonical_json,
    decimal_text,
    digest_json,
    load_json,
    parse_json_text,
)
from .db import (
    allocate_sequence,
    get_meta_int,
    require_active_snapshot,
    set_meta,
    transaction,
)
from .errors import (
    E_CLOCK_REGRESSION,
    E_PATCH_CONFLICT,
    E_PATCH_INVALID,
    E_STATE_CORRUPT,
    E_WINDOW_ARGUMENT,
    fail,
)
from .faults import maybe_fault
from .state import active_metrics, load_experiments, load_metric_records, snapshot_view_rows
from .validation import (
    Experiment,
    validate_experiments,
    validate_metric_reference_targets,
)
from .windows import (
    dependency_fingerprints,
    materialize_metric,
    refresh_trace,
    validate_window_ms,
)


_MISSING = object()


@dataclass
class ParsedPatch:
    patch_id: str
    now_ms: int
    window_ms: int
    operations: list[dict[str, Any]]
    semantics_digest: str
    raw: dict[str, Any] = field(repr=False)


@dataclass
class Closure:
    affected: dict[tuple[str, str], dict[str, Any]]
    reused: list[dict[str, str]]
    changed_experiments: list[str]
    changed_fields: dict[str, list[str]]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_patch_file(path: str, default_window_ms: int = 1000) -> ParsedPatch:
    raw = load_json(path)
    if not isinstance(raw, dict):
        fail(E_PATCH_INVALID, "Patch document must be an object")
    patch_id = raw.get("patch_id")
    if not isinstance(patch_id, str) or not patch_id.strip():
        fail(E_PATCH_INVALID, "Patch must declare a non-empty patch_id")
    now_ms = raw.get("now_ms")
    if not _is_int(now_ms) or now_ms < 0:
        fail(E_PATCH_INVALID, "Patch must declare a non-negative integer now_ms")
    window_ms = raw.get("window_ms", default_window_ms)
    if not _is_int(window_ms) or window_ms <= 0:
        fail(E_PATCH_INVALID, "Patch window_ms must be a positive integer")
    operations = raw.get("operations")
    if operations is None and "experiment_id" in raw:
        operations = [
            {
                key: value
                for key, value in raw.items()
                if key
                not in {
                    "patch_id",
                    "now_ms",
                    "window_ms",
                    "operations",
                    "description",
                }
            }
        ]
    if not isinstance(operations, list) or not operations:
        fail(E_PATCH_INVALID, "Patch operations must be a non-empty array")
    normalized_operations: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed = {
        "experiment_id",
        "hyperparameters",
        "replace_hyperparameters",
        "dataset_version",
        "parent_id",
        "code_commit",
        "baseline_metrics",
        "baseline_metrics_patch",
        "metric_references",
        "metric_references_patch",
        "aliases",
    }
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            fail(E_PATCH_INVALID, "Patch operation must be an object", index=index)
        unknown = sorted(set(operation) - allowed)
        if unknown:
            fail(
                E_PATCH_INVALID,
                "Patch operation has unsupported fields",
                index=index,
                fields=unknown,
            )
        experiment_id = operation.get("experiment_id")
        if not isinstance(experiment_id, str) or not experiment_id.strip():
            fail(E_PATCH_INVALID, "Patch operation must name an experiment", index=index)
        if experiment_id in seen:
            fail(
                E_PATCH_INVALID,
                "A patch may contain only one operation per experiment",
                experiment_id=experiment_id,
            )
        seen.add(experiment_id)
        changes = set(operation) - {"experiment_id"}
        if not changes:
            fail(
                E_PATCH_INVALID,
                "Patch operation does not change anything",
                experiment_id=experiment_id,
            )
        normalized_operations.append(operation)
    semantics_digest = digest_json(
        {
            "now_ms": now_ms,
            "window_ms": window_ms,
            "operations": normalized_operations,
        }
    )
    return ParsedPatch(
        patch_id=patch_id,
        now_ms=now_ms,
        window_ms=window_ms,
        operations=normalized_operations,
        semantics_digest=semantics_digest,
        raw=raw,
    )


def _refs_to_document(
    refs: dict[str, tuple[str, str]]
) -> dict[str, dict[str, str]]:
    return {
        metric: {"experiment_id": target[0], "metric": target[1]}
        for metric, target in sorted(refs.items())
    }


def _experiment_to_document(experiment: Experiment) -> dict[str, Any]:
    return {
        **experiment.declaration(),
        "aliases": list(experiment.aliases),
    }


def _parse_reference(value: Any, metric: str) -> tuple[str, str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        if "." not in value:
            fail(
                E_PATCH_INVALID,
                "String metric reference must be '<experiment>.<metric>'",
                metric=metric,
            )
        return tuple(value.rsplit(".", 1))  # type: ignore[return-value]
    if not isinstance(value, dict):
        fail(E_PATCH_INVALID, "Metric reference must be an object or string", metric=metric)
    target_experiment = value.get("experiment_id", value.get("experiment"))
    target_metric = value.get("metric")
    if not isinstance(target_experiment, str) or not isinstance(target_metric, str):
        fail(E_PATCH_INVALID, "Metric reference target is invalid", metric=metric)
    return target_experiment, target_metric


def _apply_operation(
    experiment: Experiment, operation: dict[str, Any]
) -> tuple[Experiment, list[str]]:
    changes: list[str] = []
    hyperparameters = dict(experiment.hyperparameters)
    if "replace_hyperparameters" in operation:
        value = operation["replace_hyperparameters"]
        if not isinstance(value, dict):
            fail(E_PATCH_INVALID, "replace_hyperparameters must be an object")
        hyperparameters = dict(value)
        changes.append("hyperparameters")
    if "hyperparameters" in operation:
        value = operation["hyperparameters"]
        if not isinstance(value, dict):
            fail(E_PATCH_INVALID, "hyperparameters must be an object")
        for key, item in value.items():
            if item is None:
                hyperparameters.pop(key, None)
            else:
                hyperparameters[key] = item
        changes.append("hyperparameters")

    dataset_version = experiment.dataset_version
    if "dataset_version" in operation:
        dataset_version = operation["dataset_version"]
        if not isinstance(dataset_version, str) or not dataset_version:
            fail(E_PATCH_INVALID, "dataset_version must be a non-empty string")
        changes.append("dataset_version")

    parent_id = experiment.parent_id
    if "parent_id" in operation:
        parent_id = operation["parent_id"]
        if parent_id is not None and (
            not isinstance(parent_id, str) or not parent_id.strip()
        ):
            fail(E_PATCH_INVALID, "parent_id must be null or a non-empty string")
        changes.append("parent_id")

    code_commit = experiment.code_commit
    if "code_commit" in operation:
        code_commit = operation["code_commit"]
        if not isinstance(code_commit, str) or not code_commit:
            fail(E_PATCH_INVALID, "code_commit must be a non-empty string")
        changes.append("code_commit")

    baseline_metrics = dict(experiment.baseline_metrics)
    if "baseline_metrics" in operation:
        value = operation["baseline_metrics"]
        if not isinstance(value, dict):
            fail(E_PATCH_INVALID, "baseline_metrics must be an object")
        baseline_metrics = {}
        for metric, raw_value in value.items():
            if not isinstance(metric, str) or not metric:
                fail(E_PATCH_INVALID, "Baseline metric names must be non-empty strings")
            baseline_metrics[metric] = _numeric_decimal(raw_value, metric)
        changes.append("baseline_metrics")
    if "baseline_metrics_patch" in operation:
        value = operation["baseline_metrics_patch"]
        if not isinstance(value, dict):
            fail(E_PATCH_INVALID, "baseline_metrics_patch must be an object")
        for metric, raw_value in value.items():
            if raw_value is None:
                baseline_metrics.pop(metric, None)
            else:
                baseline_metrics[metric] = _numeric_decimal(raw_value, metric)
        changes.append("baseline_metrics")

    references = dict(experiment.metric_references)
    if "metric_references" in operation:
        value = operation["metric_references"]
        if not isinstance(value, dict):
            fail(E_PATCH_INVALID, "metric_references must be an object")
        references = {}
        for metric, raw_target in value.items():
            reference = _parse_reference(raw_target, metric)
            if reference is not None:
                references[metric] = reference
        changes.append("metric_references")
    if "metric_references_patch" in operation:
        value = operation["metric_references_patch"]
        if not isinstance(value, dict):
            fail(E_PATCH_INVALID, "metric_references_patch must be an object")
        for metric, raw_target in value.items():
            reference = _parse_reference(raw_target, metric)
            if reference is None:
                references.pop(metric, None)
            else:
                references[metric] = reference
        changes.append("metric_references")

    aliases = experiment.aliases
    if "aliases" in operation:
        value = operation["aliases"]
        if not isinstance(value, list) or not all(
            isinstance(alias, str) and alias for alias in value
        ):
            fail(E_PATCH_INVALID, "aliases must be an array of non-empty strings")
        aliases = tuple(value)
        changes.append("aliases")

    updated = dataclasses.replace(
        experiment,
        parent_id=parent_id,
        hyperparameters=hyperparameters,
        dataset_version=dataset_version,
        code_commit=code_commit,
        baseline_metrics=baseline_metrics,
        metric_references=references,
        aliases=aliases,
    )
    actual_changes = [
        field_name
        for field_name in sorted(set(changes))
        if getattr(updated, field_name) != getattr(experiment, field_name)
    ]
    return updated, actual_changes


def _numeric_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        fail(E_PATCH_INVALID, "Baseline metric must be a finite number", metric=label)
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            fail(E_PATCH_INVALID, "Baseline metric must be finite", metric=label)
        result = Decimal(str(value))
    else:
        result = Decimal(value)
    if not result.is_finite():
        fail(E_PATCH_INVALID, "Baseline metric must be finite", metric=label)
    return result


def _apply_patch_to_experiments(
    experiments: dict[str, Experiment], patch: ParsedPatch
) -> tuple[dict[str, Experiment], list[str], dict[str, list[str]]]:
    updated = dict(experiments)
    changed_fields: dict[str, list[str]] = {}
    for operation in patch.operations:
        experiment_id = operation["experiment_id"]
        current = updated.get(experiment_id)
        if current is None:
            fail(
                E_PATCH_INVALID,
                "Patch references an unknown experiment",
                experiment_id=experiment_id,
            )
        new_experiment, fields = _apply_operation(current, operation)
        if fields:
            updated[experiment_id] = new_experiment
            changed_fields[experiment_id] = fields
    if not changed_fields:
        return updated, [], {}
    documents = [_experiment_to_document(updated[key]) for key in sorted(updated)]
    try:
        validated = validate_experiments({"experiments": documents})
    except Exception as exc:
        if hasattr(exc, "code"):
            fail(
                E_PATCH_INVALID,
                "Patch would create an invalid experiment lineage",
                cause=getattr(exc, "code"),
                detail=str(exc),
            )
        raise
    validated_map = {experiment.id: experiment for experiment in validated}
    return validated_map, sorted(changed_fields), changed_fields


def _children(experiments: dict[str, Experiment]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {experiment_id: [] for experiment_id in experiments}
    for experiment in experiments.values():
        if experiment.parent_id is not None:
            children[experiment.parent_id].append(experiment.id)
    for values in children.values():
        values.sort()
    return children


def _descendant_paths(
    root: str, experiments: dict[str, Experiment]
) -> dict[str, list[str]]:
    children = _children(experiments)
    result: dict[str, list[str]] = {}

    def walk(node: str, path: list[str]) -> None:
        for child in children.get(node, []):
            child_path = path + [child]
            if child not in result or len(child_path) < len(result[child]):
                result[child] = child_path
            walk(child, child_path)

    walk(root, [root])
    return result


def compute_closure(
    old_experiments: dict[str, Experiment],
    new_experiments: dict[str, Experiment],
    changed_experiments: list[str],
    changed_fields: dict[str, list[str]],
    metrics: dict[tuple[str, str], None],
) -> Closure:
    affected: dict[tuple[str, str], dict[str, Any]] = {}

    def add(
        experiment_id: str,
        metric: str,
        reason: str,
        chain: list[str],
    ) -> bool:
        key = (experiment_id, metric)
        if key in affected:
            if len(chain) < len(affected[key]["source_chain"]):
                affected[key]["source_chain"] = chain
                affected[key]["reason"] = reason
            return False
        affected[key] = {
            "experiment_id": experiment_id,
            "metric": metric,
            "reason": reason,
            "source_chain": chain,
        }
        return True

    children = _children(new_experiments)
    all_metrics_by_experiment: dict[str, list[str]] = {}
    for experiment_id, metric in metrics:
        all_metrics_by_experiment.setdefault(experiment_id, []).append(metric)
    for values in all_metrics_by_experiment.values():
        values.sort()

    for root in changed_experiments:
        fields = changed_fields.get(root, ["lineage"])
        for metric in all_metrics_by_experiment.get(root, []):
            add(
                root,
                metric,
                "direct_change:" + ",".join(fields),
                [root, f"{root}.{metric}"],
            )
        for descendant, path in sorted(_descendant_paths(root, new_experiments).items()):
            for metric in all_metrics_by_experiment.get(descendant, []):
                add(
                    descendant,
                    metric,
                    "lineage_ancestor_changed",
                    path + [f"{descendant}.{metric}"],
                )

    changed = True
    while changed:
        changed = False
        for experiment_id in sorted(new_experiments):
            experiment = new_experiments[experiment_id]
            for metric, (target_experiment, target_metric) in sorted(
                experiment.metric_references.items()
            ):
                target_key = (target_experiment, target_metric)
                target_info = affected.get(target_key)
                if target_info is None:
                    continue
                metric_key = (experiment_id, metric)
                chain = [f"{experiment_id}.{metric}", *target_info["source_chain"]]
                if add(experiment_id, metric, "metric_reference_invalidation", chain):
                    changed = True
                    for descendant, path in sorted(
                        _descendant_paths(experiment_id, new_experiments).items()
                    ):
                        for descendant_metric in all_metrics_by_experiment.get(
                            descendant, []
                        ):
                            if add(
                                descendant,
                                descendant_metric,
                                "lineage_of_referenced_metric_changed",
                                [f"{experiment_id}.{metric}"]
                                + path
                                + [f"{descendant}.{descendant_metric}"],
                            ):
                                changed = True
    reused = [
        {"experiment_id": experiment_id, "metric": metric, "status": "safe_reuse"}
        for (experiment_id, metric) in sorted(metrics)
        if (experiment_id, metric) not in affected
    ]
    return Closure(
        affected=affected,
        reused=reused,
        changed_experiments=changed_experiments,
        changed_fields=changed_fields,
    )


def _rejected_candidates(
    connection: sqlite3.Connection,
    snapshot_version: str,
    experiment_id: str,
    metric: str,
) -> list[dict[str, Any]]:
    rejected: list[dict[str, Any]] = []
    metric_row = connection.execute(
        """
        SELECT summary_digest, summary_json, record_count, window_count
        FROM metric_summaries
        WHERE snapshot_version = ? AND experiment_id = ? AND metric = ?
        """,
        (snapshot_version, experiment_id, metric),
    ).fetchone()
    if metric_row is not None:
        rejected.append(
            {
                "kind": "metric_summary",
                "experiment_id": experiment_id,
                "metric": metric,
                "previous_summary_digest": metric_row["summary_digest"],
                "previous_record_count": int(metric_row["record_count"]),
                "previous_window_count": int(metric_row["window_count"]),
                "reason": "dependency_changed",
            }
        )
    for row in connection.execute(
        """
        SELECT window_start_ms, window_end_ms, window_number, summary_digest,
               lineage_fingerprint, record_count
        FROM windows
        WHERE snapshot_version = ? AND experiment_id = ? AND metric = ?
        ORDER BY window_start_ms
        """,
        (snapshot_version, experiment_id, metric),
    ):
        rejected.append(
            {
                "kind": "refresh_window",
                "experiment_id": experiment_id,
                "metric": metric,
                "window_start_ms": int(row["window_start_ms"]),
                "window_end_ms": int(row["window_end_ms"]),
                "window_number": int(row["window_number"]),
                "previous_summary_digest": row["summary_digest"],
                "previous_lineage_fingerprint": row["lineage_fingerprint"],
                "previous_record_count": int(row["record_count"]),
                "reason": "dependency_changed",
            }
        )
    return rejected


def _existing_patch_result(
    connection: sqlite3.Connection, patch: ParsedPatch
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM patches WHERE patch_id = ?", (patch.patch_id,)
    ).fetchone()
    if row is None:
        return None
    if str(row["semantics_digest"]) != patch.semantics_digest:
        fail(
            E_PATCH_CONFLICT,
            "The same patch_id was submitted with different semantics",
            patch_id=patch.patch_id,
        )
    event_id = row["event_id"]
    event_status = None
    if event_id:
        event = connection.execute(
            "SELECT status FROM invalidation_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        event_status = None if event is None else event["status"]
    return {
        "ok": True,
        "operation": "patch",
        "patch_id": patch.patch_id,
        "duplicate": True,
        "event_id": event_id,
        "event_status": event_status,
        "snapshot_version": require_active_snapshot(connection),
        "data_epoch": int(row["applied_data_epoch"]),
        "changed_experiments": parse_json_text(str(row["invalidated_json"])).get(
            "changed_experiments", []
        ),
        "invalidated": parse_json_text(str(row["invalidated_json"])).get(
            "invalidated", []
        ),
        "reused": parse_json_text(str(row["invalidated_json"])).get("reused", []),
    }


def _recompute_event(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    fault_before_commit: bool = False,
) -> dict[str, Any] | None:
    event = connection.execute(
        "SELECT * FROM invalidation_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if event is None:
        fail(E_STATE_CORRUPT, "Invalidation event is missing", event_id=event_id)
    if event["status"] == "complete":
        return None
    snapshot_version = str(event["snapshot_version"])
    now_ms = int(event["now_ms"])
    window_ms = int(event["window_ms"])
    data_epoch = int(event["data_epoch"])
    items = connection.execute(
        """
        SELECT * FROM invalidation_items
        WHERE event_id = ? ORDER BY experiment_id, metric
        """,
        (event_id,),
    ).fetchall()
    experiments = load_experiments(connection)
    records = load_metric_records(connection)
    groups: dict[tuple[str, str], list[Any]] = {}
    for record in records:
        groups.setdefault((record.experiment_id, record.metric), []).append(record)
    for values in groups.values():
        values.sort(key=lambda record: (record.logical_clock_ms, record.seq, record.record_id))
    view = {
        (str(row["experiment_id"]), str(row["metric"])): row
        for row in snapshot_view_rows(connection, snapshot_version)
    }
    dependencies = dependency_fingerprints(experiments)
    recomputed = []
    for item in items:
        experiment_id = str(item["experiment_id"])
        metric = str(item["metric"])
        experiment = experiments.get(experiment_id)
        if experiment is None:
            fail(
                E_STATE_CORRUPT,
                "Invalidation item references a missing experiment",
                experiment_id=experiment_id,
            )
        result = materialize_metric(
            connection,
            snapshot_version=snapshot_version,
            experiment=experiment,
            metric=metric,
            records=groups.get((experiment_id, metric), []),
            now_ms=now_ms,
            window_ms=window_ms,
            event_id=event_id,
            data_epoch=data_epoch,
            only_new=False,
            experiments=experiments,
            experiment_dependencies=dependencies,
            view=view,
        )
        connection.execute(
            """
            UPDATE invalidation_items
            SET status = 'recomputed'
            WHERE event_id = ? AND experiment_id = ? AND metric = ?
            """,
            (event_id, experiment_id, metric),
        )
        recomputed.append(result)
    completed_order = allocate_sequence(connection, "event_sequence")
    connection.execute(
        """
        UPDATE invalidation_events
        SET status = 'complete', completed_order = ?
        WHERE event_id = ?
        """,
        (completed_order, event_id),
    )
    connection.execute(
        "UPDATE patches SET status = 'completed' WHERE patch_id = ?",
        (event["patch_id"],),
    )
    if fault_before_commit:
        maybe_fault("before_invalidation_commit")
    return {
        "event_id": event_id,
        "status": "complete",
        "recomputed": recomputed,
    }


def apply_patch(
    connection: sqlite3.Connection,
    patch: ParsedPatch,
) -> dict[str, Any]:
    active_snapshot = require_active_snapshot(connection)
    existing = _existing_patch_result(connection, patch)
    if existing is not None:
        if existing.get("event_status") not in (None, "complete") and existing.get("event_id"):
            with transaction(connection):
                _recompute_event(connection, str(existing["event_id"]))
            refreshed = _existing_patch_result(connection, patch)
            if refreshed is not None:
                return refreshed
        return existing
    current_clock = get_meta_int(connection, "logical_clock_watermark_ms")
    if patch.now_ms < current_clock:
        fail(
            E_CLOCK_REGRESSION,
            "Patch logical clock cannot move backwards",
            current_watermark_ms=current_clock,
            patch_now_ms=patch.now_ms,
        )
    configured_window = get_meta_int(connection, "window_ms", 0)
    if configured_window and configured_window != patch.window_ms:
        fail(
            E_WINDOW_ARGUMENT,
            "Patch window size differs from the active workbench window",
            configured_window_ms=configured_window,
            patch_window_ms=patch.window_ms,
        )
    old_experiments = load_experiments(connection)
    new_experiments, changed, changed_fields = _apply_patch_to_experiments(
        old_experiments, patch
    )
    if not changed:
        return {
            "ok": True,
            "operation": "patch",
            "patch_id": patch.patch_id,
            "duplicate": False,
            "noop": True,
            "changed_experiments": [],
            "changed_fields": {},
            "invalidated": [],
            "reused": [
                {"experiment_id": experiment_id, "metric": metric, "status": "safe_reuse"}
                for experiment_id, metric in sorted(
                    active_metrics(connection, active_snapshot, old_experiments)
                )
            ],
            "snapshot_version": active_snapshot,
            "data_epoch": get_meta_int(connection, "data_epoch"),
        }
    metrics = active_metrics(connection, active_snapshot, new_experiments)
    validate_metric_reference_targets(new_experiments.values(), set(metrics))
    closure = compute_closure(
        old_experiments,
        new_experiments,
        changed,
        changed_fields,
        metrics,
    )
    old_epoch = get_meta_int(connection, "data_epoch")
    new_epoch = old_epoch + 1
    event_id = digest_json(
        {
            "operation": "invalidation",
            "patch_id": patch.patch_id,
            "semantics_digest": patch.semantics_digest,
            "data_epoch": new_epoch,
        }
    )
    rejected_by_metric = {
        key: _rejected_candidates(
            connection, active_snapshot, key[0], key[1]
        )
        for key in sorted(closure.affected)
    }
    invalidated = [
        {
            **closure.affected[key],
            "rejected_reuse_candidates": rejected_by_metric[key],
        }
        for key in sorted(closure.affected)
    ]

    with transaction(connection):
        existing = _existing_patch_result(connection, patch)
        if existing is not None:
            return existing
        for experiment_id in changed:
            from .state import update_experiment

            update_experiment(connection, new_experiments[experiment_id], new_epoch)
        set_meta(connection, "data_epoch", new_epoch)
        set_meta(
            connection,
            "logical_clock_watermark_ms",
            max(current_clock, patch.now_ms),
        )
        created_order = allocate_sequence(connection, "event_sequence")
        invalidated_json = canonical_json(
            {
                "changed_experiments": changed,
                "changed_fields": changed_fields,
                "invalidated": invalidated,
                "reused": closure.reused,
            }
        )
        connection.execute(
            """
            INSERT INTO patches(
                patch_id, semantics_digest, now_ms, window_ms, operations_json,
                invalidated_json, status, applied_data_epoch, event_id
            ) VALUES (?, ?, ?, ?, ?, ?, 'applied', ?, ?)
            """,
            (
                patch.patch_id,
                patch.semantics_digest,
                patch.now_ms,
                patch.window_ms,
                canonical_json(patch.operations),
                invalidated_json,
                new_epoch,
                event_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO invalidation_events(
                event_id, patch_id, snapshot_version, data_epoch, now_ms,
                window_ms, trigger_json, status, created_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                event_id,
                patch.patch_id,
                active_snapshot,
                new_epoch,
                patch.now_ms,
                patch.window_ms,
                canonical_json(
                    {
                        "kind": "patch",
                        "patch_id": patch.patch_id,
                        "semantics_digest": patch.semantics_digest,
                        "changed_experiments": changed,
                        "changed_fields": changed_fields,
                        "operations": patch.operations,
                    }
                ),
                created_order,
            ),
        )
        for item in invalidated:
            connection.execute(
                """
                INSERT INTO invalidation_items(
                    event_id, experiment_id, metric, reason, source_chain_json,
                    rejected_candidates_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    event_id,
                    item["experiment_id"],
                    item["metric"],
                    item["reason"],
                    canonical_json(item["source_chain"]),
                    canonical_json(item["rejected_reuse_candidates"]),
                ),
            )
    maybe_fault("after_patch_commit")
    with transaction(connection):
        completed = _recompute_event(connection, event_id, fault_before_commit=True)
    maybe_fault("after_invalidation_commit")
    return {
        "ok": True,
        "operation": "patch",
        "patch_id": patch.patch_id,
        "duplicate": False,
        "event_id": event_id,
        "event_status": "complete",
        "snapshot_version": active_snapshot,
        "data_epoch": new_epoch,
        "changed_experiments": changed,
        "changed_fields": changed_fields,
        "invalidated": invalidated,
        "reused": closure.reused,
        "recomputed_metrics": [
            {
                "experiment_id": item["experiment_id"],
                "metric": item["metric"],
                "windows_updated": item["windows_updated"],
                "windows_reused": item["windows_reused"],
            }
            for item in sorted(
                [] if completed is None else completed["recomputed"],
                key=lambda value: (value["experiment_id"], value["metric"]),
            )
        ],
    }


def resume(connection: sqlite3.Connection) -> dict[str, Any]:
    pending = connection.execute(
        """
        SELECT event_id FROM invalidation_events
        WHERE status != 'complete'
        ORDER BY created_order, event_id
        """
    ).fetchall()
    completed = []
    for row in pending:
        with transaction(connection):
            result = _recompute_event(connection, str(row["event_id"]))
        if result is not None:
            completed.append(result)
    maybe_fault("after_resume")
    remaining = connection.execute(
        "SELECT COUNT(*) AS count FROM invalidation_events WHERE status != 'complete'"
    ).fetchone()
    return {
        "ok": True,
        "operation": "resume",
        "replayed_events": completed,
        "remaining_pending_events": int(remaining["count"]),
        "status": "recovered" if completed else "nothing_to_resume",
    }

