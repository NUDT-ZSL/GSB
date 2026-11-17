"""Stateful alarm session with atomic incremental invalidation and recompute."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .changes import ChangeSet, changeset_from_dict, load_changeset
from .clustering import cluster_events, cluster_summary
from .errors import DomainError, spec_error
from .loading import load_inputs, validate_consistency
from .models import (
    SEVERITIES,
    SUPPORTED_OPERATORS,
    AlarmEvent,
    Edge,
    Rule,
    Topology,
)
from .rootcause import analysis_hash, analyze_clusters
from .topology import TopologyGraph
from .util import canonical_json, is_finite_number, is_int, sha256_json


def rules_version(rules: Iterable[Rule]) -> str:
    ordered = sorted(rules, key=lambda rule: rule.rule_id)
    return sha256_json([rule.to_dict() for rule in ordered])


def topology_version(topology: Topology) -> str:
    return sha256_json(topology.to_dict())


def _cluster_map(analyses: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item["cluster_id"]): dict(item) for item in analyses}


def first_analysis_difference(
    expected: Iterable[Mapping[str, Any]],
    actual: Iterable[Mapping[str, Any]],
) -> str | None:
    expected_map = _cluster_map(expected)
    actual_map = _cluster_map(actual)
    for cluster_id in sorted(set(expected_map) | set(actual_map)):
        if cluster_id not in expected_map or cluster_id not in actual_map:
            return cluster_id
        if canonical_json(expected_map[cluster_id]) != canonical_json(actual_map[cluster_id]):
            return cluster_id
    return None


def _validate_window(window_ms: int) -> None:
    if not is_int(window_ms) or window_ms <= 0:
        raise spec_error(
            "window_ms must be a positive integer",
            file="<api>",
            line=1,
            detail={"window_ms": window_ms},
        )


def _validate_rule_object(
    value: Any,
    *,
    source_file: str,
    line: int,
) -> Rule:
    if not isinstance(value, dict):
        raise spec_error("rule must be an object", file=source_file, line=line)
    rule_id = value.get("rule_id")
    service = value.get("service")
    severity = value.get("severity")
    condition = value.get("condition", value.get("metric_condition"))
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise spec_error("rule.rule_id must be non-empty", file=source_file, line=line)
    if not isinstance(service, str) or not service.strip():
        raise spec_error("rule.service must be non-empty", file=source_file, line=line)
    if severity not in SEVERITIES:
        raise spec_error(
            "rule.severity is unsupported",
            file=source_file,
            line=line,
            detail={"rule_id": rule_id, "supported": list(SEVERITIES)},
        )
    if not isinstance(condition, dict):
        raise spec_error(
            "rule.condition must be an object", file=source_file, line=line
        )
    metric = condition.get("metric")
    operator = condition.get("operator")
    threshold = condition.get("threshold")
    if not isinstance(metric, str) or not metric.strip():
        raise spec_error(
            "rule.condition.metric must be non-empty", file=source_file, line=line
        )
    if operator not in SUPPORTED_OPERATORS:
        raise spec_error(
            "rule.condition.operator is unsupported",
            file=source_file,
            line=line,
            detail={"supported": list(SUPPORTED_OPERATORS)},
        )
    if not is_finite_number(threshold):
        raise spec_error(
            "rule.condition.threshold must be finite", file=source_file, line=line
        )
    return Rule(
        rule_id=rule_id,
        service=service,
        severity=severity,
        condition={"metric": metric, "operator": operator, "threshold": threshold},
    )


def _validate_edge_object(
    value: Any,
    *,
    source_file: str,
    line: int,
) -> Edge:
    if not isinstance(value, dict):
        raise spec_error("edge must be an object", file=source_file, line=line)
    upstream = value.get("upstream")
    downstream = value.get("downstream")
    weight = value.get("weight", 1.0)
    if not isinstance(upstream, str) or not upstream.strip():
        raise spec_error("edge.upstream must be non-empty", file=source_file, line=line)
    if not isinstance(downstream, str) or not downstream.strip():
        raise spec_error(
            "edge.downstream must be non-empty", file=source_file, line=line
        )
    if upstream == downstream:
        raise spec_error(
            "self dependency edges are not allowed", file=source_file, line=line
        )
    if not is_finite_number(weight) or weight <= 0:
        raise spec_error(
            "edge.weight must be a positive finite number",
            file=source_file,
            line=line,
        )
    return Edge(upstream=upstream, downstream=downstream, weight=float(weight))


@dataclass
class AlarmSession:
    rules: tuple[Rule, ...]
    topology: Topology
    events: tuple[AlarmEvent, ...]
    event_cursor: int
    window_ms: int
    analyses: list[dict[str, Any]]
    invalidation_trace: list[dict[str, Any]]
    last_increment: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.rules = tuple(sorted(self.rules, key=lambda rule: rule.rule_id))
        self.topology = Topology(
            services=tuple(sorted(self.topology.services)),
            edges=tuple(
                sorted(self.topology.edges, key=lambda edge: (edge.upstream, edge.downstream))
            ),
        )
        self.events = tuple(
            sorted(self.events, key=lambda event: (event.timestamp, event.event_id))
        )
        _validate_window(self.window_ms)
        validate_consistency(self.rules, self.topology)
        if not is_int(self.event_cursor) or not (0 <= self.event_cursor <= len(self.events)):
            raise spec_error(
                "event_cursor is outside the event stream",
                file="<session>",
                line=1,
                detail={"event_cursor": self.event_cursor, "event_count": len(self.events)},
            )

    @classmethod
    def from_files(
        cls,
        rules_path: str | Path,
        topology_path: str | Path,
        events_path: str | Path,
        *,
        window_ms: int = 500,
    ) -> "AlarmSession":
        _validate_window(window_ms)
        rules, topology, events = load_inputs(rules_path, topology_path, events_path)
        session = cls(
            rules=rules,
            topology=topology,
            events=events,
            event_cursor=len(events),
            window_ms=window_ms,
            analyses=[],
            invalidation_trace=[],
        )
        session.analyses = session._full_analysis()
        return session

    @classmethod
    def from_components(
        cls,
        rules: Iterable[Rule],
        topology: Topology,
        events: Iterable[AlarmEvent],
        *,
        window_ms: int = 500,
        event_cursor: int | None = None,
        invalidation_trace: Iterable[Mapping[str, Any]] = (),
        last_increment: Mapping[str, Any] | None = None,
    ) -> "AlarmSession":
        event_tuple = tuple(events)
        session = cls(
            rules=tuple(rules),
            topology=topology,
            events=event_tuple,
            event_cursor=len(event_tuple) if event_cursor is None else event_cursor,
            window_ms=window_ms,
            analyses=[],
            invalidation_trace=[dict(item) for item in invalidation_trace],
            last_increment=dict(last_increment) if last_increment is not None else None,
        )
        session.analyses = session._full_analysis()
        return session

    @property
    def rules_by_id(self) -> dict[str, Rule]:
        return {rule.rule_id: rule for rule in self.rules}

    @property
    def rules_version(self) -> str:
        return rules_version(self.rules)

    @property
    def topology_version(self) -> str:
        return topology_version(self.topology)

    @property
    def active_events(self) -> tuple[AlarmEvent, ...]:
        known_rules = self.rules_by_id
        return tuple(
            event
            for event in self.events[: self.event_cursor]
            if event.rule_id in known_rules
        )

    @property
    def cluster_summary_hash(self) -> str:
        from .models import Cluster

        clusters = tuple(
            Cluster(
                cluster_id=str(item["cluster_id"]),
                members=tuple(item["cluster"]["members"]),
                services=tuple(item["cluster"]["services"]),
                start_ts=int(item["cluster"]["start_ts"]),
                end_ts=int(item["cluster"]["end_ts"]),
            )
            for item in self.analyses
        )
        return str(cluster_summary(clusters, self.window_ms)["summary_hash"])

    @property
    def analysis_hash(self) -> str:
        return analysis_hash(self.analyses)

    def _full_analysis(
        self,
        *,
        rules: tuple[Rule, ...] | None = None,
        topology: Topology | None = None,
        events: tuple[AlarmEvent, ...] | None = None,
    ) -> list[dict[str, Any]]:
        selected_rules = self.rules if rules is None else rules
        selected_topology = self.topology if topology is None else topology
        selected_events = self.active_events if events is None else events
        rules_by_id = {rule.rule_id: rule for rule in selected_rules}
        active = tuple(
            event for event in selected_events if event.rule_id in rules_by_id
        )
        clusters = cluster_events(active, selected_topology, self.window_ms)
        return analyze_clusters(clusters, active, rules_by_id, selected_topology)

    def recompute_all(self) -> list[dict[str, Any]]:
        return self._full_analysis()

    def apply_changeset(
        self,
        changeset: ChangeSet | Mapping[str, Any] | str | Path,
    ) -> dict[str, Any]:
        if isinstance(changeset, (str, Path)):
            changes = load_changeset(changeset)
        elif isinstance(changeset, ChangeSet):
            changes = changeset
        elif isinstance(changeset, Mapping):
            changes = changeset_from_dict(dict(changeset))
        else:
            raise spec_error(
                "changeset must be a mapping, ChangeSet or local JSON path",
                file="<api>",
                line=1,
            )

        old_rules = {rule.rule_id: rule for rule in self.rules}
        new_rules = dict(old_rules)
        old_topology = self.topology
        new_edges = {edge: None for edge in old_topology.edges}
        old_services = set(old_topology.services)
        source_file = changes.source_file

        rule_changed_services: set[str] = set()
        edge_changed_services: set[str] = set()
        reason_codes: list[str] = []

        # Rule additions, updates and deletions are disjoint by target and can
        # therefore be applied in operation order without ambiguity.
        for op in ("add", "update", "delete"):
            for change in changes.rule_changes:
                if change.get("op") != op:
                    continue
                if op == "add":
                    rule = _validate_rule_object(
                        change.get("rule"), source_file=source_file, line=1
                    )
                    if rule.rule_id in new_rules:
                        raise spec_error(
                            f"cannot add existing rule {rule.rule_id}",
                            file=source_file,
                            line=1,
                        )
                    for event in self.events[: self.event_cursor]:
                        if event.rule_id == rule.rule_id and event.service != rule.service:
                            raise spec_error(
                                "new rule conflicts with historical event services",
                                file=source_file,
                                line=1,
                                detail={
                                    "rule_id": rule.rule_id,
                                    "event_id": event.event_id,
                                    "event_service": event.service,
                                    "rule_service": rule.service,
                                },
                            )
                    new_rules[rule.rule_id] = rule
                    rule_changed_services.add(rule.service)
                    reason_codes.append(f"rule:add:{rule.rule_id}")
                elif op == "update":
                    rule_id = str(change.get("rule_id"))
                    current = new_rules.get(rule_id)
                    if current is None:
                        raise spec_error(
                            f"cannot update unknown rule {rule_id}",
                            file=source_file,
                            line=1,
                        )
                    patch = change.get("changes")
                    if not isinstance(patch, dict):
                        raise spec_error(
                            "rule update changes must be an object",
                            file=source_file,
                            line=1,
                        )
                    unknown = sorted(set(patch) - {"service", "severity", "condition", "metric_condition"})
                    if unknown:
                        raise spec_error(
                            "rule update contains unsupported fields",
                            file=source_file,
                            line=1,
                            detail={"fields": unknown},
                        )
                    projected = current.to_dict()
                    if "service" in patch:
                        service = patch["service"]
                        if not isinstance(service, str) or not service.strip():
                            raise spec_error(
                                "rule update service must be non-empty",
                                file=source_file,
                                line=1,
                            )
                        if service != current.service and any(
                            event.rule_id == rule_id
                            for event in self.events[: self.event_cursor]
                        ):
                            raise spec_error(
                                "cannot change the target service of a rule with retained events",
                                file=source_file,
                                line=1,
                                detail={"rule_id": rule_id},
                            )
                        projected["service"] = service
                    if "severity" in patch:
                        projected["severity"] = patch["severity"]
                    if "condition" in patch:
                        projected["condition"] = patch["condition"]
                    if "metric_condition" in patch:
                        projected["condition"] = patch["metric_condition"]
                    updated = _validate_rule_object(
                        projected, source_file=source_file, line=1
                    )
                    rule_changed_services.add(current.service)
                    rule_changed_services.add(updated.service)
                    new_rules[rule_id] = updated
                    reason_codes.append(f"rule:update:{rule_id}")
                else:
                    rule_id = str(change.get("rule_id"))
                    current = new_rules.get(rule_id)
                    if current is None:
                        raise spec_error(
                            f"cannot delete unknown rule {rule_id}",
                            file=source_file,
                            line=1,
                        )
                    del new_rules[rule_id]
                    rule_changed_services.add(current.service)
                    reason_codes.append(f"rule:delete:{rule_id}")

        # Edge additions, weight updates and deletions.
        for op in ("add", "update", "delete"):
            for change in changes.edge_changes:
                if change.get("op") != op:
                    continue
                if op == "add":
                    edge = _validate_edge_object(
                        change.get("edge"), source_file=source_file, line=1
                    )
                    if any(
                        existing.upstream == edge.upstream
                        and existing.downstream == edge.downstream
                        for existing in new_edges
                    ):
                        raise spec_error(
                            f"dependency edge {edge.upstream}->{edge.downstream} already exists",
                            file=source_file,
                            line=1,
                        )
                    new_edges[edge] = None
                    edge_changed_services.update((edge.upstream, edge.downstream))
                    reason_codes.append(f"edge:add:{edge.upstream}->{edge.downstream}")
                elif op == "update":
                    raw_edge = change.get("edge")
                    edge = _validate_edge_object(
                        raw_edge, source_file=source_file, line=1
                    )
                    matches = [
                        existing
                        for existing in new_edges
                        if existing.upstream == edge.upstream
                        and existing.downstream == edge.downstream
                    ]
                    if not matches:
                        raise spec_error(
                            f"cannot update unknown edge {edge.upstream}->{edge.downstream}",
                            file=source_file,
                            line=1,
                        )
                    existing = matches[0]
                    del new_edges[existing]
                    new_edges[edge] = None
                    edge_changed_services.update((edge.upstream, edge.downstream))
                    reason_codes.append(f"edge:update:{edge.upstream}->{edge.downstream}")
                else:
                    raw_edge = change.get("edge")
                    edge = _validate_edge_object(
                        raw_edge, source_file=source_file, line=1
                    )
                    matches = [
                        existing
                        for existing in new_edges
                        if existing.upstream == edge.upstream
                        and existing.downstream == edge.downstream
                    ]
                    if not matches:
                        raise spec_error(
                            f"cannot delete unknown edge {edge.upstream}->{edge.downstream}",
                            file=source_file,
                            line=1,
                        )
                    del new_edges[matches[0]]
                    edge_changed_services.update((edge.upstream, edge.downstream))
                    reason_codes.append(f"edge:delete:{edge.upstream}->{edge.downstream}")

        final_services = set(new_rules[rule_id].service for rule_id in new_rules)
        removed_services = sorted(old_services - final_services)
        for service in removed_services:
            dependents = sorted(
                edge.upstream for edge in new_edges if edge.downstream == service
            )
            dependencies = sorted(
                edge.downstream for edge in new_edges if edge.upstream == service
            )
            if dependents or dependencies:
                raise DomainError(
                    "E_DEPEND",
                    "cannot delete a service that still participates in dependencies",
                    file=source_file,
                    line=1,
                    detail={
                        "service": service,
                        "dependents": dependents,
                        "dependencies": dependencies,
                    },
                )
        for edge in new_edges:
            if edge.upstream not in final_services or edge.downstream not in final_services:
                raise spec_error(
                    "dependency edge references a service without a rule",
                    file=source_file,
                    line=1,
                    detail={
                        "edge": f"{edge.upstream}->{edge.downstream}",
                        "missing_services": sorted(
                            {edge.upstream, edge.downstream} - final_services
                        ),
                    },
                )

        new_topology = Topology(
            services=tuple(sorted(final_services)),
            edges=tuple(sorted(new_edges, key=lambda edge: (edge.upstream, edge.downstream))),
        )
        validate_consistency(tuple(new_rules.values()), new_topology)

        old_graph = TopologyGraph(old_topology)
        new_graph = TopologyGraph(new_topology)

        # Rule changes affect only clusters that involve the changed services.
        # Reachability-changing edge operations affect every cluster in the old
        # and new connected components of their endpoints.
        affected_services = set(rule_changed_services)
        edge_add_or_delete = any(
            change.get("op") in {"add", "delete"} for change in changes.edge_changes
        )
        if edge_add_or_delete:
            affected_services.update(edge_changed_services)
            for service in sorted(edge_changed_services):
                affected_services.update(old_graph.component(service))
                affected_services.update(new_graph.component(service))
        elif edge_changed_services:
            affected_services.update(edge_changed_services)

        invalidated = sorted(
            str(item["cluster_id"])
            for item in self.analyses
            if set(item["cluster"]["services"]) & affected_services
        )
        invalidated_set = set(invalidated)
        reused_analyses = [
            item for item in self.analyses if str(item["cluster_id"]) not in invalidated_set
        ]

        affected_event_ids: set[str] = set()
        for item in self.analyses:
            if str(item["cluster_id"]) in invalidated_set:
                affected_event_ids.update(item["cluster"]["members"])
        for event in self.events[: self.event_cursor]:
            if event.service in affected_services:
                affected_event_ids.add(event.event_id)

        event_lookup = {event.event_id: event for event in self.events[: self.event_cursor]}
        affected_events = tuple(
            event_lookup[event_id]
            for event_id in sorted(
                affected_event_ids,
                key=lambda event_id: (event_lookup[event_id].timestamp, event_id),
            )
        )
        affected_active_events = tuple(
            event for event in affected_events if event.rule_id in new_rules
        )
        new_clusters = cluster_events(
            affected_active_events, new_topology, self.window_ms
        )
        recomputed_analyses = analyze_clusters(
            new_clusters,
            affected_active_events,
            new_rules,
            new_topology,
        )

        combined = sorted(
            [*reused_analyses, *recomputed_analyses],
            key=lambda item: (item["cluster"]["start_ts"], str(item["cluster_id"])),
        )
        reused_event_ids: set[str] = set()
        for item in reused_analyses:
            members = set(item["cluster"]["members"])
            if reused_event_ids & members:
                raise DomainError(
                    "E_INCR",
                    "reused clusters overlap",
                    file=source_file,
                    line=1,
                    detail={"cluster_id": item["cluster_id"]},
                )
            reused_event_ids.update(members)
        for item in recomputed_analyses:
            overlap = reused_event_ids & set(item["cluster"]["members"])
            if overlap:
                raise DomainError(
                    "E_INCR",
                    "incremental recompute overlaps a reused cluster",
                    file=source_file,
                    line=1,
                    detail={
                        "cluster_id": item["cluster_id"],
                        "event_ids": sorted(overlap),
                    },
                )

        oracle = self._full_analysis(
            rules=tuple(new_rules.values()),
            topology=new_topology,
            events=self.events[: self.event_cursor],
        )
        difference = first_analysis_difference(oracle, combined)
        if difference is not None:
            raise DomainError(
                "E_INCR",
                "incremental recompute differs from full recompute",
                file=source_file,
                line=1,
                detail={
                    "first_difference_cluster_id": difference,
                    "recomputed_count": len(recomputed_analyses),
                    "reused_count": len(reused_analyses),
                },
            )

        recomputed_ids = sorted(str(item["cluster_id"]) for item in recomputed_analyses)
        reused_ids = sorted(str(item["cluster_id"]) for item in reused_analyses)
        total_count = len(combined)
        counts_consistent = len(recomputed_analyses) + len(reused_analyses) == total_count
        if not counts_consistent:
            raise DomainError(
                "E_INCR",
                "recompute and reuse counts do not cover the cluster set",
                file=source_file,
                line=1,
                detail={
                    "recomputed_count": len(recomputed_analyses),
                    "reused_count": len(reused_analyses),
                    "total_cluster_count": total_count,
                },
            )

        before_versions = {
            "rules": self.rules_version,
            "topology": self.topology_version,
        }
        after_versions = {
            "rules": rules_version(tuple(new_rules.values())),
            "topology": topology_version(new_topology),
        }
        reused_before = {
            str(item["cluster_id"]): canonical_json(item) for item in reused_analyses
        }
        reused_after = {
            str(item["cluster_id"]): canonical_json(item)
            for item in combined
            if str(item["cluster_id"]) in set(reused_ids)
        }
        trace_entry: dict[str, Any] = {
            "seq": len(self.invalidation_trace) + 1,
            "changeset_id": changes.changeset_id,
            "reason_codes": sorted(set(reason_codes)),
            "before_versions": before_versions,
            "after_versions": after_versions,
            "affected_services": sorted(affected_services),
            "invalidated_clusters": invalidated,
            "recomputed_clusters": recomputed_ids,
            "reused_clusters": reused_ids,
            "recomputed_count": len(recomputed_analyses),
            "reused_count": len(reused_analyses),
            "total_cluster_count": total_count,
            "counts_consistent": counts_consistent,
            "reused_hashes_verified": reused_before == reused_after,
            "full_equivalence": True,
            "first_difference_cluster_id": None,
        }

        self.rules = tuple(sorted(new_rules.values(), key=lambda rule: rule.rule_id))
        self.topology = new_topology
        self.analyses = combined
        self.invalidation_trace.append(trace_entry)
        self.last_increment = dict(trace_entry)
        return trace_entry

    def snapshot_payload(self) -> dict[str, Any]:
        return {
            "schema": "alarm-session-snapshot-v1",
            "window_ms": self.window_ms,
            "rules_version": self.rules_version,
            "topology_version": self.topology_version,
            "rules": [rule.to_dict() for rule in self.rules],
            "topology": self.topology.to_dict(),
            "events": [event.to_dict() for event in self.events],
            "event_cursor": self.event_cursor,
            "clusters": self.analyses,
            "invalidation_trace": self.invalidation_trace,
            "last_increment": self.last_increment,
        }

    def diagnostic_trace(self) -> dict[str, Any] | None:
        return dict(self.invalidation_trace[-1]) if self.invalidation_trace else None

    def save_snapshot(
        self,
        store: Any,
        *,
        label: str = "",
        omit_versions: bool = False,
    ) -> Any:
        from .snapshot import save_snapshot

        return save_snapshot(
            self, store, label=label, omit_versions=omit_versions
        )

    def restore_snapshot(
        self,
        store: Any,
        snapshot_id: str | None = None,
    ) -> tuple["AlarmSession", dict[str, Any]]:
        from .snapshot import restore_snapshot

        return restore_snapshot(store, snapshot_id, current_session=self)

    def verify_full_recompute(self) -> dict[str, Any]:
        return verify_full_recompute(self)


def apply_changeset(
    session: AlarmSession,
    changeset: ChangeSet | Mapping[str, Any] | str | Path,
) -> dict[str, Any]:
    return session.apply_changeset(changeset)


def verify_full_recompute(session: AlarmSession) -> dict[str, Any]:
    full = session.recompute_all()
    difference = first_analysis_difference(full, session.analyses)
    if difference is not None:
        raise DomainError(
            "E_INCR",
            "stored incremental result differs from full recompute",
            file="<session>",
            line=1,
            detail={"first_difference_cluster_id": difference},
        )
    return {
        "match": True,
        "cluster_count": len(full),
        "first_difference_cluster_id": None,
        "analysis_hash": analysis_hash(full),
    }
