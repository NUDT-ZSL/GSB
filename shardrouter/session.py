"""Routing session orchestration: versions, cache, dependencies, snapshots."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .algorithms import route_table, sort_targets
from .cache import RouteCacheKey, RoutingCache
from .config import (
    parse_config,
    ring_fingerprint,
    route_unit_fingerprint,
    table_rule_fingerprint,
)
from .dependency import DependencyGraph
from .errors import E_CACHE, E_CONFIG, E_DEPEND, E_SNAPSHOT, E_VERSION, RouterError
from .models import ConsistentHashAlgorithm, RouteUnit, RouterConfig, TableConfig
from .predicates import normalize_predicate
from .util import canonical_json, read_json, sha256_text, write_json


SNAPSHOT_SCHEMA = "shardrouter.snapshot.v1"


class RouterSession:
    """A deterministic routing session with versioned cache and snapshots."""

    def __init__(
        self,
        config: RouterConfig,
        *,
        cache_capacity: int = 32,
        history_limit: int = 32,
        expected_metadata_version: int | None = None,
    ) -> None:
        if expected_metadata_version is not None and config.metadata_version != expected_metadata_version:
            raise RouterError(
                code=E_VERSION,
                message=(
                    "metadata version mismatch: "
                    f"configuration has {config.metadata_version}, expected {expected_metadata_version}"
                ),
                field_path="$.metadata_version",
                details={
                    "actual": config.metadata_version,
                    "expected": expected_metadata_version,
                    "source": "configuration",
                },
            )
        if isinstance(history_limit, bool) or not isinstance(history_limit, int) or history_limit <= 0:
            raise RouterError(code=E_CONFIG, message="history_limit must be a positive integer")
        self.config = config
        self.dependency_graph = DependencyGraph.build(config.route_units)
        self.cache = RoutingCache(capacity=cache_capacity)
        self.history_limit = history_limit
        self._table_map = {table.name.casefold(): table for table in config.tables}
        self._unit_map = {unit.name.casefold(): unit for unit in config.route_units}
        self.unit_results: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self.expected_metadata_version = expected_metadata_version
        self.last_invalidation: dict[str, Any] = {
            "cache_entries_removed": 0,
            "order": [],
            "reason": "initial_load",
            "seeds": [],
            "unit_results_removed": [],
        }

    # ------------------------------------------------------------------
    # Lookup and version state
    # ------------------------------------------------------------------
    def table(self, name: str) -> TableConfig:
        table = self._table_map.get(name.casefold())
        if table is None:
            raise RouterError(
                code=E_CONFIG,
                message=f"logical table {name!r} does not exist",
                field_path="$.tables",
            )
        return table

    def route_unit_definition(self, name: str) -> RouteUnit:
        unit = self._unit_map.get(name.casefold())
        if unit is None:
            raise RouterError(
                code=E_CONFIG,
                message=f"route unit {name!r} does not exist",
                field_path="$.route_units",
            )
        return unit

    def version_state(self) -> dict[str, Any]:
        return {
            "config_version": self.config.config_version,
            "dependency_graph_version": self.dependency_graph.version,
            "metadata_version": self.config.metadata_version,
            "ring_versions": {
                table.name: table.ring_version if table.ring_version is not None else 0
                for table in sorted(self.config.tables, key=lambda item: item.name)
            },
            "table_versions": {
                table.name: table.version
                for table in sorted(self.config.tables, key=lambda item: item.name)
            },
        }

    def dependency_edges(self) -> list[dict[str, str]]:
        return self.dependency_graph.edges()

    def _record_history(self, event: Mapping[str, Any]) -> None:
        self.history.append({str(key): value for key, value in event.items()})
        if len(self.history) > self.history_limit:
            self.history = self.history[-self.history_limit :]

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def _cache_key(
        self,
        table: TableConfig,
        predicate_info: Any,
    ) -> RouteCacheKey:
        ring_version = table.ring_version if table.ring_version is not None else 0
        return RouteCacheKey(
            table=table.name,
            normalized_predicate=predicate_info.normalized,
            rule_version=table.version,
            ring_version=ring_version,
        )

    def route_with_cache(self, table_name: str, predicate: str) -> dict[str, Any]:
        table = self.table(table_name)
        info = normalize_predicate(table, predicate)
        key = self._cache_key(table, info)
        lookup = self.cache.lookup(
            key,
            current_rule_version=table.version,
            current_ring_version=key.ring_version,
        )
        cache_status = lookup.status
        cache_code = lookup.code
        if lookup.entry is not None:
            plan = copy.deepcopy(lookup.entry.plan)
        else:
            plan = route_table(table, info)
            self.cache.put(
                key,
                copy.deepcopy(plan),
                recorded_rule_version=table.version,
                recorded_ring_version=key.ring_version,
            )
        if lookup.status == "miss":
            cache_status = "miss"
        elif lookup.status == "stale":
            cache_status = "recomputed"
            cache_code = E_CACHE
        self._record_history(
            {
                "action": "route",
                "algorithm": plan["routing"]["algorithm"],
                "cache_code": cache_code,
                "cache_status": cache_status,
                "routing_id": plan["predicate"]["routing_id"],
                "table": plan["table"],
                "target_count": plan["routing"]["target_count"],
            }
        )
        return {
            "cache": {"code": cache_code, "status": cache_status},
            "invalidation": copy.deepcopy(self.last_invalidation),
            "plan": plan,
        }

    def route(self, table_name: str, predicate: str) -> dict[str, Any]:
        return self.route_with_cache(table_name, predicate)["plan"]

    def _ancestor_subset(self, unit_name: str) -> set[str]:
        subset = {unit_name}
        pending = [unit_name]
        while pending:
            current = pending.pop()
            for dependency in self.dependency_graph.dependencies.get(current, ()):
                if dependency not in subset:
                    subset.add(dependency)
                    pending.append(dependency)
        return subset

    def route_unit(self, unit_name: str) -> dict[str, Any]:
        requested = self.route_unit_definition(unit_name).name
        subset = self._ancestor_subset(requested)
        order = self.dependency_graph.topological_order(subset)
        local_results: dict[str, dict[str, Any]] = {}
        invalidation_events: list[dict[str, Any]] = []
        cache_events: list[dict[str, Any]] = []
        for name in order:
            definition = self.route_unit_definition(name)
            base_plan: dict[str, Any] | None = None
            base_outcome: dict[str, Any] | None = None
            if definition.table is not None and definition.predicate is not None:
                base_outcome = self.route_with_cache(definition.table, definition.predicate)
                base_plan = base_outcome["plan"]
                cache_events.append(
                    {"unit": name, **copy.deepcopy(base_outcome["cache"])}
                )
            targets: list[dict[str, Any]] = []
            if base_plan is not None:
                targets.extend(base_plan["routing"]["targets"])
            for dependency in definition.depends_on:
                targets.extend(local_results[dependency]["targets"])
            ordered_targets = sort_targets(targets)
            result: dict[str, Any] = {
                "base_plan": base_plan,
                "dependencies": list(definition.depends_on),
                "table": definition.table,
                "target_count": len(ordered_targets),
                "targets": ordered_targets,
                "unit": name,
            }
            fingerprint = sha256_text(canonical_json(result))
            result["result_id"] = fingerprint
            previous = self.unit_results.get(name)
            if previous is not None and previous.get("result_id") != fingerprint:
                downstream = self.dependency_graph.downstream([name], include_seeds=False)
                removed = [
                    candidate for candidate in downstream if candidate in self.unit_results
                ]
                for candidate in removed:
                    self.unit_results.pop(candidate, None)
                cache_removed = self.cache.clear()
                event = self.dependency_graph.invalidation_report(
                    [name],
                    reason=f"route_unit_result_changed:{name}",
                    cache_entries_removed=cache_removed,
                    unit_results_removed=removed,
                )
                event["seeds"] = [name]
                event["order"] = self.dependency_graph.downstream([name], include_seeds=False)
                invalidation_events.append(event)
                self.last_invalidation = copy.deepcopy(event)
            self.unit_results[name] = {"result": copy.deepcopy(result), "result_id": fingerprint}
            local_results[name] = result

        final_result = local_results[requested]
        self._record_history(
            {
                "action": "route_unit",
                "result_id": final_result["result_id"],
                "target_count": final_result["target_count"],
                "unit": requested,
            }
        )
        report: dict[str, Any] = {
            "cache_events": cache_events,
            "invalidation_events": invalidation_events,
            "result": final_result,
            "unit": requested,
        }
        return report

    # ------------------------------------------------------------------
    # Version evolution and dependency-aware invalidation
    # ------------------------------------------------------------------
    def _reconcile_tables(
        self,
        new_config: RouterConfig,
    ) -> tuple[tuple[TableConfig, ...], set[str], set[str]]:
        old_tables = {table.name: table for table in self.config.tables}
        new_tables: list[TableConfig] = []
        changed_by_rule: set[str] = set()
        changed_by_ring: set[str] = set()
        for table in sorted(new_config.tables, key=lambda item: item.name):
            previous = old_tables.get(table.name)
            if previous is None:
                changed_by_rule.add(table.name)
                new_tables.append(table)
                continue
            rule_changed = table_rule_fingerprint(previous) != table_rule_fingerprint(table)
            ring_changed = ring_fingerprint(previous) != ring_fingerprint(table)
            if rule_changed:
                changed_by_rule.add(table.name)
            if ring_changed:
                changed_by_ring.add(table.name)
            rule_version = table.version
            if rule_changed:
                rule_version = max(table.version, previous.version + 1)
            else:
                rule_version = max(table.version, previous.version)
            adjusted = table.with_version(rule_version)
            if isinstance(adjusted.algorithm, ConsistentHashAlgorithm):
                previous_ring_version = previous.ring_version if previous.ring_version is not None else 0
                ring_version = adjusted.algorithm.ring_version
                if ring_changed:
                    ring_version = max(ring_version, previous_ring_version + 1)
                else:
                    ring_version = max(ring_version, previous_ring_version)
                adjusted = adjusted.with_algorithm(
                    replace(adjusted.algorithm, ring_version=ring_version)
                )
            if adjusted != table:
                if adjusted.version != table.version:
                    changed_by_rule.add(table.name)
                if adjusted.ring_version != table.ring_version:
                    changed_by_ring.add(table.name)
            new_tables.append(adjusted)
        removed = set(old_tables) - {table.name for table in new_config.tables}
        changed_by_rule |= removed
        return tuple(new_tables), changed_by_rule, changed_by_ring

    def apply_config(self, new_config: RouterConfig) -> dict[str, Any]:
        """Atomically evolve configuration and invalidate affected caches."""

        if (
            self.expected_metadata_version is not None
            and new_config.metadata_version != self.expected_metadata_version
        ):
            raise RouterError(
                code=E_VERSION,
                message=(
                    "metadata version mismatch while applying configuration: "
                    f"actual {new_config.metadata_version}, expected {self.expected_metadata_version}"
                ),
                field_path="$.metadata_version",
                details={
                    "actual": new_config.metadata_version,
                    "expected": self.expected_metadata_version,
                    "source": "apply_config",
                },
            )
        old_config = self.config
        old_graph = self.dependency_graph
        old_units = {unit.name: unit for unit in old_config.route_units}
        new_units = {unit.name: unit for unit in new_config.route_units}
        changed_units = {
            name
            for name in set(old_units) | set(new_units)
            if (old_units.get(name) != new_units.get(name))
        }
        reconciled_tables, changed_by_rule, changed_by_ring = self._reconcile_tables(new_config)
        changed_any = bool(
            changed_by_rule
            or changed_by_ring
            or changed_units
            or old_config.metadata_version != new_config.metadata_version
        )
        config_version = new_config.config_version
        if changed_any:
            config_version = max(config_version, old_config.config_version + 1)
        else:
            config_version = max(config_version, old_config.config_version)
        reconciled = new_config.with_updates(
            config_version=config_version,
            tables=reconciled_tables,
        )
        new_graph = DependencyGraph.build(reconciled.route_units)
        table_seeds: set[str] = set()
        for table_name in changed_by_rule | changed_by_ring:
            table_seeds.update(old_graph.table_referrers(table_name))
            table_seeds.update(new_graph.table_referrers(table_name))
        seeds = set(table_seeds) | changed_units
        old_order = old_graph.downstream(seeds, include_seeds=True)
        new_order = new_graph.downstream(seeds, include_seeds=True)
        union_order = set(old_order) | set(new_order)
        order = new_graph.topological_order(union_order)
        order.extend(name for name in old_order if name in union_order and name not in order)
        unit_results_removed = [name for name in self.unit_results if name in set(order)]
        for name in unit_results_removed:
            self.unit_results.pop(name, None)
        cache_entries_removed = self.cache.clear() if changed_any else 0
        self.config = reconciled
        self.dependency_graph = new_graph
        self._table_map = {table.name.casefold(): table for table in reconciled.tables}
        self._unit_map = {unit.name.casefold(): unit for unit in reconciled.route_units}
        self.last_invalidation = {
            "cache_entries_removed": cache_entries_removed,
            "order": order,
            "reason": "configuration_changed",
            "seeds": sorted(seeds),
            "unit_results_removed": sorted(unit_results_removed),
        }
        report = {
            "changed_route_units": sorted(changed_units),
            "changed_tables_by_ring": sorted(changed_by_ring),
            "changed_tables_by_rule": sorted(changed_by_rule),
            "config_version": reconciled.config_version,
            "invalidation": copy.deepcopy(self.last_invalidation),
            "metadata_version": reconciled.metadata_version,
            "mode": "applied",
            "status": "ok",
        }
        self._record_history(
            {
                "action": "apply_config",
                "config_version": reconciled.config_version,
                "invalidation_count": len(order),
            }
        )
        return report

    def remove_table_rule(self, table_name: str) -> dict[str, Any]:
        table = self.table(table_name)
        referrers = self.dependency_graph.table_referrers(table.name)
        if referrers:
            raise RouterError(
                code=E_DEPEND,
                message=f"cannot delete shard rule {table.name!r}; it is still referenced",
                field_path="$.tables",
                details={"referrers": referrers, "table": table.name},
            )
        tables = tuple(item for item in self.config.tables if item.name != table.name)
        candidate = self.config.with_updates(tables=tables)
        return self.apply_config(candidate)

    def remove_route_unit(self, unit_name: str) -> dict[str, Any]:
        unit = self.route_unit_definition(unit_name)
        referrers = self.dependency_graph.unit_referrers(unit.name)
        if referrers:
            raise RouterError(
                code=E_DEPEND,
                message=f"cannot delete route unit {unit.name!r}; it is still referenced",
                field_path="$.route_units",
                details={"referrers": referrers, "unit": unit.name},
            )
        units = tuple(item for item in self.config.route_units if item.name != unit.name)
        return self.apply_config(self.config.with_updates(route_units=units))

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        unit_results = [
            copy.deepcopy(self.unit_results[name])
            for name in sorted(self.unit_results)
        ]
        return {
            "cache": self.cache.to_dict(),
            "config": self.config.to_dict(),
            "dependency_graph_version": self.dependency_graph.version,
            "route_history": copy.deepcopy(self.history),
            "schema": SNAPSHOT_SCHEMA,
            "unit_results": unit_results,
            "versions": self.version_state(),
        }

    def save_snapshot(self, path: str | Path) -> dict[str, Any]:
        snapshot = self.snapshot()
        write_json(path, snapshot, pretty=True)
        return {
            "cache_entries": len(self.cache),
            "config_version": self.config.config_version,
            "dependency_graph_version": self.dependency_graph.version,
            "route_history_entries": len(self.history),
            "status": "saved",
        }

    @staticmethod
    def load_snapshot(path: str | Path) -> dict[str, Any]:
        try:
            payload = read_json(path)
        except FileNotFoundError as error:
            raise RouterError(code=E_SNAPSHOT, message="snapshot file not found", field_path="$") from error
        except json.JSONDecodeError as error:
            raise RouterError(
                code=E_SNAPSHOT,
                message="snapshot is not valid JSON",
                field_path="$",
                details={"line": error.lineno, "column": error.colno},
            ) from error
        if not isinstance(payload, dict) or payload.get("schema") != SNAPSHOT_SCHEMA:
            raise RouterError(
                code=E_SNAPSHOT,
                message=f"snapshot schema must be {SNAPSHOT_SCHEMA!r}",
                field_path="$.schema",
            )
        return payload

    def _snapshot_differences(self, snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
        snapshot_versions = snapshot.get("versions")
        if not isinstance(snapshot_versions, Mapping):
            raise RouterError(code=E_SNAPSHOT, message="snapshot versions must be an object", field_path="$.versions")
        current_versions = self.version_state()
        comparisons: list[tuple[str, Any, Any]] = [
            ("config_version", current_versions["config_version"], snapshot_versions.get("config_version")),
            (
                "dependency_graph_version",
                current_versions["dependency_graph_version"],
                snapshot.get("dependency_graph_version"),
            ),
            ("metadata_version", current_versions["metadata_version"], snapshot_versions.get("metadata_version")),
        ]
        for field_name in ("ring_versions", "table_versions"):
            current = current_versions[field_name]
            previous = snapshot_versions.get(field_name, {})
            if not isinstance(previous, Mapping):
                raise RouterError(
                    code=E_SNAPSHOT,
                    message=f"snapshot {field_name} must be an object",
                    field_path=f"$.versions.{field_name}",
                )
            for name in sorted(set(current) | set(previous)):
                comparisons.append(
                    (f"{field_name}.{name}", current.get(name), previous.get(name))
                )
        differences = [
            {"current": current, "field": field, "snapshot": previous}
            for field, current, previous in comparisons
            if current != previous
        ]
        differences.sort(key=lambda item: item["field"])
        return differences

    def restore_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        expected_metadata_version: int | None = None,
    ) -> dict[str, Any]:
        """Whole-state replacement from a snapshot.

        Snapshot versions are compared first.  A mismatch never triggers an
        incremental merge: configuration, dependency graph, plan cache, unit
        result cache, and history are all replaced atomically.
        """

        if not isinstance(snapshot, Mapping) or snapshot.get("schema") != SNAPSHOT_SCHEMA:
            raise RouterError(
                code=E_SNAPSHOT,
                message=f"snapshot schema must be {SNAPSHOT_SCHEMA!r}",
                field_path="$.schema",
            )
        raw_config = snapshot.get("config")
        if not isinstance(raw_config, Mapping):
            raise RouterError(code=E_SNAPSHOT, message="snapshot config must be an object", field_path="$.config")
        snapshot_config = parse_config(copy.deepcopy(dict(raw_config)), source="<snapshot>")
        snapshot_versions = snapshot.get("versions")
        if isinstance(snapshot_versions, Mapping):
            snapshot_metadata = snapshot_versions.get("metadata_version", snapshot_config.metadata_version)
        else:
            snapshot_metadata = snapshot_config.metadata_version
        if snapshot_metadata != snapshot_config.metadata_version:
            raise RouterError(
                code=E_VERSION,
                message="snapshot metadata version disagrees with embedded configuration",
                field_path="$.metadata_version",
                details={
                    "actual": snapshot_config.metadata_version,
                    "expected": snapshot_metadata,
                    "source": "snapshot",
                },
            )
        effective_expected = (
            expected_metadata_version
            if expected_metadata_version is not None
            else self.expected_metadata_version
        )
        if effective_expected is not None and snapshot_config.metadata_version != effective_expected:
            raise RouterError(
                code=E_VERSION,
                message=(
                    "snapshot metadata version mismatch: "
                    f"snapshot has {snapshot_config.metadata_version}, expected {effective_expected}"
                ),
                field_path="$.metadata_version",
                details={
                    "actual": snapshot_config.metadata_version,
                    "expected": effective_expected,
                    "source": "snapshot",
                },
            )
        differences = self._snapshot_differences(snapshot)
        previous_config_version = self.config.config_version
        previous_dep_version = self.dependency_graph.version
        mismatch = any(
            item["field"] in ("config_version", "dependency_graph_version")
            for item in differences
        )
        replacement = RouterSession(
            snapshot_config,
            cache_capacity=int(snapshot.get("cache", {}).get("capacity", self.cache.capacity))
            if isinstance(snapshot.get("cache", {}), Mapping)
            else self.cache.capacity,
            history_limit=self.history_limit,
            expected_metadata_version=effective_expected,
        )
        raw_cache = snapshot.get("cache")
        if not isinstance(raw_cache, Mapping):
            raise RouterError(code=E_SNAPSHOT, message="snapshot cache must be an object", field_path="$.cache")
        replacement.cache = RoutingCache.from_dict(raw_cache)
        raw_unit_results = snapshot.get("unit_results", [])
        if not isinstance(raw_unit_results, list):
            raise RouterError(
                code=E_SNAPSHOT,
                message="snapshot unit_results must be an array",
                field_path="$.unit_results",
            )
        unit_results: dict[str, dict[str, Any]] = {}
        for index, raw_result in enumerate(raw_unit_results):
            if not isinstance(raw_result, Mapping):
                raise RouterError(
                    code=E_SNAPSHOT,
                    message=f"unit result {index} must be an object",
                    field_path=f"$.unit_results[{index}]",
                )
            result = copy.deepcopy(dict(raw_result))
            unit_name = result.get("result", {}).get("unit") if isinstance(result.get("result"), Mapping) else None
            if not isinstance(unit_name, str):
                raise RouterError(
                    code=E_SNAPSHOT,
                    message=f"unit result {index} has no stable unit name",
                    field_path=f"$.unit_results[{index}].result.unit",
                )
            unit_results[unit_name] = result
        raw_history = snapshot.get("route_history", [])
        if not isinstance(raw_history, list):
            raise RouterError(
                code=E_SNAPSHOT,
                message="snapshot route_history must be an array",
                field_path="$.route_history",
            )
        replacement.unit_results = unit_results
        replacement.history = copy.deepcopy(raw_history[-self.history_limit :])
        replacement.last_invalidation = {
            "cache_entries_removed": 0,
            "order": [],
            "reason": "snapshot_restored",
            "seeds": [],
            "unit_results_removed": [],
        }

        # Atomic state swap.  No current-session cache entry is merged into the
        # replacement state.
        self.config = replacement.config
        self.dependency_graph = replacement.dependency_graph
        self.cache = replacement.cache
        self._table_map = replacement._table_map
        self._unit_map = replacement._unit_map
        self.unit_results = replacement.unit_results
        self.history = replacement.history
        self.last_invalidation = replacement.last_invalidation
        report = {
            "cache_entries_replaced": len(self.cache),
            "current_config_version_before": previous_config_version,
            "current_dependency_graph_version_before": previous_dep_version,
            "differences": differences,
            "mode": "whole_replace" if mismatch else "replace_same_versions",
            "restored_config_version": self.config.config_version,
            "restored_dependency_graph_version": self.dependency_graph.version,
            "route_history_entries": len(self.history),
            "status": "restored",
        }
        self._record_history(
            {
                "action": "restore_snapshot",
                "mode": report["mode"],
                "restored_config_version": self.config.config_version,
            }
        )
        return report

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------
    def report(self) -> dict[str, Any]:
        return {
            "cache_entries": [entry.to_dict() for entry in self.cache.entries()],
            "dependency_edges": self.dependency_edges(),
            "last_invalidation": copy.deepcopy(self.last_invalidation),
            "route_history": copy.deepcopy(self.history),
            "versions": self.version_state(),
        }


def route_predicate(config: RouterConfig, table: str, predicate: str) -> dict[str, Any]:
    return RouterSession(config).route(table, predicate)


def save_snapshot(session: RouterSession, path: str | Path) -> dict[str, Any]:
    return session.save_snapshot(path)


def restore_snapshot(
    session: RouterSession,
    path: str | Path,
    *,
    expected_metadata_version: int | None = None,
) -> dict[str, Any]:
    snapshot = RouterSession.load_snapshot(path)
    return session.restore_snapshot(
        snapshot,
        expected_metadata_version=expected_metadata_version,
    )
