"""Field-level lineage reconstruction and sensitivity propagation.

The engine consumes parsed snapshots in version order and builds:

* a node per ``(version, canonical field key)``;
* continuity edges between the same canonical key across adjacent versions;
* derivation edges from the (possibly cross-version) declaration references;
* the transitive closure of those edges;
* propagated sensitivity levels and mask-bypass / dangling-derive diagnostics.

Diagnostics are split into two buckets:

* ``parse_diagnostics`` - structural rejections produced while parsing, stored
  in the checkpoint and never recomputed;
* ``rule_diagnostics`` - ``E_MASK_BYPASS`` / ``E_DANGLING_DERIVE`` / endpoint
  ``E_FIELD`` records derived from the graph and recomputed idempotently on
  every :meth:`rebuild`.

All collections are keyed/sorted by canonical addressing keys so results are
independent of input ordering and dict traversal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .errors import Diagnostic, E_DANGLING_DERIVE, E_FIELD, E_MASK_BYPASS, sort_key
from .models import LEVEL_ORDER, PLAIN, SENSITIVE, Snapshot, version_sort_key


def node_id(version: str, key: str) -> str:
    return f"{version}::{key}"


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    kind: str  # "derive" or "continuity"
    transitive: bool
    decl_version: str
    entry_seq: int
    source_key: str
    target_key: str


@dataclass
class Node:
    version: str
    key: str
    declared: str
    effective: str = PLAIN
    carried: str = PLAIN

    @property
    def id(self) -> str:
        return node_id(self.version, self.key)


@dataclass
class VersionStat:
    version: str
    field_count: int
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)


class LineageEngine:
    """Incrementally rebuilds lineage over an ordered chain of snapshots."""

    def __init__(self) -> None:
        self.nodes: Dict[str, Node] = {}
        self.edges: List[Edge] = []
        self._edge_index: Set[Tuple[str, str, str]] = set()
        self.parse_diagnostics: List[Diagnostic] = []
        self.rule_diagnostics: List[Diagnostic] = []
        self.version_stats: List[VersionStat] = []
        self.snapshots: List[Snapshot] = []
        self._latest_fields: Dict[str, str] = {}
        self._all_keys_ever: Set[str] = set()
        self._removed_keys: Set[str] = set()

    @property
    def diagnostics(self) -> List[Diagnostic]:
        return sorted(self.parse_diagnostics + self.rule_diagnostics, key=sort_key)

    # ------------------------------------------------------------------ ingest
    def ingest_all(self, snapshots: List[Snapshot],
                   parse_diagnostics: Optional[List[Diagnostic]] = None) -> None:
        if parse_diagnostics:
            self.parse_diagnostics.extend(parse_diagnostics)
        for snapshot in sorted(snapshots, key=lambda snap: version_sort_key(snap.version)):
            self.ingest(snapshot)
        self.rebuild()

    def ingest(self, snapshot: Snapshot) -> None:
        self.snapshots.append(snapshot)
        current_keys = {item.key for item in snapshot.fields}

        for item in sorted(snapshot.fields, key=lambda f: f.key):
            self.nodes[node_id(snapshot.version, item.key)] = Node(
                version=snapshot.version, key=item.key, declared=item.sensitivity
            )

        self._all_keys_ever.update(current_keys)
        removed_now = set(self._latest_fields) - current_keys
        self._removed_keys.update(removed_now)
        self._latest_fields = {
            key: (snapshot.version if key in current_keys else owner)
            for key, owner in self._latest_fields.items()
        }
        for key in current_keys:
            self._latest_fields[key] = snapshot.version
            self._removed_keys.discard(key)

        self._record_continuity(snapshot, current_keys)
        self._record_derivations(snapshot)
        self._record_version_stat(snapshot, current_keys)

    def rebuild(self) -> None:
        """Recompute propagation and rule diagnostics over the whole graph.

        Idempotent: repeated calls (including after a checkpoint restore)
        produce the exact same diagnostic set rather than duplicating records.
        """

        self.rule_diagnostics = []
        self._recompute_levels()
        for snapshot in sorted(self.snapshots, key=lambda s: version_sort_key(s.version)):
            self._resolve_dangling(snapshot)
            self._check_mask_bypass(snapshot)
        self.rule_diagnostics.sort(key=sort_key)

    # ------------------------------------------------------------- edge building
    def _add_edge(self, edge: Edge) -> None:
        signature = (edge.source, edge.target, edge.kind)
        if signature in self._edge_index:
            return
        self._edge_index.add(signature)
        self.edges.append(edge)

    def _record_continuity(self, snapshot: Snapshot, current_keys: Set[str]) -> None:
        if not self.version_stats:
            return
        previous_version = self.version_stats[-1].version
        for key in sorted(current_keys):
            previous = node_id(previous_version, key)
            if previous in self.nodes:
                # Continuity preserves full transitive sensitivity flow.
                self._add_edge(
                    Edge(
                        source=previous,
                        target=node_id(snapshot.version, key),
                        kind="continuity",
                        transitive=True,
                        decl_version="continuity",
                        entry_seq=0,
                        source_key=key,
                        target_key=key,
                    )
                )

    def _record_derivations(self, snapshot: Snapshot) -> None:
        # Sorting by canonical endpoints makes edge construction order
        # independent of the raw declaration arrangement.
        ordered = sorted(
            snapshot.derivations,
            key=lambda d: (d.source_key, d.target_key, d.decl_version),
        )
        for canonical_seq, derivation in enumerate(ordered, start=1):
            source_node = self._endpoint(snapshot, derivation.source_key)
            target_node = self._endpoint(snapshot, derivation.target_key)
            if source_node is None or target_node is None:
                continue
            self._add_edge(
                Edge(
                    source=source_node,
                    target=target_node,
                    kind="derive",
                    transitive=derivation.transitive,
                    decl_version=derivation.decl_version,
                    # Stable ordinal: deterministic even if inputs are shuffled.
                    entry_seq=canonical_seq,
                    source_key=derivation.source_key,
                    target_key=derivation.target_key,
                )
            )

    def _endpoint(self, snapshot: Snapshot, key: str) -> Optional[str]:
        local = node_id(snapshot.version, key)
        if local in self.nodes:
            return local
        owner = self._latest_fields.get(key)
        if owner is not None and key not in self._removed_keys:
            candidate = node_id(owner, key)
            if candidate in self.nodes:
                return candidate
        return None

    def _resolve_dangling(self, snapshot: Snapshot) -> None:
        """Classify derivation endpoints that could not be bound to a node."""

        present = {node_id(snapshot.version, item.key) for item in snapshot.fields}
        bound = {(edge.source, edge.target) for edge in self.edges}
        for derivation in sorted(
            snapshot.derivations,
            key=lambda d: (d.source_key, d.target_key, d.decl_version),
        ):
            source_local = node_id(snapshot.version, derivation.source_key)
            target_local = node_id(snapshot.version, derivation.target_key)
            for endpoint_key, local in (
                (derivation.source_key, source_local),
                (derivation.target_key, target_local),
            ):
                if local in self.nodes or local in present:
                    continue
                if any(local == target and source in self.nodes
                       for source, target in bound):
                    continue
                code = E_DANGLING_DERIVE if endpoint_key in self._all_keys_ever else E_FIELD
                message = (
                    f"derivation references removed field {endpoint_key!r}"
                    if code == E_DANGLING_DERIVE
                    else f"derivation endpoint {endpoint_key!r} does not exist"
                )
                self.rule_diagnostics.append(
                    Diagnostic(
                        code,
                        snapshot.version,
                        derivation.entry_seq,
                        message,
                        field=endpoint_key,
                    )
                )

    def _record_version_stat(self, snapshot: Snapshot, current_keys: Set[str]) -> None:
        previous_keys: Set[str] = set()
        if self.version_stats:
            previous_version = self.version_stats[-1].version
            previous_keys = {
                node.key for node in self.nodes.values()
                if node.version == previous_version
            }
        self.version_stats.append(
            VersionStat(
                version=snapshot.version,
                field_count=len(current_keys),
                added=sorted(current_keys - previous_keys),
                removed=sorted(previous_keys - current_keys),
            )
        )

    # ------------------------------------------------------------- propagation
    def _recompute_levels(self) -> None:
        for node in self.nodes.values():
            node.effective = node.declared
            node.carried = node.declared

        incoming: Dict[str, List[Edge]] = {identifier: [] for identifier in self.nodes}
        for edge in self.edges:
            if edge.target in incoming:
                incoming[edge.target].append(edge)

        order = sorted(
            self.nodes,
            key=lambda identifier: (
                version_sort_key(self.nodes[identifier].version),
                self.nodes[identifier].key,
            ),
        )
        # Fixed-point propagation. Every edge only forwards the upstream
        # *carried* level: a non-transitive edge exposes that level to the
        # target's effective value but cannot pass an inherited level onward,
        # which is exactly the v1 direct-derivation semantics.
        for _ in range(len(order) + 1):
            changed = False
            for identifier in order:
                node = self.nodes[identifier]
                best_effective = node.declared
                best_carried = node.declared
                for edge in incoming[identifier]:
                    upstream = self.nodes.get(edge.source)
                    if upstream is None:
                        continue
                    best_effective = _max_level(best_effective, upstream.carried)
                    if edge.transitive:
                        best_carried = _max_level(best_carried, upstream.carried)
                if (best_effective, best_carried) != (node.effective, node.carried):
                    node.effective = best_effective
                    node.carried = best_carried
                    changed = True
            if not changed:
                break

    def _check_mask_bypass(self, snapshot: Snapshot) -> None:
        present = {node_id(snapshot.version, item.key) for item in snapshot.fields}
        for edge in self.edges:
            if edge.kind != "derive" or edge.target not in present:
                continue
            source_node = self.nodes.get(edge.source)
            target_node = self.nodes.get(edge.target)
            if source_node is None or target_node is None:
                continue
            # Bypass: a directly sensitive source flows into a plaintext-marked
            # direct downstream field, defeating the masking convention.
            if source_node.declared == SENSITIVE and target_node.declared == PLAIN:
                self.rule_diagnostics.append(
                    Diagnostic(
                        E_MASK_BYPASS,
                        snapshot.version,
                        edge.entry_seq,
                        (
                            f"sensitive source {edge.source_key!r} feeds plaintext "
                            f"downstream {edge.target_key!r}"
                        ),
                        field=edge.target_key,
                    )
                )

    # ------------------------------------------------------------ transitive set
    def transitive_closure(self) -> Dict[str, List[str]]:
        adjacency: Dict[str, List[str]] = {identifier: [] for identifier in self.nodes}
        for edge in self.edges:
            adjacency.setdefault(edge.source, []).append(edge.target)
        closure: Dict[str, List[str]] = {}
        for start in sorted(adjacency):
            reachable: Set[str] = set()
            stack = list(adjacency[start])
            while stack:
                current = stack.pop()
                if current in reachable:
                    continue
                reachable.add(current)
                stack.extend(adjacency.get(current, []))
            closure[start] = sorted(reachable)
        return closure

    # ------------------------------------------------------------- serialization
    def to_dict(self) -> Dict[str, Any]:
        nodes = [
            {
                "id": node.id,
                "version": node.version,
                "field": node.key,
                "declared": node.declared,
                "effective": node.effective,
                "carried": node.carried,
            }
            for node in sorted(
                self.nodes.values(),
                key=lambda n: (version_sort_key(n.version), n.key),
            )
        ]
        edges = [
            {
                "source": edge.source,
                "target": edge.target,
                "source_field": edge.source_key,
                "target_field": edge.target_key,
                "kind": edge.kind,
                "transitive": edge.transitive,
                "decl_version": edge.decl_version,
                "entry_seq": edge.entry_seq,
            }
            for edge in sorted(
                self.edges,
                key=lambda e: (e.source, e.target, e.kind, e.decl_version),
            )
        ]
        return {
            "versions": [
                {
                    "version": stat.version,
                    "field_count": stat.field_count,
                    "added": stat.added,
                    "removed": stat.removed,
                }
                for stat in sorted(self.version_stats, key=lambda s: version_sort_key(s.version))
            ],
            "nodes": nodes,
            "edges": edges,
            "transitive_closure": self.transitive_closure(),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


def _max_level(left: str, right: str) -> str:
    return left if LEVEL_ORDER[left] >= LEVEL_ORDER[right] else right
