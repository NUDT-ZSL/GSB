from __future__ import annotations

import json
from fractions import Fraction
from typing import Any

from quota_ledger.db import Store
from quota_ledger.errors import fail
from quota_ledger.models import Dataset, PolicyParams
from quota_ledger.policy import compute_policy_merge
from quota_ledger.util import canonical_json


def params_to_json(params: PolicyParams) -> str:
    return canonical_json(params.as_dict())


def default_params_from_dataset(dataset: Dataset) -> PolicyParams:
    baseline_sum = sum(item.baseline_micros for item in dataset.configs)
    global_budget = max(1, baseline_sum + baseline_sum // 5)
    round_budget = max(1, global_budget // 10)
    per_cap = max(1, global_budget // 20)
    return PolicyParams(
        global_quota_budget_micros=global_budget,
        round_threshold_budget_micros=round_budget,
        per_tenant_adjustment_cap_micros=per_cap,
        safety_watermark_micros=0,
        drift_threshold_ratio=Fraction(1, 10),
        drift_threshold_text="0.1",
        danger_threshold_ratio=Fraction(1, 5),
        danger_threshold_text="0.2",
    )


def _previous_merge_rows(connection: Any) -> dict[str, dict[str, Any]]:
    rows = connection.execute("SELECT * FROM policy_merge").fetchall()
    return {row["tenant"]: dict(row) for row in rows}


def _insert_policy_rows(connection: Any, dataset: Dataset) -> None:
    for snapshot in dataset.snapshots:
        connection.execute(
            "INSERT INTO policies("
            "policy_id, snapshot_id, version, generation, created_clock_ms, "
            "effective_clock_ms, params_json, thresholds_json, tombstones_json, "
            "dependencies_json, shards_json, fingerprint, source_path, version_rank"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot.policy_id,
                snapshot.snapshot_id,
                snapshot.version,
                snapshot.generation,
                snapshot.created_clock_ms,
                snapshot.effective_clock_ms,
                params_to_json(snapshot.params),
                canonical_json(snapshot.thresholds),
                canonical_json([item.as_dict() for item in snapshot.tombstones]),
                canonical_json([item.as_dict() for item in snapshot.dependencies]),
                canonical_json(list(snapshot.shards)),
                snapshot.fingerprint,
                snapshot.source_path,
                snapshot.version_rank,
            ),
        )
        for tenant, threshold_micros in sorted(snapshot.thresholds.items()):
            config = dataset.config_by_tenant[tenant]
            connection.execute(
                "INSERT INTO policy_thresholds(policy_id, tenant, resource_key, threshold_micros) "
                "VALUES(?,?,?,?)",
                (snapshot.policy_id, tenant, config.resource_key, threshold_micros),
            )
        for tombstone in snapshot.tombstones:
            connection.execute(
                "INSERT INTO policy_tombstones("
                "policy_id, target_snapshot_id, target_version, tenant, resource_key, "
                "invalidated_generation, effective_clock_ms, reason"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    snapshot.policy_id,
                    tombstone.target_snapshot_id,
                    tombstone.target_version,
                    tombstone.tenant,
                    tombstone.resource_key,
                    tombstone.invalidated_generation,
                    tombstone.effective_clock_ms,
                    tombstone.reason,
                ),
            )
        for dependency in snapshot.dependencies:
            connection.execute(
                "INSERT INTO resource_dependencies(policy_id, resource_key, depends_on) "
                "VALUES(?,?,?)",
                (snapshot.policy_id, dependency.resource_key, dependency.depends_on),
            )


def _update_merge_rows(
    connection: Any,
    dataset: Dataset,
    *,
    write_recomputed_zero: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    previous = _previous_merge_rows(connection)
    merged, closures = compute_policy_merge(dataset, previous)
    for row in merged:
        if previous and write_recomputed_zero and not row["recomputed"]:
            connection.execute(
                "UPDATE policy_merge SET recomputed=0 WHERE tenant=?",
                (row["tenant"],),
            )
            continue
        connection.execute(
            "INSERT INTO policy_merge("
            "tenant, policy_id, source_version, source_snapshot_id, effective_clock_ms, "
            "policy_generation, threshold_micros, invalidated, recomputed, previous_policy_id"
            ") VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(tenant) DO UPDATE SET "
            "policy_id=excluded.policy_id, source_version=excluded.source_version, "
            "source_snapshot_id=excluded.source_snapshot_id, "
            "effective_clock_ms=excluded.effective_clock_ms, "
            "policy_generation=excluded.policy_generation, "
            "threshold_micros=excluded.threshold_micros, "
            "invalidated=excluded.invalidated, recomputed=excluded.recomputed, "
            "previous_policy_id=excluded.previous_policy_id",
            (
                row["tenant"],
                row["policy_id"],
                row["source_version"],
                row["source_snapshot_id"],
                row["effective_clock_ms"],
                row["policy_generation"],
                row["threshold_micros"],
                row["invalidated"],
                row["recomputed"],
                row["previous_policy_id"],
            ),
        )
    if previous and write_recomputed_zero:
        for row in merged:
            row["recomputed"] = 0
    if not previous:
        connection.execute("DELETE FROM policy_invalidations")
        for closure in closures:
            connection.execute(
                "INSERT OR IGNORE INTO policy_invalidations("
                "root_snapshot_id, root_tenant, tenant, resource_key, depth, direct, reason"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    closure["root_snapshot_id"],
                    closure["root_tenant"],
                    closure["tenant"],
                    closure["resource_key"],
                    closure["depth"],
                    closure["direct"],
                    closure["reason"],
                ),
            )
    return merged, closures


def load_into_store(dataset: Dataset, store: Store, *, force: bool = False) -> dict[str, Any]:
    store.initialize()
    with store.write_tx() as connection:
        existing_digest = store.meta_get(connection, "dataset_digest")
        if existing_digest is not None:
            if existing_digest != dataset.digest and not force:
                fail(
                    "E_DATASET_CONFLICT",
                    "database already contains a different dataset; use a new database "
                    "or load --force before processing",
                    str(store.path),
                    existing=existing_digest,
                    incoming=dataset.digest,
                )
            if existing_digest == dataset.digest:
                previous = _previous_merge_rows(connection)
                merged, closures = compute_policy_merge(dataset, previous)
                for row in merged:
                    row["recomputed"] = 0
                return {
                    "ok": True,
                    "duplicate_load": True,
                    "dataset_digest": dataset.digest,
                    "applied": False,
                    "merge": merged,
                    "invalidations": closures,
                    "database": str(store.path),
                }
            if force:
                for table in (
                    "ledger_entries",
                    "drift_decisions",
                    "threshold_changes",
                    "shave_logs",
                    "scheduling_rounds",
                    "pipeline_rounds",
                    "applied_policies",
                    "policy_events",
                    "policy_invalidations",
                    "policy_merge",
                    "policy_tombstones",
                    "resource_dependencies",
                    "policy_thresholds",
                    "policies",
                    "observations",
                    "releases",
                    "applications",
                    "tenants",
                ):
                    connection.execute(f"DELETE FROM {table}")

        store.meta_set(connection, "schema_version", "1")
        store.meta_set(connection, "dataset_digest", dataset.digest)
        store.meta_set(connection, "round_span_ms", dataset.round_span_ms)
        store.meta_set(connection, "process_round_watermark", 0)
        store.meta_set(connection, "adjust_round_watermark", 0)
        store.meta_set(connection, "logical_clock_watermark_ms", 0)
        store.meta_set(connection, "target_round", dataset.target_round)
        store.meta_set(connection, "state_generation", 0)
        if dataset.snapshots:
            default_params = min(
                dataset.snapshots,
                key=lambda item: (item.effective_clock_ms, item.generation),
            ).params
        else:
            default_params = default_params_from_dataset(dataset)
        store.meta_set(connection, "default_params_json", params_to_json(default_params))

        for config in dataset.configs:
            connection.execute(
                "INSERT INTO tenants("
                "tenant, resource_key, baseline_micros, elasticity_text, lease_duration_ms, "
                "config_generation, quota_generation, threshold_micros, threshold_generation, "
                "source_version, source_snapshot_id, policy_id, effective_clock_ms, "
                "policy_generation, safety_watermark_micros"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    config.tenant,
                    config.resource_key,
                    config.baseline_micros,
                    config.elasticity_text,
                    config.lease_duration_ms,
                    config.quota_generation,
                    config.quota_generation,
                    config.baseline_micros,
                    config.quota_generation,
                    "config",
                    config.tenant,
                    "",
                    0,
                    config.quota_generation,
                    default_params.safety_watermark_micros,
                ),
            )
        for application in dataset.applications:
            connection.execute(
                "INSERT INTO applications("
                "application_seq, tenant, resource_key, requested_micros, "
                "requested_error_micros, requested_rounding, requested_input, "
                "logical_clock_ms, priority, state"
                ") VALUES(?,?,?,?,?,?,?,?,?, 'pending')",
                (
                    application.application_seq,
                    application.tenant,
                    application.resource_key,
                    application.requested_micros,
                    application.requested_error_micros,
                    application.requested_rounding,
                    application.requested_input,
                    application.logical_clock_ms,
                    application.priority,
                ),
            )
        for release in dataset.releases:
            connection.execute(
                "INSERT INTO releases("
                "release_seq, application_seq, tenant, resource_key, actual_micros, "
                "actual_error_micros, actual_rounding, actual_input, logical_clock_ms, state"
                ") VALUES(?,?,?,?,?,?,?,?,?, 'pending')",
                (
                    release.release_seq,
                    release.application_seq,
                    release.tenant,
                    release.resource_key,
                    release.actual_micros,
                    release.actual_error_micros,
                    release.actual_rounding,
                    release.actual_input,
                    release.logical_clock_ms,
                ),
            )
        for observation in dataset.observations:
            connection.execute(
                "INSERT INTO observations("
                "observation_seq, tenant, resource_key, actual_occupancy_micros, "
                "actual_error_micros, actual_rounding, actual_input, logical_clock_ms, state"
                ") VALUES(?,?,?,?,?,?,?,?, 'pending')",
                (
                    observation.observation_seq,
                    observation.tenant,
                    observation.resource_key,
                    observation.actual_occupancy_micros,
                    observation.actual_error_micros,
                    observation.actual_rounding,
                    observation.actual_input,
                    observation.logical_clock_ms,
                ),
            )
        _insert_policy_rows(connection, dataset)
        merged, closures = _update_merge_rows(
            connection, dataset, write_recomputed_zero=False
        )
        store.meta_set(connection, "state_generation", 1)
        return {
            "ok": True,
            "duplicate_load": False,
            "dataset_digest": dataset.digest,
            "applied": True,
            "tenants": len(dataset.configs),
            "applications": len(dataset.applications),
            "releases": len(dataset.releases),
            "observations": len(dataset.observations),
            "snapshots": len(dataset.snapshots),
            "target_round": dataset.target_round,
            "merge": merged,
            "invalidations": closures,
            "database": str(store.path),
        }

