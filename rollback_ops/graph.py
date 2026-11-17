"""回滚依赖图：强校验（唯一标识、引用存在、无环）、确定性拓扑序、删除保护。"""

from __future__ import annotations

import heapq

from .errors import E_DEPEND, OpsError
from .models import RollbackPlan


def _dependents_map(plan: RollbackPlan):
    """step_id -> 排序后的直接依赖者元组。"""
    dependents = {s.step_id: [] for s in plan.steps}
    for step in plan.steps:
        for dep in step.depends_on:
            if dep in dependents:
                dependents[dep].append(step.step_id)
    return {k: tuple(sorted(v)) for k, v in dependents.items()}


def validate_plan(plan: RollbackPlan) -> None:
    ids = [s.step_id for s in plan.steps]
    seen = set()
    duplicates = sorted({i for i in ids if i in seen or seen.add(i)})
    if duplicates:
        raise OpsError(E_DEPEND, "duplicate rollback step ids", {"duplicates": duplicates})

    id_set = set(ids)
    missing = []
    for step in plan.steps:
        for dep in step.depends_on:
            if dep not in id_set:
                missing.append({"step_id": step.step_id, "missing_dependency": dep})
    if missing:
        raise OpsError(E_DEPEND, "dependency references unknown step",
                       {"missing_references": sorted(missing, key=lambda m: (m["step_id"], m["missing_dependency"]))})

    cycle = find_cycle(plan)
    if cycle:
        raise OpsError(E_DEPEND, "dependency cycle detected", {"cycle": cycle})


def find_cycle(plan: RollbackPlan):
    """确定性环检测：节点与邻边均按字典序遍历，返回环上步骤序列（首尾相接）或 None。"""
    deps = {s.step_id: tuple(sorted(s.depends_on)) for s in plan.steps}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in deps}
    stack = []

    def visit(node):
        color[node] = GRAY
        stack.append(node)
        for nxt in deps[node]:
            if color.get(nxt) == GRAY:
                idx = stack.index(nxt)
                return stack[idx:] + [nxt]
            if color.get(nxt) == WHITE:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for sid in sorted(deps):
        if color[sid] == WHITE:
            found = visit(sid)
            if found:
                return found
    return None


def topo_order(plan: RollbackPlan) -> list:
    """Kahn 算法 + 最小堆，输出确定性的拓扑序（不依赖字典/哈希顺序）。"""
    validate_plan(plan)
    indegree = {s.step_id: len(s.depends_on) for s in plan.steps}
    dependents = _dependents_map(plan)
    heap = [sid for sid, deg in indegree.items() if deg == 0]
    heapq.heapify(heap)
    order = []
    while heap:
        node = heapq.heappop(heap)
        order.append(node)
        for dependent in dependents[node]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(heap, dependent)
    return order


def remove_step(plan: RollbackPlan, step_id: str) -> RollbackPlan:
    """删除回滚步骤；若仍被其他步骤依赖则拒绝并列出依赖者。"""
    step_map = plan.step_map()
    if step_id not in step_map:
        raise OpsError(E_DEPEND, "step not found", {"step_id": step_id})
    dependents = sorted(
        s.step_id for s in plan.steps if step_id in s.depends_on
    )
    if dependents:
        raise OpsError(E_DEPEND, "step is still depended upon",
                       {"step_id": step_id, "dependents": dependents})
    remaining = tuple(s for s in plan.steps if s.step_id != step_id)
    new_plan = RollbackPlan(plan_version=plan.plan_version, steps=remaining)
    validate_plan(new_plan)
    return new_plan


def dependents_closure(plan: RollbackPlan, step_ids) -> list:
    """给定步骤集合的全部传递依赖者（含自身），排序输出。"""
    dependents = _dependents_map(plan)
    affected = set(step_ids)
    queue = sorted(step_ids)
    while queue:
        node = queue.pop(0)
        for dependent in dependents.get(node, ()):
            if dependent not in affected:
                affected.add(dependent)
                queue.append(dependent)
    return sorted(affected)
