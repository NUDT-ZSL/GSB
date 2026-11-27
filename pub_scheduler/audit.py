"""Independent replay checks over workloads and scheduler journals."""

from __future__ import annotations

from typing import Any, Mapping

from .model import (
    DIMENSIONS,
    validate_workload,
    vector_add,
    vector_leq,
    vector_nonnegative,
    vector_sub,
    vector_sum,
    workload_digest,
    zero_vector,
)


def verify_result(
    workload: Mapping[str, Any], result: Mapping[str, Any]
) -> dict[str, Any]:
    """Replay capacity changes and snapshots without trusting engine internals."""

    errors: list[str] = []
    validated = validate_workload(workload)
    if result.get("workload_hash") != workload_digest(validated):
        errors.append("result.workload_hash 与作业工程不一致")

    nodes = {node["node_id"]: node for node in validated["nodes"]}
    decisions = result.get("decisions", [])
    changes = result.get("capacity_changes", [])
    snapshots = result.get("samples", [])
    if not isinstance(decisions, list):
        errors.append("decisions 不是数组")
        decisions = []
    if not isinstance(changes, list):
        errors.append("capacity_changes 不是数组")
        changes = []
    if not isinstance(snapshots, list):
        errors.append("samples 不是数组")
        snapshots = []

    decision_ids: set[str] = set()
    previous_time = -1
    previous_sequence = 0
    for decision in decisions:
        decision_id = decision.get("decision_id")
        if not isinstance(decision_id, str) or not decision_id:
            errors.append("决策缺少 decision_id")
            continue
        if decision_id in decision_ids:
            errors.append(f"决策标识重复: {decision_id}")
        decision_ids.add(decision_id)
        at = decision.get("at")
        sequence = decision.get("sequence")
        if not isinstance(at, int) or not isinstance(sequence, int):
            errors.append(f"{decision_id} 的 at/sequence 不是整数")
            continue
        if at < previous_time:
            errors.append(f"{decision_id} 未按逻辑时刻排序")
        if sequence <= previous_sequence:
            errors.append(f"{decision_id} 未按决策序号严格排序")
        previous_time = at
        previous_sequence = sequence
        if decision.get("type") not in ("ADMIT", "PREEMPT", "DOWNGRADE", "REJECT"):
            errors.append(f"{decision_id} 含未知决策类型")

    replay_used = {node_id: zero_vector() for node_id in nodes}
    replay_changes: list[dict[str, Any]] = []
    previous_time = -1
    previous_sequence = 0
    for change in changes:
        change_id = change.get("change_id")
        node_id = change.get("node_id")
        if node_id not in nodes:
            errors.append(f"{change_id} 引用未知节点 {node_id}")
            continue
        at = change.get("at")
        sequence = change.get("sequence")
        if not isinstance(at, int) or not isinstance(sequence, int):
            errors.append(f"{change_id} 的 at/sequence 不是整数")
            continue
        if at < previous_time:
            errors.append(f"{change_id} 未按逻辑时刻排序")
        if sequence <= previous_sequence:
            errors.append(f"{change_id} 未按变更序号严格排序")
        previous_time = at
        previous_sequence = sequence

        before = change.get("before", {})
        after = change.get("after", {})
        delta = change.get("delta", {})
        if not isinstance(delta, dict) or set(delta) != set(DIMENSIONS):
            errors.append(f"{change_id} delta 不是三维整数向量")
            continue
        before_used = replay_used[node_id]
        capacity = nodes[node_id]["capacity"]
        expected_before_free = vector_sub(capacity, before_used)
        if before.get("used") != before_used:
            errors.append(f"{change_id} before.used 与重放账本不一致")
        if before.get("free") != expected_before_free:
            errors.append(f"{change_id} before.free 不守恒")
        new_used = vector_add(before_used, delta)
        new_free = vector_sub(capacity, new_used)
        if not vector_nonnegative(new_used) or not vector_nonnegative(new_free):
            errors.append(f"{change_id} 导致容量越界")
        if after.get("used") != new_used or after.get("free") != new_free:
            errors.append(f"{change_id} after 账本不守恒")
        replay_used[node_id] = new_used
        replay_changes.append(change)
        cause = change.get("cause", {})
        if cause.get("kind") == "decision":
            if cause.get("id") not in decision_ids:
                errors.append(f"{change_id} 关联的决策不存在")
        elif cause.get("kind") == "event":
            if not cause.get("id"):
                errors.append(f"{change_id} 事件原因缺少标识")
        else:
            errors.append(f"{change_id} 缺少可追溯原因")

    previous_snapshot_time = -1
    active_assignment_count = 0
    for snapshot in snapshots:
        at = snapshot.get("at")
        if not isinstance(at, int):
            errors.append("快照 at 不是整数")
            continue
        if at < previous_snapshot_time:
            errors.append("快照未按逻辑时刻排序")
        previous_snapshot_time = at
        expected_used = {
            node_id: zero_vector()
            for node_id in nodes
        }
        for change in replay_changes:
            if change["at"] > at:
                break
            node_id = change["node_id"]
            expected_used[node_id] = vector_add(
                expected_used[node_id], change["delta"]
            )

        seen_nodes: set[str] = set()
        for snapshot_node in snapshot.get("nodes", []):
            node_id = snapshot_node.get("node_id")
            if node_id not in nodes:
                errors.append(f"快照引用未知节点 {node_id}")
                continue
            if node_id in seen_nodes:
                errors.append(f"快照重复节点 {node_id}")
            seen_nodes.add(node_id)
            capacity = nodes[node_id]["capacity"]
            used = expected_used[node_id]
            free = vector_sub(capacity, used)
            if snapshot_node.get("used") != used:
                errors.append(f"快照 {at}/{node_id} used 与重放不一致")
            if snapshot_node.get("free") != free:
                errors.append(f"快照 {at}/{node_id} free 不守恒")
            if snapshot_node.get("capacity") != capacity:
                errors.append(f"快照 {at}/{node_id} capacity 不一致")
            assignments = snapshot_node.get("active_assignments", [])
            allocated_sum = zero_vector()
            mutex_groups: set[str] = set()
            for assignment in assignments:
                allocation = assignment.get("allocation", {})
                if set(allocation) != set(DIMENSIONS):
                    errors.append(f"快照 {at}/{node_id} 活跃分配不是三维向量")
                    continue
                allocated_sum = vector_add(allocated_sum, allocation)
                group = assignment.get("mutex_group")
                if group is not None:
                    if group in mutex_groups:
                        errors.append(
                            f"快照 {at}/{node_id} 互斥组 {group} 同时存在"
                        )
                    mutex_groups.add(group)
                category = assignment.get("category")
                if category not in nodes[node_id]["categories"]:
                    errors.append(
                        f"快照 {at}/{node_id} 接纳了不支持的类别 {category}"
                    )
                active_assignment_count += 1
            if allocated_sum != used:
                errors.append(f"快照 {at}/{node_id} 活跃分配之和不等于 used")
            if not vector_leq(used, capacity):
                errors.append(f"快照 {at}/{node_id} used 超过容量")
        if seen_nodes != set(nodes):
            errors.append(f"快照 {at} 节点集合不完整")

    summary = result.get("summary", {})
    total_demand = zero_vector()
    total_allocated = zero_vector()
    for bucket in summary.get("by_category", {}).values():
        demand = bucket.get("demand_totals", zero_vector())
        allocated = bucket.get("allocated_totals", zero_vector())
        total_demand = vector_add(total_demand, demand)
        total_allocated = vector_add(total_allocated, allocated)
        if not vector_leq(allocated, demand):
            errors.append(f"类别 {bucket.get('category')} 分配量超过需求量")
    if summary.get("totals", {}).get("jobs_submitted") != sum(
        bucket.get("jobs_submitted", 0)
        for bucket in summary.get("by_category", {}).values()
    ):
        errors.append("汇总 jobs_submitted 与类别分项不一致")

    return {
        "ok": not errors,
        "errors": errors,
        "checks": {
            "decision_count": len(decisions),
            "capacity_change_count": len(changes),
            "snapshot_count": len(snapshots),
            "active_assignment_observations": active_assignment_count,
            "final_replayed_used": replay_used,
            "total_demand": total_demand,
            "total_allocated": total_allocated,
            "total_demand_scalar": vector_sum(total_demand),
            "total_allocated_scalar": vector_sum(total_allocated),
        },
    }

