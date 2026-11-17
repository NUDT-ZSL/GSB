"""Deterministic derivation-chain reconstruction and consistency checks."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from .errors import (
    E_ITER_REGRESSION,
    E_LENGTH_ESCALATION,
    E_PARAM,
    E_SALT_REUSE,
)
from .models import Event, Issue


@dataclass
class DerivationNode:
    event: Event
    node_id: str
    chain_id: str
    parent: "DerivationNode | None" = None
    path: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DerivationResult:
    issues: tuple[Issue, ...]
    chain_count: int
    total_nodes: int
    checked_nodes: int
    completed: bool = True


def _node_id(event: Event) -> str:
    return f"{event.session_id}#{event.seq}"


def build_forest(
    session_id: str,
    derive_events: list[Event],
    *,
    deadline: float | None = None,
) -> tuple[list[DerivationNode], list[Issue]]:
    """Build parent links by level and stage sequence.

    A level-1 event starts a chain. For level N > 1, the parent is the latest
    event at level N-1 whose stage sequence is lower. Ties are resolved by the
    already stable record index. This reconstruction is independent of record
    arrival order.
    """

    by_level: dict[int, list[Event]] = {}
    for event in sorted(derive_events, key=lambda item: (item.seq, item.record_index)):
        by_level.setdefault(event.level, []).append(event)

    nodes_by_event: dict[int, DerivationNode] = {}
    nodes: list[DerivationNode] = []
    issues: list[Issue] = []
    chain_count = 0

    for index, event in enumerate(
        sorted(derive_events, key=lambda item: (item.seq, item.record_index))
    ):
        if deadline is not None and (index & 63) == 0 and time.perf_counter() > deadline:
            raise TimeoutError(f"session {session_id!r} exceeded its time budget")
        if event.level == 1:
            chain_count += 1
            node = DerivationNode(
                event=event,
                node_id=_node_id(event),
                chain_id=_node_id(event),
                parent=None,
                path=(_node_id(event),),
            )
        else:
            candidates = [
                candidate
                for candidate in by_level.get(event.level - 1, ())
                if candidate.seq < event.seq
            ]
            if not candidates:
                issues.append(
                    Issue(
                        code=E_PARAM,
                        message=(
                            f"derive node {_node_id(event)} has no parent at level "
                            f"{event.level - 1}"
                        ),
                        stage=event.stage,
                        seq=event.seq,
                        offset=event.offset,
                        node=_node_id(event),
                        details={"level": event.level},
                    )
                )
                chain_count += 1
                node = DerivationNode(
                    event=event,
                    node_id=_node_id(event),
                    chain_id=_node_id(event),
                    parent=None,
                    path=(_node_id(event),),
                )
            else:
                parent_event = max(
                    candidates, key=lambda candidate: (candidate.seq, candidate.record_index)
                )
                parent = nodes_by_event[parent_event.record_index]
                node = DerivationNode(
                    event=event,
                    node_id=_node_id(event),
                    chain_id=parent.chain_id,
                    parent=parent,
                    path=parent.path + (_node_id(event),),
                )
        nodes_by_event[event.record_index] = node
        nodes.append(node)

    return nodes, issues


def evaluate_chains(
    session_id: str,
    derive_events: list[Event],
    selected_keys: frozenset[tuple[str, int, int]],
    *,
    deadline: float | None = None,
) -> DerivationResult:
    nodes, build_issues = build_forest(session_id, derive_events, deadline=deadline)
    selected_nodes = [
        node
        for node in nodes
        if (node.event.session_id, node.event.seq, node.event.record_index) in selected_keys
    ]
    issues: list[Issue] = list(build_issues)

    for index, node in enumerate(selected_nodes):
        if deadline is not None and (index & 63) == 0 and time.perf_counter() > deadline:
            raise TimeoutError(f"session {session_id!r} exceeded its time budget")
        parent = node.parent
        if parent is None:
            continue
        child_event = node.event
        parent_event = parent.event
        if child_event.output_length > parent_event.output_length:
            issues.append(
                Issue(
                    code=E_LENGTH_ESCALATION,
                    message=(
                        f"child output length {child_event.output_length} exceeds parent "
                        f"output length {parent_event.output_length}"
                    ),
                    stage=child_event.stage,
                    seq=child_event.seq,
                    offset=child_event.offset,
                    node=node.node_id,
                    parent_node=parent.node_id,
                    chain_id=node.chain_id,
                    path=(parent.node_id, node.node_id),
                    details={
                        "parent_output_length": parent_event.output_length,
                        "child_output_length": child_event.output_length,
                    },
                )
            )
        if child_event.iterations < parent_event.iterations:
            issues.append(
                Issue(
                    code=E_ITER_REGRESSION,
                    message=(
                        f"child iterations {child_event.iterations} are lower than parent "
                        f"iterations {parent_event.iterations}"
                    ),
                    stage=child_event.stage,
                    seq=child_event.seq,
                    offset=child_event.offset,
                    node=node.node_id,
                    parent_node=parent.node_id,
                    chain_id=node.chain_id,
                    path=(parent.node_id, node.node_id),
                    details={
                        "parent_iterations": parent_event.iterations,
                        "child_iterations": child_event.iterations,
                    },
                )
            )

    selected_by_chain: dict[str, list[DerivationNode]] = {}
    for node in selected_nodes:
        selected_by_chain.setdefault(node.chain_id, []).append(node)

    for chain_id in sorted(selected_by_chain):
        first_by_salt: dict[bytes, DerivationNode] = {}
        for index, node in enumerate(sorted(
            selected_by_chain[chain_id],
            key=lambda item: (item.event.seq, item.event.record_index),
        )):
            if deadline is not None and (index & 63) == 0 and time.perf_counter() > deadline:
                raise TimeoutError(f"session {session_id!r} exceeded its time budget")
            salt_digest = hashlib.sha256(node.event.salt).digest()
            first = first_by_salt.get(salt_digest)
            if first is None:
                first_by_salt[salt_digest] = node
                continue
            issues.append(
                Issue(
                    code=E_SALT_REUSE,
                    message=f"salt is reused in derivation chain {chain_id}",
                    stage=node.event.stage,
                    seq=node.event.seq,
                    offset=node.event.offset,
                    node=node.node_id,
                    parent_node=first.node_id,
                    chain_id=chain_id,
                    path=(first.node_id, node.node_id),
                    details={
                        "first_node_path": list(first.path),
                        "duplicate_node_path": list(node.path),
                        "salt_sha256": salt_digest.hex(),
                    },
                )
            )

    return DerivationResult(
        issues=tuple(issues),
        chain_count=len({node.chain_id for node in nodes}),
        total_nodes=len(nodes),
        checked_nodes=len(selected_nodes),
        completed=True,
    )

