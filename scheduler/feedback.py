"""进度反馈事件解析、受影响集合推导、增量重排与一致性核对。"""
import copy

from .cpm import adjacency, backward, build_edges, critical_paths, forward
from .errors import BusinessError, E_EVENT
from .resources import daily_curve

EARLY = "early_finish"
DELAYED = "delayed_finish"
RESOURCE = "resource_change"


def affected_closure(edges, start):
    """沿依赖边从 start 出发的直接+间接波及集合与传播经过的边。"""
    _, succs = adjacency(edges)
    seen = {start}
    stack = [start]
    traversed = []
    while stack:
        node = stack.pop()
        for s in succs.get(node, []):
            traversed.append((node, s))
            if s not in seen:
                seen.add(s)
                stack.append(s)
    return seen, traversed


def _over_list(curve):
    return [{"day": day, "trade": trade,
             "demand": slot["demand"], "cap": slot["cap"],
             "activities": slot["activities"]}
            for (day, trade), slot in sorted(curve.items()) if slot["over"]]


def apply_event(project, event):
    """应用单个事件：仅对受影响工序做增量正向重算，未波及工序 ES/EF 不变。

    反向参数（LS/LF/浮动/关键标记）是全局导出量，随最晚终点整体刷新，
    增量与全量两条路径使用同一套规则，保证逐项一致。
    """
    etype = event.get("type")
    edges = build_edges(project)
    if etype in (EARLY, DELAYED):
        aid = event.get("activity")
        act = project.activities.get(aid)
        if act is None:
            raise BusinessError(E_EVENT, f"反馈事件指向不存在的工序: {aid}")
        actual = event.get("actual_finish_ms")
        if actual is None:
            raise BusinessError(E_EVENT, "完成反馈缺少 actual_finish_ms")
        if int(actual) < act.es_ms:
            raise BusinessError(
                E_EVENT, f"工序 {aid} 的实际完成时刻早于其最早开始",
                {"activity": aid, "actual_finish_ms": actual, "es_ms": act.es_ms})
        act.actual_finish_ms = int(actual)
        affected, traversed = affected_closure(edges, aid)
        kind_map = {(p, s): k for p, s, k in edges}
        old = {x: (project.activities[x].es_ms, project.activities[x].ef_ms)
               for x in affected}
        forward(project, only=affected)
        backward(project, edges)
        curve = daily_curve(project)
        changes = []
        for x in sorted(affected):
            a = project.activities[x]
            o_es, o_ef = old[x]
            if (o_es, o_ef) != (a.es_ms, a.ef_ms):
                changes.append({"activity": x,
                                "old_es_ms": o_es, "old_ef_ms": o_ef,
                                "new_es_ms": a.es_ms, "new_ef_ms": a.ef_ms})
        return {"event": event, "affected": sorted(affected),
                "edges": [{"pred": p, "succ": s, "kind": kind_map[(p, s)]}
                          for p, s in traversed],
                "changes": changes,
                "over_capacity": _over_list(curve)}
    if etype == RESOURCE:
        trade = event.get("trade")
        new_cap = event.get("new_cap")
        if trade not in project.caps:
            raise BusinessError(E_EVENT, f"资源变化事件指向未知工种: {trade}")
        if new_cap is None or float(new_cap) < 0:
            raise BusinessError(E_EVENT, "资源变化事件缺少合法的 new_cap")
        project.caps[trade] = float(new_cap)
        curve = daily_curve(project)
        return {"event": event, "affected": [], "edges": [], "changes": [],
                "over_capacity": _over_list(curve)}
    raise BusinessError(E_EVENT, f"无法识别的事件类型: {etype!r}")


def apply_events(project, events):
    return [apply_event(project, ev) for ev in events]


def full_reschedule(base_project, events):
    """对同一事件序列做全量重排，作为增量结果的对照。"""
    proj = copy.deepcopy(base_project)
    for ev in events:
        etype = ev.get("type")
        if etype in (EARLY, DELAYED):
            proj.activities[ev["activity"]].actual_finish_ms = \
                int(ev["actual_finish_ms"])
        elif etype == RESOURCE:
            proj.caps[ev["trade"]] = float(ev["new_cap"])
        else:
            raise BusinessError(E_EVENT, f"无法识别的事件类型: {ev.get('type')!r}")
    edges = build_edges(proj)
    forward(proj)
    backward(proj, edges)
    return proj, daily_curve(proj)


def consistency_report(incremental_project, incremental_curve,
                       base_project, events):
    """逐工序核对增量与全量结果，输出逐项报告而非整体结论。"""
    full_project, full_curve = full_reschedule(base_project, events)
    ok = True
    activity_lines = []
    for aid in sorted(full_project.activities):
        inc = incremental_project.activities[aid]
        ful = full_project.activities[aid]
        match = (inc.es_ms == ful.es_ms and inc.ef_ms == ful.ef_ms
                 and inc.ls_ms == ful.ls_ms and inc.lf_ms == ful.lf_ms
                 and inc.critical == ful.critical)
        ok = ok and match
        activity_lines.append({
            "activity": aid, "match": match,
            "incremental": {"es_ms": inc.es_ms, "ef_ms": inc.ef_ms,
                            "critical": inc.critical},
            "full": {"es_ms": ful.es_ms, "ef_ms": ful.ef_ms,
                     "critical": ful.critical},
        })
    resource_lines = []
    for key in sorted(set(incremental_curve) | set(full_curve)):
        a = incremental_curve.get(key)
        b = full_curve.get(key)
        match = (a is not None and b is not None
                 and a["demand"] == b["demand"] and a["over"] == b["over"])
        ok = ok and match
        resource_lines.append({"day": key[0], "trade": key[1], "match": match})
    paths_inc = critical_paths(incremental_project,
                               build_edges(incremental_project))
    paths_full = critical_paths(full_project, build_edges(full_project))
    paths_match = (sorted(tuple(p) for p in paths_inc)
                   == sorted(tuple(p) for p in paths_full))
    ok = ok and paths_match
    return {"ok": ok, "activities": activity_lines,
            "resources": resource_lines,
            "critical_paths_incremental": paths_inc,
            "critical_paths_full": paths_full,
            "critical_paths_match": paths_match}
