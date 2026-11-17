"""Content-digest majority consensus with deterministic tie-breaking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .fixed import RatioAlert, classify_ratio, quantize_fraction
from .integrity import ReplicaBlockState
from .spec import ReplicaGroupSpec
from .util import digest_json


@dataclass(frozen=True)
class DigestGroup:
    digest: str
    node_ids: tuple[str, ...]
    node_count: int
    priority_sum: int

    def to_dict(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "node_ids": list(self.node_ids),
            "node_count": self.node_count,
            "priority_sum": self.priority_sum,
        }


@dataclass(frozen=True)
class TruthDecision:
    block_id: str
    replica_count: int
    digest_groups: tuple[DigestGroup, ...]
    truth_digest: str
    truth_nodes: tuple[str, ...]
    representative_source: str
    mismatch_nodes: tuple[str, ...]
    mismatch_count: int
    strict_majority: bool
    ratio: RatioAlert
    tie_break_applied: bool
    tie_break_stage: str
    tie_break_rule: str

    def to_dict(self) -> dict[str, object]:
        return {
            "block_id": self.block_id,
            "replica_count": self.replica_count,
            "digest_groups": [group.to_dict() for group in self.digest_groups],
            "truth_digest": self.truth_digest,
            "truth_nodes": list(self.truth_nodes),
            "representative_source": self.representative_source,
            "mismatch_nodes": list(self.mismatch_nodes),
            "mismatch_count": self.mismatch_count,
            "strict_majority": self.strict_majority,
            "tie_break_applied": self.tie_break_applied,
            "tie_break_stage": self.tie_break_stage,
            "tie_break_rule": self.tie_break_rule,
            "mismatch_ratio": self.ratio.to_dict(),
        }


@dataclass(frozen=True)
class ConsensusResult:
    scale: int
    decisions: Mapping[str, TruthDecision]
    replicas: tuple[ReplicaBlockState, ...]
    state_digest: str
    spec_digest: str
    aggregate_mismatch_ratio: RatioAlert

    def to_dict(self) -> dict[str, object]:
        return {
            "scale": self.scale,
            "spec_digest": self.spec_digest,
            "group_state_digest": self.state_digest,
            "aggregate_mismatch_ratio": self.aggregate_mismatch_ratio.to_dict(),
            "truth": [self.decisions[key].to_dict() for key in sorted(self.decisions)],
            "replicas": [
                state.to_dict()
                for state in sorted(
                    self.replicas, key=lambda item: (item.block_id, item.node_id)
                )
            ],
        }


def group_states(
    spec: ReplicaGroupSpec, states: Iterable[ReplicaBlockState]
) -> dict[str, tuple[ReplicaBlockState, ...]]:
    by_block: dict[str, list[ReplicaBlockState]] = {}
    for state in states:
        by_block.setdefault(state.block_id, []).append(state)
    return {
        block_id: tuple(
            sorted(by_block[block_id], key=lambda item: item.node_id)
        )
        for block_id in sorted(by_block)
    }


def determine_truth(
    spec: ReplicaGroupSpec, states: Iterable[ReplicaBlockState], scale: int
) -> ConsensusResult:
    """Resolve one truth digest per block using exact digest multiplicity.

    Selection order is:
    1. highest digest frequency;
    2. highest total priority weight among nodes holding that digest;
    3. lexicographically smallest sorted node-id tuple.

    The representative source among equal truth digests is selected by higher
    node priority and then node id.  Input order is irrelevant.
    """

    state_list = tuple(states)
    by_block = group_states(spec, state_list)
    node_map = spec.node_map
    decisions: dict[str, TruthDecision] = {}
    total_mismatches = 0
    total_replicas = 0
    for block_id in sorted(by_block):
        replicas = by_block[block_id]
        grouped: dict[str, list[str]] = {}
        for state in replicas:
            grouped.setdefault(state.digest, []).append(state.node_id)
        groups: list[DigestGroup] = []
        for digest in grouped:
            node_ids = tuple(sorted(grouped[digest]))
            groups.append(
                DigestGroup(
                    digest=digest,
                    node_ids=node_ids,
                    node_count=len(node_ids),
                    priority_sum=sum(node_map[node_id].priority for node_id in node_ids),
                )
            )
        groups.sort(
            key=lambda group: (
                -group.node_count,
                -group.priority_sum,
                group.node_ids,
            )
        )
        if not groups:
            continue
        winner = groups[0]
        frequency_tie = len(groups) > 1 and groups[1].node_count == winner.node_count
        priority_tie = (
            frequency_tie and groups[1].priority_sum == winner.priority_sum
        )
        tie_break_applied = frequency_tie
        if not frequency_tie:
            tie_break_stage = "none"
        elif not priority_tie:
            tie_break_stage = "priority"
        else:
            tie_break_stage = "node_id"
        truth_nodes = tuple(
            sorted(
                winner.node_ids,
                key=lambda node_id: (-node_map[node_id].priority, node_id),
            )
        )
        representative = truth_nodes[0]
        mismatch_nodes = tuple(
            sorted(
                state.node_id for state in replicas if state.digest != winner.digest
            )
        )
        total_mismatches += len(mismatch_nodes)
        total_replicas += len(replicas)
        ratio = classify_ratio(
            len(mismatch_nodes),
            len(replicas),
            spec.repair_policy.alert_ratio_numerator,
            spec.repair_policy.alert_ratio_denominator,
            scale,
        )
        decisions[block_id] = TruthDecision(
            block_id=block_id,
            replica_count=len(replicas),
            digest_groups=tuple(groups),
            truth_digest=winner.digest,
            truth_nodes=truth_nodes,
            representative_source=representative,
            mismatch_nodes=mismatch_nodes,
            mismatch_count=len(mismatch_nodes),
            strict_majority=winner.node_count * 2 > len(replicas),
            ratio=ratio,
            tie_break_applied=tie_break_applied,
            tie_break_stage=tie_break_stage,
            tie_break_rule=(
                "frequency_desc,total_priority_desc,node_id_tuple_asc"
            ),
        )
    aggregate_ratio = classify_ratio(
        total_mismatches,
        max(1, total_replicas),
        spec.repair_policy.alert_ratio_numerator,
        spec.repair_policy.alert_ratio_denominator,
        scale,
    )
    state_digest = compute_group_state_digest(spec, state_list)
    return ConsensusResult(
        scale=scale,
        decisions=decisions,
        replicas=tuple(
            sorted(state_list, key=lambda item: (item.block_id, item.node_id))
        ),
        state_digest=state_digest,
        spec_digest=spec.spec_digest,
        aggregate_mismatch_ratio=aggregate_ratio,
    )


def compute_group_state_digest(
    spec: ReplicaGroupSpec, states: Iterable[ReplicaBlockState]
) -> str:
    """Digest the logical spec and current replica contents, not their order."""

    state_list = sorted(
        states, key=lambda item: (item.block_id, item.node_id)
    )
    document = {
        "spec_digest": spec.spec_digest,
        "replicas": [
            {
                "block_id": state.block_id,
                "node_id": state.node_id,
                "byte_size": state.byte_size,
                "digest": state.digest,
            }
            for state in state_list
        ],
    }
    return digest_json(document)


def distribution_by_node(
    states: Iterable[ReplicaBlockState],
) -> list[dict[str, object]]:
    by_node: dict[str, list[ReplicaBlockState]] = {}
    for state in states:
        by_node.setdefault(state.node_id, []).append(state)
    result: list[dict[str, object]] = []
    for node_id in sorted(by_node):
        block_rows = []
        for state in sorted(by_node[node_id], key=lambda item: item.block_id):
            block_rows.append(
                {
                    "block_id": state.block_id,
                    "byte_size": state.byte_size,
                    "digest": state.digest,
                }
            )
        result.append(
            {
                "node_id": node_id,
                "block_count": len(block_rows),
                "total_bytes": sum(row["byte_size"] for row in block_rows),
                "blocks": block_rows,
            }
        )
    return result
