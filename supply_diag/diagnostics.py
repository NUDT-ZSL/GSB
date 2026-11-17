"""Dependency diagnostics, error propagation, and integrity decisions."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable

from .errors import (
    E_CONSTRAINT_UNSAT,
    E_CYCLE,
    E_HASH_ALGO,
    E_HASH_MISMATCH,
    E_TRUTH_MISMATCH,
    E_UNKNOWN_DEPENDENCY,
    W_BOUND_UNCERTAIN,
    W_SIGNATURE_MISSING,
    Issue,
)
from .graph import DependencyGraph, shortest_paths_from_source
from .manifest import Component, HASH_ALGORITHMS, ParsedManifest
from .truth import verify_manifest_truth
from .versioning import (
    IntersectionResult,
    VersionContext,
    fraction_to_dict,
    intersect_intervals,
)


HASH_ROUNDING_PER_LINK = Fraction(1, 1 << 256)
MAX_STORED_SIGNATURE_PATHS_PER_COMPONENT = 64
MAX_FALLBACK_PATHS = 100_000


def canonical_content_bytes(content: Any) -> bytes:
    """Canonicalize component content before hashing."""

    if isinstance(content, str):
        return content.encode("utf-8")
    return json.dumps(
        content,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass
class ComponentDiagnostic:
    component: Component
    intersection: IntersectionResult
    incoming_constraints: list[dict]
    hash_status: str
    hash_verification: str
    computed_hash: str | None
    hash_rounding_error: Fraction | None
    signature_status: str
    signature_missing_source: bool
    signature_missing_by: list[dict] = field(default_factory=list)
    local_version_error: Fraction | None = None
    cumulative_version_error: Fraction | None = None
    cumulative_hash_error: Fraction | None = None
    path: tuple[str, ...] = ()
    path_status: str = "root_to_component"
    cycle_member: bool = False
    integrity_status: str = "incomplete"
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def component_id(self) -> str:
        return self.component.component_id

    @property
    def version_status(self) -> str:
        return self.intersection.status

    @property
    def signature_missing(self) -> bool:
        return bool(self.signature_missing_by)

    @property
    def complete(self) -> bool:
        return self.integrity_status == "complete"

    def to_dict(self, context: VersionContext) -> dict:
        return {
            "component_id": self.component_id,
            "entry_index": self.component.entry_index,
            "declared_version": self.component.declared_version,
            "normalized_version": self.component.version_interval.to_dict(context),
            "hash": {
                "algorithm": self.component.hash_algorithm,
                "declared_digest": self.component.content_hash,
                "digest_bits": HASH_ALGORITHMS[self.component.hash_algorithm],
                "status": self.hash_status,
                "verification": self.hash_verification,
                "computed_digest": self.computed_hash,
            },
            "signature": {
                "status": self.signature_status,
                "missing_source": self.signature_missing_source,
                "missing_propagated": any(
                    item["source"] != self.component_id
                    for item in self.signature_missing_by
                ),
                "missing_by": self.signature_missing_by,
            },
            "incoming_constraints": self.incoming_constraints,
            "version_constraint": self.intersection.to_dict(context),
            "version_status": self.version_status,
            "error_bounds": {
                "version": {
                    "local_width": fraction_to_dict(self.local_version_error),
                    "cumulative_width": fraction_to_dict(self.cumulative_version_error),
                    "path": list(self.path),
                    "path_status": self.path_status,
                    "unit": "quantum",
                },
                "hash_rounding": {
                    "per_link": fraction_to_dict(self.hash_rounding_error),
                    "cumulative": fraction_to_dict(self.cumulative_hash_error),
                    "unit": "2^-digest_bits",
                    "digest_bits": HASH_ALGORITHMS[self.component.hash_algorithm],
                },
            },
            "cycle_member": self.cycle_member,
            "integrity_status": self.integrity_status,
            "complete": self.integrity_status == "complete",
            "errors": [issue.to_dict() for issue in _sorted_issues(self.errors)],
            "warnings": [issue.to_dict() for issue in _sorted_issues(self.warnings)],
        }


@dataclass
class ManifestDiagnostic:
    manifest: ParsedManifest
    graph: DependencyGraph
    context: VersionContext
    components: dict[str, ComponentDiagnostic]
    cycles: list[dict]
    truth_check: dict
    errors: list[Issue]
    warnings: list[Issue]
    status: str
    conclusion: str
    path_enumeration_truncated: bool = False

    def uncertain_items(self) -> list[dict]:
        return [
            {
                "component_id": diagnostic.component_id,
                "reason": diagnostic.intersection.reason,
                "version_constraint": diagnostic.intersection.to_dict(self.context),
            }
            for diagnostic in sorted(
                self.components.values(), key=lambda item: item.component_id
            )
            if diagnostic.version_status == "uncertain"
        ]

    def summary(self) -> dict:
        components = list(self.components.values())
        return {
            "component_count": len(components),
            "complete_count": sum(item.complete for item in components),
            "incomplete_count": sum(
                item.integrity_status == "incomplete" for item in components
            ),
            "indeterminate_count": sum(
                item.integrity_status == "indeterminate" for item in components
            ),
            "satisfiable_count": sum(
                item.version_status == "satisfiable" for item in components
            ),
            "unsatisfiable_count": sum(
                item.version_status == "unsatisfiable" for item in components
            ),
            "uncertain_count": sum(
                item.version_status == "uncertain" for item in components
            ),
            "signature_missing_count": sum(
                item.signature_missing for item in components
            ),
            "rejected_entry_count": len(self.manifest.rejections),
            "cycle_count": len(self.cycles),
        }

    def to_dict(self) -> dict:
        components = [
            self.components[component_id].to_dict(self.context)
            for component_id in sorted(self.components)
        ]
        return {
            "source": self.manifest.source_path,
            "file_digest": self.manifest.file_digest,
            "content_digest": self.manifest.content_digest,
            "project_id": self.manifest.project_id,
            "manifest_version": self.manifest.manifest_version_text,
            "status": self.status,
            "conclusion": self.conclusion,
            "version_resolution": self.context.to_dict(),
            "hash_algorithm_whitelist": sorted(HASH_ALGORITHMS),
            "counts": self.summary(),
            "roots": list(self.graph.roots),
            "errors": [issue.to_dict() for issue in _sorted_issues(self.errors)],
            "warnings": [issue.to_dict() for issue in _sorted_issues(self.warnings)],
            "rejections": sorted(
                (rejection.to_dict() for rejection in self.manifest.rejections),
                key=lambda item: (
                    item["entry_index"],
                    item.get("component_id", ""),
                    item["codes"],
                ),
            ),
            "cycles": self.cycles,
            "cycle_enumeration_truncated": self.graph.truncated_cycles,
            "path_enumeration_truncated": self.path_enumeration_truncated,
            "components": components,
            "uncertain_items": self.uncertain_items(),
            "truth_check": self.truth_check,
        }


def _sorted_issues(issues: Iterable[Issue]) -> list[Issue]:
    return sorted(
        issues,
        key=lambda issue: (
            issue.code,
            issue.entry_index if issue.entry_index is not None else -1,
            issue.component_id or "",
            issue.field or "",
            issue.message,
            json.dumps(issue.details, sort_keys=True, ensure_ascii=False, default=str),
        ),
    )


def _topological_order(graph: DependencyGraph) -> list[str] | None:
    indegree = {node: 0 for node in graph.nodes}
    for node in graph.nodes:
        for target in graph.adjacency[node]:
            indegree[target] += 1
    ready = sorted(node for node in graph.nodes if indegree[node] == 0)
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for target in graph.adjacency[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    if len(order) != len(graph.nodes):
        return None
    return order


def _better_candidate(
    candidate: tuple[tuple[str, ...], Fraction | None],
    current: tuple[tuple[str, ...], Fraction | None] | None,
) -> bool:
    if current is None:
        return True
    candidate_path, candidate_error = candidate
    current_path, current_error = current
    if candidate_error is None and current_error is not None:
        return True
    if candidate_error is not None and current_error is None:
        return False
    if candidate_error is not None and current_error is not None:
        if candidate_error != current_error:
            return candidate_error > current_error
    return (len(candidate_path), candidate_path) < (len(current_path), current_path)


def _combine_error(
    left: Fraction | None,
    right: Fraction | None,
) -> Fraction | None:
    if left is None or right is None:
        return None
    return left + right


def _best_cumulative_paths(
    graph: DependencyGraph,
    local_errors: dict[str, Fraction | None],
) -> tuple[dict[str, tuple[tuple[str, ...], Fraction | None]], bool]:
    """Choose worst-case cumulative version error with a canonical tie-break."""

    order = _topological_order(graph)
    candidates: dict[str, tuple[tuple[str, ...], Fraction | None]] = {}
    truncated = False
    if order is not None:
        for node in order:
            if node in graph.roots:
                candidates[node] = ((node,), local_errors[node])
            for target in graph.adjacency[node]:
                if node not in candidates:
                    continue
                source_path, source_error = candidates[node]
                candidate = (
                    source_path + (target,),
                    _combine_error(source_error, local_errors[target]),
                )
                if _better_candidate(candidate, candidates.get(target)):
                    candidates[target] = candidate
    else:
        starts = graph.roots if graph.roots else graph.nodes
        stack: list[tuple[str, tuple[str, ...], frozenset[str], Fraction | None]] = []
        for start in sorted(starts):
            stack.append((start, (start,), frozenset({start}), local_errors[start]))
        explored = 0
        while stack:
            node, path, visited, error = stack.pop()
            explored += 1
            if explored > MAX_FALLBACK_PATHS:
                truncated = True
                break
            candidate = (path, error)
            if _better_candidate(candidate, candidates.get(node)):
                candidates[node] = candidate
            for target in reversed(graph.adjacency[node]):
                if target in visited:
                    continue
                stack.append(
                    (
                        target,
                        path + (target,),
                        visited | {target},
                        _combine_error(error, local_errors[target]),
                    )
                )
    for node in graph.nodes:
        if node not in candidates:
            candidates[node] = ((node,), local_errors[node])
    return candidates, truncated


def _hash_component(component: Component) -> tuple[str, str, str | None]:
    algorithm = component.hash_algorithm
    if not component.content_present:
        return "format_only", "not_requested", None
    try:
        content = canonical_content_bytes(component.content)
    except (TypeError, ValueError):
        return "algorithm_error", "canonicalization_failed", None
    if algorithm == "sha256":
        return (
            "valid",
            "content_digest",
            hashlib.sha256(content).hexdigest(),
        )
    if algorithm == "hmac-sha256":
        key = component.hmac_key
        if isinstance(key, str):
            key = key.encode("utf-8")
        if not isinstance(key, bytes):
            return "algorithm_error", "hmac_key_missing", None
        return (
            "valid",
            "content_hmac",
            hmac.new(key, content, hashlib.sha256).hexdigest(),
        )
    return "algorithm_error", "unsupported_algorithm", None


class DiagnosticEngine:
    """Analyze one normalized manifest deterministically."""

    def diagnose(self, manifest: ParsedManifest) -> ManifestDiagnostic:
        context = manifest.context()
        graph = DependencyGraph.from_manifest(manifest)
        cycle_members = graph.cycle_members()
        errors = list(manifest.errors)
        warnings: list[Issue] = []
        components: dict[str, ComponentDiagnostic] = {}

        cycles = [
            {
                "nodes": list(cycle[:-1]),
                "cycle": list(cycle),
                "length": len(cycle) - 1,
            }
            for cycle in graph.cycles
        ]
        for cycle in graph.cycles:
            errors.append(
                Issue(
                    E_CYCLE,
                    "dependency cycle detected",
                    details={"nodes": list(cycle[:-1]), "cycle": list(cycle)},
                )
            )

        incoming_by_target: dict[str, list[dict]] = {
            node: [] for node in graph.nodes
        }
        for source in graph.nodes:
            for dependency in manifest.components[source].dependencies:
                if dependency.target_id not in graph.nodes:
                    continue
                incoming_by_target[dependency.target_id].append(
                    {
                        "source": source,
                        "edge_index": dependency.edge_index,
                        "declared": dependency.declared_constraint,
                        "normalized": dependency.constraint.canonical_text(),
                        "constraint": dependency.constraint,
                    }
                )

        for component_id in graph.nodes:
            component = manifest.components[component_id]
            incoming = sorted(
                incoming_by_target[component_id],
                key=lambda item: (
                    item["source"],
                    item["normalized"],
                    item["edge_index"],
                ),
            )
            intervals = [item["constraint"] for item in incoming]
            intervals.append(component.version_interval)
            intersection = intersect_intervals(intervals, context)

            hash_status, hash_verification, computed_hash = _hash_component(component)
            component_errors: list[Issue] = []
            component_warnings: list[Issue] = []
            if hash_status == "algorithm_error":
                component_errors.append(
                    Issue(
                        E_HASH_ALGO,
                        f"hash verification failed: {hash_verification}",
                        entry_index=component.entry_index,
                        component_id=component_id,
                        field="content_hash",
                    )
                )
            elif hash_status == "valid" and computed_hash != component.content_hash:
                hash_status = "mismatch"
                component_errors.append(
                    Issue(
                        E_HASH_MISMATCH,
                        "canonical content digest does not match content_hash",
                        entry_index=component.entry_index,
                        component_id=component_id,
                        field="content_hash",
                        details={
                            "declared": component.content_hash,
                            "computed": computed_hash,
                        },
                    )
                )
            elif hash_status == "valid":
                hash_status = "valid"

            if intersection.status == "unsatisfiable":
                component_errors.append(
                    Issue(
                        E_CONSTRAINT_UNSAT,
                        "version constraint intersection is unsatisfiable",
                        entry_index=component.entry_index,
                        component_id=component_id,
                        field="version",
                        details={
                            "reason": intersection.reason,
                            "interval": intersection.interval.canonical_text(),
                        },
                    )
                )
            elif intersection.status == "uncertain":
                component_warnings.append(
                    Issue(
                        W_BOUND_UNCERTAIN,
                        "error-bound overlap makes the version decision uncertain",
                        entry_index=component.entry_index,
                        component_id=component_id,
                        field="version",
                        details={
                            "reason": intersection.reason,
                            "interval": intersection.interval.canonical_text(),
                        },
                    )
                )

            components[component_id] = ComponentDiagnostic(
                component=component,
                intersection=intersection,
                incoming_constraints=[
                    {
                        "source": item["source"],
                        "edge_index": item["edge_index"],
                        "declared": item["declared"],
                        "canonical": item["constraint"].canonical_text(),
                    }
                    for item in incoming
                ],
                hash_status=hash_status,
                hash_verification=hash_verification,
                computed_hash=computed_hash,
                hash_rounding_error=HASH_ROUNDING_PER_LINK
                if hash_status != "algorithm_error"
                else None,
                signature_status=component.signature_status,
                signature_missing_source=component.signature_status != "signed",
                local_version_error=intersection.width_error,
                cycle_member=component_id in cycle_members,
                errors=component_errors,
                warnings=component_warnings,
            )
            errors.extend(component_errors)
            warnings.extend(component_warnings)

            if component_id in cycle_members:
                related_cycles = [
                    list(cycle)
                    for cycle in graph.cycles
                    if component_id in cycle[:-1]
                ]
                components[component_id].errors.append(
                    Issue(
                        E_CYCLE,
                        "component participates in a dependency cycle",
                        entry_index=component.entry_index,
                        component_id=component_id,
                        details={"cycles": related_cycles},
                    )
                )
            unresolved = sorted(
                dependency.target_id
                for dependency in component.dependencies
                if dependency.target_id not in manifest.components
            )
            if unresolved:
                components[component_id].errors.append(
                    Issue(
                        E_UNKNOWN_DEPENDENCY,
                        "component has unresolved dependency targets",
                        entry_index=component.entry_index,
                        component_id=component_id,
                        details={"targets": unresolved},
                    )
                )

        local_errors = {
            component_id: diagnostic.local_version_error
            for component_id, diagnostic in components.items()
        }
        best_paths, path_truncated = _best_cumulative_paths(graph, local_errors)
        for component_id, diagnostic in components.items():
            path, cumulative_error = best_paths[component_id]
            diagnostic.path = path
            diagnostic.cumulative_version_error = cumulative_error
            diagnostic.cumulative_hash_error = (
                HASH_ROUNDING_PER_LINK * len(path)
                if diagnostic.hash_rounding_error is not None
                else None
            )
            if graph.cycles:
                diagnostic.path_status = "cycle_guarded_simple_path"
            elif component_id in graph.roots:
                diagnostic.path_status = "root"
            else:
                diagnostic.path_status = "root_to_component"

        missing_sources = sorted(
            component_id
            for component_id, diagnostic in components.items()
            if diagnostic.signature_missing_source
        )
        for source in missing_sources:
            reachable_paths = shortest_paths_from_source(
                source, graph.reverse_adjacency
            )
            for target_id in sorted(reachable_paths):
                path = reachable_paths[target_id]
                components[target_id].signature_missing_by.append(
                    {
                        "source": source,
                        "source_status": components[source].signature_status,
                        "path": list(path),
                    }
                )

        for component_id, diagnostic in components.items():
            diagnostic.signature_missing_by.sort(
                key=lambda item: (item["source"], tuple(item["path"]))
            )
            if len(diagnostic.signature_missing_by) > MAX_STORED_SIGNATURE_PATHS_PER_COMPONENT:
                diagnostic.signature_missing_by = diagnostic.signature_missing_by[
                    :MAX_STORED_SIGNATURE_PATHS_PER_COMPONENT
                ]
            propagated_paths = [
                item
                for item in diagnostic.signature_missing_by
                if item["source"] != component_id
            ]
            if diagnostic.signature_missing_source or propagated_paths:
                if diagnostic.signature_missing_source and propagated_paths:
                    message = (
                        "component signature is missing and signature-missing "
                        "status also propagated from an upstream dependency"
                    )
                elif diagnostic.signature_missing_source:
                    message = "component signature is missing or unknown"
                else:
                    message = (
                        "signature-missing status propagated from an upstream dependency"
                    )
                diagnostic.warnings.append(
                    Issue(
                        W_SIGNATURE_MISSING,
                        message,
                        entry_index=diagnostic.component.entry_index,
                        component_id=component_id,
                        field="signature_status",
                        details={
                            "own_signature_missing": diagnostic.signature_missing_source,
                            "paths": [
                                item["path"] for item in propagated_paths[:16]
                            ],
                        },
                    )
                )
                warnings.extend(diagnostic.warnings[-1:])

            undeniable_failure = (
                diagnostic.hash_status in {"mismatch", "algorithm_error"}
                or diagnostic.version_status == "unsatisfiable"
                or diagnostic.signature_status != "signed"
                or bool(propagated_paths)
                or diagnostic.cycle_member
                or any(
                    dependency.target_id not in components
                    for dependency in diagnostic.component.dependencies
                )
            )
            if undeniable_failure:
                diagnostic.integrity_status = "incomplete"
            elif diagnostic.version_status == "uncertain":
                diagnostic.integrity_status = "indeterminate"
            else:
                diagnostic.integrity_status = "complete"

        observations = {
            component_id: {
                "version_components": list(diagnostic.component.version.components)
                if diagnostic.component.version is not None
                else None,
                "hash_algorithm": diagnostic.component.hash_algorithm,
                "local_version_width": diagnostic.local_version_error,
                "cumulative_version_width": diagnostic.cumulative_version_error,
            }
            for component_id, diagnostic in components.items()
        }
        truth_check = verify_manifest_truth(manifest, observations)
        if truth_check["inconsistent_count"]:
            errors.append(
                Issue(
                    E_TRUTH_MISMATCH,
                    "synchronization-marker truth comparison found inconsistencies",
                    details={
                        "inconsistent_count": truth_check["inconsistent_count"],
                        "mismatch_count": truth_check["mismatch_count"],
                    },
                )
            )

        if manifest.fatal:
            status = "failed"
            conclusion = "failed"
        elif (
            errors
            or manifest.rejections
            or any(
                diagnostic.integrity_status != "complete"
                for diagnostic in components.values()
            )
        ):
            status = "partial"
            conclusion = "attention"
        else:
            status = "success"
            conclusion = "consistent"

        return ManifestDiagnostic(
            manifest=manifest,
            graph=graph,
            context=context,
            components=components,
            cycles=cycles,
            truth_check=truth_check,
            errors=errors,
            warnings=warnings,
            status=status,
            conclusion=conclusion,
            path_enumeration_truncated=path_truncated,
        )

