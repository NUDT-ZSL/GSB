"""Repair task planning, cost estimation, and error-aware source ordering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .consensus import ConsensusResult, TruthDecision, compute_group_state_digest
from .fixed import CostSelection, Quantized, estimate_transfer, estimate_workload, estimate_source_cost, rank_candidates
from .integrity import ReplicaBlockState, ShardDigest
from .spec import ReplicaGroupSpec
from .util import sha256_hex


@dataclass(frozen=True)
class RepairTask:
    task_id: str
    block_id: str
    source_node_id: str
    target_node_id: str
    truth_digest: str
    transfer: Quantized
    workload: Quantized
    selection: CostSelection
    repair_amount_fixed: int
    repair_amount_error_bound_units: int
    cost_fixed: int
    cost_error_bound_units: int

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "block_id": self.block_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "truth_digest": self.truth_digest,
            "repair_amount_fixed": self.repair_amount_fixed,
            "repair_amount_error_bound_units": self.repair_amount_error_bound_units,
            "transfer": self.transfer.to_dict(),
            "workload": self.workload.to_dict(),
            "cost_fixed": self.cost_fixed,
            "cost_error_bound_units": self.cost_error_bound_units,
            "source_selection": self.selection.to_dict(),
            "tie_channel": self.selection.tie_channel,
            "tie_reason": self.selection.tie_reason,
        }


@dataclass(frozen=True)
class RepairPlan:
    scale: int
    tasks: tuple[RepairTask, ...]
    after_replicas: tuple[ReplicaBlockState, ...]
    before_state_digest: str
    after_state_digest: str
    summary: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "scale": self.scale,
            "before_state_digest": self.before_state_digest,
            "after_state_digest": self.after_state_digest,
            "tasks": [task.to_dict() for task in self.tasks],
            "summary": self.summary,
        }


def _task_id(block_id: str, target_node_id: str) -> str:
    digest = sha256_hex(f"{block_id}\x00{target_node_id}".encode("utf-8"))
    return "task-" + digest[:20]


def _find_state(
    states: Iterable[ReplicaBlockState], block_id: str, node_id: str
) -> ReplicaBlockState:
    for state in states:
        if state.block_id == block_id and state.node_id == node_id:
            return state
    raise KeyError((block_id, node_id))


def build_repair_plan(spec: ReplicaGroupSpec, consensus: ConsensusResult) -> RepairPlan:
    """Create deterministic tasks for every mismatched replica."""

    node_map = spec.node_map
    max_priority = max(node.priority for node in spec.nodes)
    max_load = max(1, max(node.repair_load for node in spec.nodes))
    policy = spec.repair_policy
    cost_policy = policy.cost
    tasks: list[RepairTask] = []
    replacements: dict[tuple[str, str], ReplicaBlockState] = {}

    for block_id in sorted(consensus.decisions):
        decision = consensus.decisions[block_id]
        truth_states = {
            state.node_id: state
            for state in consensus.replicas
            if state.block_id == block_id and state.digest == decision.truth_digest
        }
        truth_source_ids = sorted(truth_states)
        if not truth_source_ids:
            raise RuntimeError(f"no truth source for block {block_id}")
        manifest = spec.manifest(truth_source_ids[0], block_id)
        for target_node_id in decision.mismatch_nodes:
            candidates = []
            for source_node_id in truth_source_ids:
                source_node = node_map[source_node_id]
                candidates.append(
                    estimate_source_cost(
                        node_id=source_node_id,
                        priority=source_node.priority,
                        repair_load=source_node.repair_load,
                        max_priority=max_priority,
                        max_load=max_load,
                        byte_size=manifest.byte_size,
                        scale=consensus.scale,
                        transfer_weight=cost_policy.transfer_weight,
                        work_weight=cost_policy.work_weight,
                        priority_weight=cost_policy.priority_weight,
                        load_weight=cost_policy.load_weight,
                        overhead_numerator=cost_policy.transfer_overhead_numerator,
                        overhead_denominator=cost_policy.transfer_overhead_denominator,
                        work_numerator=cost_policy.work_numerator,
                        work_denominator=cost_policy.work_denominator,
                    )
                )
            selection = rank_candidates(candidates)
            source_node_id = selection.winner.node_id
            source_state = truth_states[source_node_id]
            transfer = estimate_transfer(
                manifest.byte_size,
                cost_policy.transfer_overhead_numerator,
                cost_policy.transfer_overhead_denominator,
                consensus.scale,
            )
            workload = estimate_workload(
                manifest.byte_size,
                cost_policy.work_numerator,
                cost_policy.work_denominator,
                consensus.scale,
            )
            task = RepairTask(
                task_id=_task_id(block_id, target_node_id),
                block_id=block_id,
                source_node_id=source_node_id,
                target_node_id=target_node_id,
                truth_digest=decision.truth_digest,
                transfer=transfer,
                workload=workload,
                selection=selection,
                repair_amount_fixed=transfer.value,
                repair_amount_error_bound_units=transfer.error_bound_units,
                cost_fixed=selection.winner.value_fixed,
                cost_error_bound_units=selection.winner.error_bound_units,
            )
            tasks.append(task)
            replacements[(block_id, target_node_id)] = ReplicaBlockState(
                node_id=target_node_id,
                block_id=block_id,
                byte_size=source_state.byte_size,
                digest=source_state.digest,
                shards=tuple(
                    ShardDigest(
                        index=shard.index,
                        byte_size=shard.byte_size,
                        digest=shard.digest,
                    )
                    for shard in source_state.shards
                ),
                observed_via="planned",
            )

    after_replicas = tuple(
        sorted(
            (
                replacements.get((state.block_id, state.node_id), state)
                for state in consensus.replicas
            ),
            key=lambda item: (item.block_id, item.node_id),
        )
    )
    after_state_digest = compute_group_state_digest(spec, after_replicas)
    affected_nodes = tuple(sorted({task.target_node_id for task in tasks}))
    affected_blocks = tuple(sorted({task.block_id for task in tasks}))
    summary = {
        "repair_task_count": len(tasks),
        "affected_nodes": list(affected_nodes),
        "affected_blocks": list(affected_blocks),
        "tie_task_count": sum(1 for task in tasks if task.selection.tie_channel),
        "total_repair_amount_fixed": sum(task.repair_amount_fixed for task in tasks),
        "total_repair_amount_error_bound_units": sum(
            task.repair_amount_error_bound_units for task in tasks
        ),
        "total_workload_fixed": sum(task.workload.value for task in tasks),
        "total_workload_error_bound_units": sum(
            task.workload.error_bound_units for task in tasks
        ),
        "total_cost_fixed": sum(task.cost_fixed for task in tasks),
        "total_cost_error_bound_units": sum(
            task.cost_error_bound_units for task in tasks
        ),
        "scale": consensus.scale,
        "derived_error_bound_note": (
            "每个 floor 量化项误差严格小于 1 个定点单位；加权后按权重累加，"
            "任务与汇总上界为保守的整数和"
        ),
    }
    return RepairPlan(
        scale=consensus.scale,
        tasks=tuple(tasks),
        after_replicas=after_replicas,
        before_state_digest=consensus.state_digest,
        after_state_digest=after_state_digest,
        summary=summary,
    )


def decision_summary(decision: TruthDecision) -> dict[str, object]:
    return {
        "block_id": decision.block_id,
        "truth_digest": decision.truth_digest,
        "truth_nodes": list(decision.truth_nodes),
        "representative_source": decision.representative_source,
        "mismatch_nodes": list(decision.mismatch_nodes),
        "replica_count": decision.replica_count,
        "strict_majority": decision.strict_majority,
        "alert_certainty": decision.ratio.certainty,
        "ratio_fixed": decision.ratio.ratio.value,
        "threshold_fixed": decision.ratio.threshold.value,
        "scale": decision.ratio.scale,
        "ratio_error_bound_units": decision.ratio.gap_error_bound_units,
    }
