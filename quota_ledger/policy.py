from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from quota_ledger.models import Dataset, PolicyParams, Snapshot


@dataclass(frozen=True)
class ThresholdSelection:
    tenant: str
    policy_id: str
    source_version: str
    source_snapshot_id: str
    effective_clock_ms: int
    policy_generation: int
    threshold_micros: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "policy_id": self.policy_id,
            "source_version": self.source_version,
            "source_snapshot_id": self.source_snapshot_id,
            "effective_clock_ms": self.effective_clock_ms,
            "policy_generation": self.policy_generation,
            "threshold_micros": self.threshold_micros,
        }


def policy_params_from_json(value: str) -> PolicyParams:
    from fractions import Fraction

    payload = json.loads(value)
    return PolicyParams(
        global_quota_budget_micros=int(payload["global_quota_budget_micros"]),
        round_threshold_budget_micros=int(
            payload["round_threshold_budget_micros"]
        ),
        per_tenant_adjustment_cap_micros=int(
            payload["per_tenant_adjustment_cap_micros"]
        ),
        safety_watermark_micros=int(payload["safety_watermark_micros"]),
        drift_threshold_ratio=Fraction(payload["drift_threshold_ratio"]),
        drift_threshold_text=str(payload["drift_threshold_ratio"]),
        danger_threshold_ratio=Fraction(payload["danger_threshold_ratio"]),
        danger_threshold_text=str(payload["danger_threshold_ratio"]),
    )


def _candidate_key(snapshot: Snapshot) -> tuple[int, int, int, str]:
    return (
        snapshot.effective_clock_ms,
        snapshot.generation,
        snapshot.version_rank,
        snapshot.snapshot_id,
    )


def _tombstone_index(dataset: Dataset) -> list[tuple[str, str, str, str, int]]:
    targets: list[tuple[str, str, str, str, int]] = []
    for snapshot in dataset.snapshots:
        for tombstone in snapshot.tombstones:
            targets.append(
                (
                    tombstone.target_snapshot_id,
                    tombstone.target_version,
                    tombstone.tenant,
                    tombstone.resource_key,
                    tombstone.invalidated_generation,
                )
            )
    return targets


def compute_policy_merge(
    dataset: Dataset, previous_rows: dict[str, dict[str, Any]] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select the latest non-tombstoned policy for every tenant.

    The invalidation closure walks tenant resource keys and the reverse resource
    dependency graph.  Only rows whose selected policy changed are marked for
    recomputation; unaffected existing conclusions are retained.
    """

    previous_rows = previous_rows or {}
    tombstones = _tombstone_index(dataset)
    config_by_tenant = dataset.config_by_tenant
    selected: dict[str, dict[str, Any]] = {}
    for tenant, config in sorted(config_by_tenant.items()):
        candidates: list[tuple[Snapshot, int]] = []
        for snapshot in dataset.snapshots:
            if tenant not in snapshot.thresholds:
                continue
            matched = False
            for target_id, target_version, target_tenant, target_resource, target_generation in tombstones:
                if (
                    snapshot.snapshot_id == target_id
                    and snapshot.version == target_version
                    and tenant == target_tenant
                    and config.resource_key == target_resource
                    and snapshot.generation == target_generation
                ):
                    matched = True
                    break
            if not matched:
                candidates.append((snapshot, snapshot.thresholds[tenant]))
        if candidates:
            snapshot, threshold = max(candidates, key=lambda item: _candidate_key(item[0]))
            selected[tenant] = {
                "tenant": tenant,
                "policy_id": snapshot.policy_id,
                "source_version": snapshot.version,
                "source_snapshot_id": snapshot.snapshot_id,
                "effective_clock_ms": snapshot.effective_clock_ms,
                "policy_generation": snapshot.generation,
                "threshold_micros": threshold,
                "invalidated": 0,
            }
        else:
            selected[tenant] = {
                "tenant": tenant,
                "policy_id": "",
                "source_version": "config",
                "source_snapshot_id": config.tenant,
                "effective_clock_ms": 0,
                "policy_generation": config.quota_generation,
                "threshold_micros": config.baseline_micros,
                "invalidated": 0,
            }

    dependency_graph: dict[str, set[str]] = {
        key: set() for key in dataset.resource_keys
    }
    for config in dataset.configs:
        dependency_graph[config.resource_key].update(config.depends_on_resource_keys)
    for snapshot in dataset.snapshots:
        for dependency in snapshot.dependencies:
            dependency_graph[dependency.resource_key].add(dependency.depends_on)
    reverse_dependencies: dict[str, set[str]] = {
        key: set() for key in dataset.resource_keys
    }
    for resource_key, dependencies in dependency_graph.items():
        for dependency in dependencies:
            reverse_dependencies.setdefault(dependency, set()).add(resource_key)
    tenants_by_resource: dict[str, set[str]] = {
        key: set() for key in dataset.resource_keys
    }
    for config in dataset.configs:
        tenants_by_resource.setdefault(config.resource_key, set()).add(config.tenant)

    closures: list[dict[str, Any]] = []
    for snapshot in dataset.snapshots:
        for tombstone in snapshot.tombstones:
            affected: dict[str, int] = {}
            queue: list[tuple[str, int]] = [(tombstone.resource_key, 0)]
            visited_resources: set[str] = set()
            while queue:
                resource_key, depth = queue.pop(0)
                if resource_key in visited_resources:
                    continue
                visited_resources.add(resource_key)
                for tenant in sorted(tenants_by_resource.get(resource_key, ())):
                    current_depth = depth
                    if tenant == tombstone.tenant:
                        current_depth = 0
                    elif tenant not in affected:
                        current_depth = max(depth, 1)
                    if tenant not in affected or current_depth < affected[tenant]:
                        affected[tenant] = current_depth
                for dependent in sorted(reverse_dependencies.get(resource_key, ())):
                    if dependent not in visited_resources:
                        queue.append((dependent, depth + 1))
            for tenant in sorted(affected):
                resource_key = config_by_tenant[tenant].resource_key
                closures.append(
                    {
                        "root_snapshot_id": snapshot.snapshot_id,
                        "root_tenant": tombstone.tenant,
                        "tenant": tenant,
                        "resource_key": resource_key,
                        "depth": affected[tenant],
                        "direct": 1 if tenant == tombstone.tenant else 0,
                        "reason": tombstone.reason,
                    }
                )
                selected[tenant]["invalidated"] = 1

    merged_rows: list[dict[str, Any]] = []
    for tenant in sorted(selected):
        row = selected[tenant]
        previous = previous_rows.get(tenant)
        changed = previous is None or (
            previous.get("policy_id") != row["policy_id"]
            or int(previous.get("threshold_micros", -1)) != row["threshold_micros"]
            or int(previous.get("invalidated", 0)) != row["invalidated"]
        )
        row["recomputed"] = 1 if changed else 0
        row["previous_policy_id"] = "" if previous is None else previous["policy_id"]
        merged_rows.append(row)
    return merged_rows, closures


def select_runtime_policy(
    connection: sqlite3.Connection, round_start_ms: int
) -> tuple[dict[str, Any] | None, dict[str, ThresholdSelection]]:
    policy_rows = connection.execute(
        "SELECT * FROM policies ORDER BY effective_clock_ms, generation, version_rank, snapshot_id"
    ).fetchall()
    if not policy_rows:
        return None, {}
    applicable = [row for row in policy_rows if row["effective_clock_ms"] <= round_start_ms]
    if applicable:
        global_row = max(
            applicable,
            key=lambda row: (
                row["effective_clock_ms"],
                row["generation"],
                row["version_rank"],
                row["snapshot_id"],
            ),
        )
    else:
        global_row = min(
            policy_rows,
            key=lambda row: (
                row["effective_clock_ms"],
                row["generation"],
                row["version_rank"],
                row["snapshot_id"],
            ),
        )
    tenant_rows = connection.execute("SELECT tenant, resource_key FROM tenants").fetchall()
    selections: dict[str, ThresholdSelection] = {}
    tombstone_rows = connection.execute(
        "SELECT target_snapshot_id, target_version, tenant, resource_key, "
        "invalidated_generation, effective_clock_ms FROM policy_tombstones"
    ).fetchall()
    for tenant_row in tenant_rows:
        tenant = tenant_row["tenant"]
        candidates = connection.execute(
            "SELECT p.policy_id, p.snapshot_id, p.version, p.generation, "
            "p.effective_clock_ms, p.version_rank, t.threshold_micros "
            "FROM policy_thresholds t JOIN policies p ON p.policy_id=t.policy_id "
            "WHERE t.tenant=? AND p.effective_clock_ms<=?",
            (tenant, round_start_ms),
        ).fetchall()
        valid = []
        for candidate in candidates:
            invalidated = False
            for tombstone in tombstone_rows:
                if (
                    tombstone["target_snapshot_id"] == candidate["snapshot_id"]
                    and tombstone["target_version"] == candidate["version"]
                    and tombstone["tenant"] == tenant
                    and tombstone["resource_key"] == tenant_row["resource_key"]
                    and tombstone["invalidated_generation"]
                    == candidate["generation"]
                    and tombstone["effective_clock_ms"] <= round_start_ms
                ):
                    invalidated = True
                    break
            if not invalidated:
                valid.append(candidate)
        if not valid:
            continue
        chosen = max(
            valid,
            key=lambda row: (
                row["effective_clock_ms"],
                row["generation"],
                row["version_rank"],
                row["snapshot_id"],
            ),
        )
        selections[tenant] = ThresholdSelection(
            tenant=tenant,
            policy_id=chosen["policy_id"],
            source_version=chosen["version"],
            source_snapshot_id=chosen["snapshot_id"],
            effective_clock_ms=chosen["effective_clock_ms"],
            policy_generation=chosen["generation"],
            threshold_micros=chosen["threshold_micros"],
        )
    return dict(global_row), selections

