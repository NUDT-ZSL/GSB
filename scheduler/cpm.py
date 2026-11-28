"""依赖图构建、正向/反向时间参数计算与关键路径识别。"""
import sys

from .errors import BusinessError, E_CYCLE


def build_edges(project):
    """显式依赖与段内顺序取并集；跨段依赖不会被段内顺序覆盖。"""
    kinds = {}
    for a in project.activities.values():
        for p in a.predecessors:
            kinds.setdefault((p, a.activity_id), set()).add("explicit")
    by_seg = {}
    for a in project.activities.values():
        by_seg.setdefault(a.segment_id, []).append(a)
    for acts in by_seg.values():
        acts.sort(key=lambda x: (x.order, x.activity_id))
        for prev, nxt in zip(acts, acts[1:]):
            kinds.setdefault((prev.activity_id, nxt.activity_id),
                             set()).add("segment_order")
    return [(p, s, "+".join(sorted(ks)))
            for (p, s), ks in sorted(kinds.items())]


def adjacency(edges):
    preds, succs = {}, {}
    for p, s, _ in edges:
        preds.setdefault(s, []).append(p)
        succs.setdefault(p, []).append(s)
    return preds, succs


def _cycle_members(node_ids, succs):
    """Tarjan 强连通分量，返回成环成员（含自环）。"""
    sys.setrecursionlimit(max(10000, len(node_ids) * 4 + 100))
    index, lowlink, on_stack, stack, members = {}, {}, set(), [], set()
    counter = [0]

    def strongconnect(v):
        index[v] = lowlink[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in succs.get(v, []):
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])
        if lowlink[v] == index[v]:
            scc = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) > 1:
                members.update(scc)
            elif scc[0] in succs.get(scc[0], []):
                members.add(scc[0])

    for v in node_ids:
        if v not in index:
            strongconnect(v)
    return members


def topo_order(project, edges):
    preds, succs = adjacency(edges)
    indegree = {aid: 0 for aid in project.activities}
    for _, s, _ in edges:
        indegree[s] += 1
    queue = sorted(a for a, d in indegree.items() if d == 0)
    order = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for s in sorted(succs.get(n, [])):
            indegree[s] -= 1
            if indegree[s] == 0:
                queue.append(s)
    if len(order) != len(indegree):
        members = _cycle_members(list(indegree), succs)
        raise BusinessError(E_CYCLE,
                            "工序依赖存在环，无法计算关键路径",
                            {"cycle_members": sorted(members)})
    return order


def forward(project, only=None):
    """正向计算 ES/EF。only 非空时仅重算受影响工序（须对后继封闭）。"""
    edges = build_edges(project)
    preds, _ = adjacency(edges)
    order = topo_order(project, edges)
    target = order if only is None else [n for n in order if n in only]
    for aid in target:
        a = project.activities[aid]
        ps = preds.get(aid, [])
        if ps:
            best = max(project.activities[p].ef_ms for p in ps)
            a.es_ms = best
            a.trace_predecessors = sorted(
                p for p in ps if project.activities[p].ef_ms == best)
        else:
            a.es_ms = project.planned_start_ms
            a.trace_predecessors = []
        if a.actual_finish_ms is not None:
            a.ef_ms = a.actual_finish_ms
        else:
            a.ef_ms = a.es_ms + round(a.duration_hours * 3_600_000)
    return edges


def backward(project, edges):
    """反向计算 LS/LF 与浮动；多个终点时以最晚终点作为反向起点。"""
    _, succs = adjacency(edges)
    order = topo_order(project, edges)
    project_end = max(a.ef_ms for a in project.activities.values())
    for aid in reversed(order):
        a = project.activities[aid]
        eff_duration = a.ef_ms - a.es_ms
        ss = succs.get(aid, [])
        a.lf_ms = min((project.activities[s].ls_ms for s in ss),
                      default=project_end)
        a.ls_ms = a.lf_ms - eff_duration
        a.float_ms = a.ls_ms - a.es_ms
        a.critical = a.float_ms <= 0
    return project_end


def critical_paths(project, edges):
    """零浮动工序沿紧密边（前驱 EF == 后继 ES）串成关键路径序列。"""
    tight = [(p, s) for p, s, _ in edges
             if project.activities[p].critical
             and project.activities[s].critical
             and project.activities[p].ef_ms == project.activities[s].es_ms]
    tight_preds, tight_succs = {}, {}
    for p, s in tight:
        tight_preds.setdefault(s, []).append(p)
        tight_succs.setdefault(p, []).append(s)
    critical_ids = [a.activity_id for a in project.activities.values()
                    if a.critical]
    starts = sorted(a for a in critical_ids if not tight_preds.get(a))
    paths = []

    def dfs(node, path):
        nxts = tight_succs.get(node, [])
        if not nxts:
            paths.append(path)
            return
        for n in sorted(nxts):
            dfs(n, path + [n])

    for s0 in starts:
        dfs(s0, [s0])
    return paths


def critical_causes(project, edges):
    """逐关键工序给出成因说明。"""
    preds, _ = adjacency(edges)
    causes = {}
    for a in project.activities.values():
        if not a.critical:
            continue
        tight_preds = [p for p in preds.get(a.activity_id, [])
                       if project.activities[p].critical
                       and project.activities[p].ef_ms == a.es_ms]
        if tight_preds:
            causes[a.activity_id] = (
                f"总浮动 {a.float_ms / 3_600_000:.1f}h；受关键前驱 "
                f"{', '.join(sorted(tight_preds))} 制约，其完成时刻即本工序最早开始")
        else:
            causes[a.activity_id] = (
                f"总浮动 {a.float_ms / 3_600_000:.1f}h；关键链起点，"
                f"取工程计划开始或实际完成时刻")
    return causes
