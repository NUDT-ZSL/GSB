"""Versioned migration engine orchestrating rules, validation and persistence."""

from __future__ import annotations

from pathlib import Path

from .errors import BusinessCode, MigrationError
from .graph import GraphState, detect_cycle
from .models import Rule, Sample
from .rules import RULE_HANDLERS
from .store import Store


class Engine:
    def __init__(self, store: Store):
        self.store = store

    @classmethod
    def bootstrap(cls, db_path: str | Path, sample: Sample) -> "Engine":
        """Create a fresh database from an empty/non-existent file and load v0."""
        path = Path(db_path)
        if path.exists():
            path.unlink()
        store = Store(path)
        store.initialize_baseline(sample.fields, sample.edges)
        return cls(store)

    def apply_version(self, version: int, rules: list[Rule]) -> dict:
        """Apply one version; all validation happens before/during one transaction."""
        if version <= self.store.last_version():
            raise MigrationError(
                BusinessCode.E_VERSION_REGRESS,
                f"version {version} is not greater than last committed version"
                f" {self.store.last_version()}",
            )
        if version != self.store.last_version() + 1:
            raise MigrationError(
                BusinessCode.E_VERSION_REGRESS,
                f"version {version} skips expected next version"
                f" {self.store.last_version() + 1}",
            )
        ordered = self._resolve_order(rules)

        state = GraphState.from_snapshot(self.store.snapshot_fields(version - 1),
                                         self.store.snapshot_edges(version - 1))
        audit_events: list[dict] = []
        for rule in ordered:
            handler = RULE_HANDLERS.get(rule.rule_type)
            if handler is None:
                raise MigrationError(
                    BusinessCode.E_RULE_ORDER,
                    f"unknown rule type {rule.rule_type}",
                    rule_id=rule.rule_id,
                )
            result = handler(state, rule, version)
            if isinstance(result, list):
                audit_events.extend(result)

        cycle = detect_cycle(state.ordered_edges())
        if cycle is not None:
            raise MigrationError(
                BusinessCode.E_CYCLE,
                "field-level cycle detected: " + " -> ".join(cycle),
            )
        self._assert_edge_endpoints(state)

        fingerprint = self.store.commit_version(
            version=version,
            fields=state.ordered_fields(),
            edges=state.ordered_edges(),
            changes=state.changes,
            audit_events=audit_events,
            rule_count=len(rules),
        )
        return {
            "version": version,
            "status": "committed",
            "field_count": len(state.fields),
            "edge_count": len(state.edges),
            "fingerprint": fingerprint,
        }

    @staticmethod
    def _assert_edge_endpoints(state: GraphState) -> None:
        for edge in state.ordered_edges():
            if edge.source_ref not in state.fields:
                raise MigrationError(
                    BusinessCode.E_TARGET_MISSING,
                    f"edge {edge.edge_id} source {edge.source_ref} does not exist",
                )
            if edge.target_ref not in state.fields:
                raise MigrationError(
                    BusinessCode.E_TARGET_MISSING,
                    f"edge {edge.edge_id} target {edge.target_ref} does not exist",
                )

    def _resolve_order(self, rules: list[Rule]) -> list[Rule]:
        """Determine execution order or raise E_RULE_ORDER.

        Two explicit mechanisms are supported:
        * ``seq``: every rule carries a unique non-null execution number;
        * ``depends_on``: a DAG of rule ids resolved by deterministic topo sort.
        If neither fully disambiguates rules touching the same field, the
        version is rejected before any graph mutation.
        """
        ids = [rule.rule_id for rule in rules]
        if len(set(ids)) != len(ids):
            raise MigrationError(BusinessCode.E_RULE_ORDER, "duplicate rule_id in version")
        by_id = {rule.rule_id: rule for rule in rules}
        has_seq = [rule for rule in rules if rule.seq is not None]
        has_deps = [rule for rule in rules if rule.depends_on]

        if has_seq and not has_deps:
            if len(has_seq) != len(rules):
                raise MigrationError(
                    BusinessCode.E_RULE_ORDER,
                    "seq must be provided for every rule when used",
                )
            seq_values = [rule.seq for rule in rules]
            if len(set(seq_values)) != len(seq_values):
                raise MigrationError(
                    BusinessCode.E_RULE_ORDER,
                    "duplicate seq values cannot determine rule order",
                )
            return sorted(rules, key=lambda rule: (rule.seq, rule.rule_id))

        if has_deps and not has_seq:
            for rule in rules:
                for dep in rule.depends_on:
                    if dep not in by_id:
                        raise MigrationError(
                            BusinessCode.E_RULE_ORDER,
                            f"rule {rule.rule_id} depends on unknown rule {dep}",
                        )
            return self._topo_sort(rules)

        if has_seq and has_deps:
            raise MigrationError(
                BusinessCode.E_RULE_ORDER,
                "mixing seq and depends_on in one version is ambiguous",
            )

        touched: dict[str, list[str]] = {}
        for rule in rules:
            for ref in rule.touches():
                touched.setdefault(ref, []).append(rule.rule_id)
        ambiguous = {ref: rule_ids for ref, rule_ids in touched.items() if len(rule_ids) > 1}
        if ambiguous:
            detail = "; ".join(f"{ref}: {', '.join(sorted(rule_ids))}"
                               for ref, rule_ids in sorted(ambiguous.items()))
            raise MigrationError(
                BusinessCode.E_RULE_ORDER,
                f"rule order cannot be determined for shared fields ({detail})",
            )
        return sorted(rules, key=lambda rule: rule.rule_id)

    @staticmethod
    def _topo_sort(rules: list[Rule]) -> list[Rule]:
        by_id = {rule.rule_id: rule for rule in rules}
        remaining = set(by_id)
        indegree = {rule.rule_id: len(set(rule.depends_on)) for rule in rules}
        dependents: dict[str, list[str]] = {rule.rule_id: [] for rule in rules}
        for rule in rules:
            for dep in set(rule.depends_on):
                dependents[dep].append(rule.rule_id)
        ordered: list[Rule] = []
        while remaining:
            ready = sorted(rule_id for rule_id in remaining if indegree[rule_id] == 0)
            if not ready:
                raise MigrationError(
                    BusinessCode.E_RULE_ORDER,
                    "cyclic depends_on relationship in version rules",
                )
            rule_id = ready[0]
            ordered.append(by_id[rule_id])
            remaining.remove(rule_id)
            for dependent in dependents[rule_id]:
                indegree[dependent] -= 1
        return ordered

    def replay_all(self, sample: Sample, *, stop_before: int | None = None) -> list[dict]:
        """Replay versions from an empty baseline DB; used for determinism checks."""
        results = []
        for version in sample.ordered_versions():
            if stop_before is not None and version >= stop_before:
                break
            results.append(self.apply_version(version, sample.versions[version]))
        return results
