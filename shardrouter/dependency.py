"""Route-unit dependency graph and deterministic invalidation ordering."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .errors import E_CONFIG, RouterError
from .models import RouteUnit
from .util import canonical_json, sha256_text


@dataclass(frozen=True)
class DependencyGraph:
    units: tuple[RouteUnit, ...]
    dependencies: dict[str, tuple[str, ...]]
    dependents: dict[str, tuple[str, ...]]
    version: str

    @classmethod
    def build(cls, units: Iterable[RouteUnit]) -> "DependencyGraph":
        ordered_units = tuple(sorted(units, key=lambda unit: unit.name))
        names = [unit.name for unit in ordered_units]
        if len(names) != len(set(names)):
            raise RouterError(code=E_CONFIG, message="duplicate route unit names cannot form a graph", field_path="$.route_units")
        name_set = set(names)
        dependencies: dict[str, tuple[str, ...]] = {}
        dependents_mutable: dict[str, set[str]] = {name: set() for name in names}
        for unit in ordered_units:
            for dependency in unit.depends_on:
                if dependency not in name_set:
                    raise RouterError(
                        code=E_CONFIG,
                        message=f"route unit {unit.name!r} references missing dependency {dependency!r}",
                        field_path="$.route_units",
                    )
            dependencies[unit.name] = tuple(sorted(set(unit.depends_on)))
            for dependency in dependencies[unit.name]:
                dependents_mutable[dependency].add(unit.name)
        graph = cls(
            units=ordered_units,
            dependencies={name: dependencies[name] for name in sorted(dependencies)},
            dependents={name: tuple(sorted(dependents_mutable[name])) for name in sorted(dependents_mutable)},
            version="",
        )
        order = graph.topological_order()
        if len(order) != len(names):
            raise RouterError(
                code=E_CONFIG,
                message="route unit dependency graph contains a cycle",
                field_path="$.route_units",
                details={"cycle_members": sorted(set(names) - set(order))},
            )
        payload = [
            {"depends_on": list(graph.dependencies[name]), "name": name}
            for name in sorted(graph.dependencies)
        ]
        return cls(
            units=graph.units,
            dependencies=graph.dependencies,
            dependents=graph.dependents,
            version=sha256_text(canonical_json(payload)),
        )

    def unit_map(self) -> dict[str, RouteUnit]:
        return {unit.name: unit for unit in self.units}

    def topological_order(self, subset: Iterable[str] | None = None) -> list[str]:
        known = set(self.dependencies)
        selected = known if subset is None else set(subset) & known
        indegree = {
            name: sum(1 for dependency in self.dependencies[name] if dependency in selected)
            for name in selected
        }
        ready = [name for name, count in indegree.items() if count == 0]
        heapq.heapify(ready)
        result: list[str] = []
        while ready:
            name = heapq.heappop(ready)
            result.append(name)
            for dependent in self.dependents.get(name, ()):
                if dependent not in selected:
                    continue
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(ready, dependent)
        return result

    def downstream(self, seeds: Iterable[str], *, include_seeds: bool = True) -> list[str]:
        known = set(self.dependencies)
        seed_order = sorted(set(seeds) & known)
        visited: set[str] = set(seed_order if include_seeds else ())
        queue = list(seed_order)
        while queue:
            current = queue.pop(0)
            for dependent in self.dependents.get(current, ()):
                if dependent not in visited:
                    visited.add(dependent)
                    queue.append(dependent)
        return self.topological_order(visited)

    def table_referrers(self, table_name: str) -> list[str]:
        return sorted(unit.name for unit in self.units if unit.table == table_name)

    def unit_referrers(self, unit_name: str) -> list[str]:
        return list(self.dependents.get(unit_name, ()))

    def edges(self) -> list[dict[str, str]]:
        return [
            {"dependency": dependency, "dependent": dependent}
            for dependent in sorted(self.dependencies)
            for dependency in self.dependencies[dependent]
        ]

    def invalidation_report(
        self,
        seeds: Sequence[str],
        *,
        reason: str,
        cache_entries_removed: int = 0,
        unit_results_removed: Sequence[str] = (),
    ) -> dict[str, Any]:
        ordered_seeds = sorted(set(seeds))
        order = self.downstream(ordered_seeds, include_seeds=True)
        return {
            "cache_entries_removed": cache_entries_removed,
            "order": order,
            "reason": reason,
            "seeds": ordered_seeds,
            "unit_results_removed": sorted(set(unit_results_removed)),
        }
