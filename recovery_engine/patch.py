"""补丁驱动的依赖失效闭包与结论复用裁决。

补丁操作：upsert_service / remove_service / set_upstreams /
set_actions / set_ladder / tombstone。

失效闭包沿 “服务—依赖上游—恢复动作—计划执行结论” 关系计算：
- 直接变更的服务为种子；
- 沿依赖边向下游传播（依赖了变更服务的服务，传递闭包）；
- 闭包内服务的动作与计划执行结论（failover/failback）标记为重算；
- 闭包外服务的结论、来源版本与证据链逐字保持不变（安全复用）。
"""

import copy

from .errors import BizError, ErrCode
from .models import Action, Service

PATCH_OPS = {"upsert_service", "remove_service", "set_upstreams",
             "set_actions", "set_ladder", "tombstone"}


def apply_patch(plan, patch):
    """应用补丁并返回 (新计划, 失效闭包信息)。不修改入参 plan。"""
    new_plan = copy.deepcopy(plan)
    seeds = set()
    for op in patch.get("patches", []):
        kind = op.get("op")
        if kind not in PATCH_OPS:
            raise BizError(ErrCode.E_BAD_CONFIG, "未知补丁操作", op=kind)
        if kind == "upsert_service":
            entry = op["service"]
            name = entry["name"]
            new_plan.services[name] = Service(
                name=name,
                failure_domain=entry.get("failure_domain", ""),
                actions=[Action(action_id=a["id"], kind=a.get("kind", "switch"),
                                at_tick=a.get("at_ms", a.get("at_tick", 0)),
                                script=a.get("script", ""),
                                source_version=entry.get("source_version", "v2"),
                                priority=a.get("priority", ""), seq=i)
                         for i, a in enumerate(entry.get("actions", []))],
                upstreams=list(entry.get("upstreams", [])),
                tombstoned=bool(entry.get("tombstoned", False)),
                source_version=entry.get("source_version", "v2"),
                effective_ms=entry.get("effective_ms", 0),
                generation=entry.get("generation", 0),
                fallback_seconds=entry.get("fallback_seconds", 0),
                ladder=list(entry.get("ladder", [10, 25, 50, 100])),
            )
            seeds.add(name)
        elif kind == "remove_service":
            name = op["name"]
            if name not in new_plan.services:
                raise BizError(ErrCode.E_BAD_CONFIG, "补丁目标服务不存在", service=name)
            del new_plan.services[name]
            seeds.add(name)
        elif kind == "set_upstreams":
            name = op["name"]
            svc = new_plan.services.get(name)
            if svc is None:
                raise BizError(ErrCode.E_BAD_CONFIG, "补丁目标服务不存在", service=name)
            svc.upstreams = list(op["upstreams"])
            seeds.add(name)
        elif kind == "set_actions":
            name = op["name"]
            svc = new_plan.services.get(name)
            if svc is None:
                raise BizError(ErrCode.E_BAD_CONFIG, "补丁目标服务不存在", service=name)
            svc.actions = [Action(action_id=a["id"], kind=a.get("kind", "switch"),
                                  at_tick=a.get("at_ms", a.get("at_tick", 0)),
                                  script=a.get("script", ""),
                                  source_version=svc.source_version,
                                  priority=a.get("priority", ""), seq=i)
                           for i, a in enumerate(op["actions"])]
            seeds.add(name)
        elif kind == "set_ladder":
            name = op["name"]
            svc = new_plan.services.get(name)
            if svc is None:
                raise BizError(ErrCode.E_BAD_CONFIG, "补丁目标服务不存在", service=name)
            svc.ladder = list(op["ladder"])
            seeds.add(name)
        elif kind == "tombstone":
            name = op["name"]
            svc = new_plan.services.get(name)
            if svc is None:
                raise BizError(ErrCode.E_BAD_CONFIG, "补丁目标服务不存在", service=name)
            svc.tombstoned = bool(op.get("value", True))
            seeds.add(name)

    # 校验补丁后依赖图（上游存在、无自环、无环）
    for name, svc in new_plan.services.items():
        for up in svc.upstreams:
            if up == name:
                raise BizError(ErrCode.E_BAD_CONFIG, "依赖图存在自环", service=name)
            if up not in new_plan.services:
                raise BizError(ErrCode.E_BAD_CONFIG, "依赖上游服务不存在",
                               service=name, upstream=up)
    new_plan.topo_order()  # 成环抛 E_CYCLE

    closure = new_plan.dependents_closure(seeds)
    affected = sorted(closure & set(new_plan.services))
    reusable = sorted(set(new_plan.services) - closure)
    info = {
        "seeds": sorted(seeds),
        "affected_services": affected,
        "affected_actions": sorted(
            a.action_id for n in affected for a in new_plan.services[n].actions),
        "recompute_conclusions": [
            {"service": n, "kind": k} for n in affected for k in ("failover", "failback")],
        "reusable_services": reusable,
    }
    return new_plan, info


def invalidate(store, closure_info):
    """按失效闭包精确删除需重算的结论；未受影响结论逐字保留。"""
    with store.transaction() as conn:
        store.delete_conclusions(conn, closure_info["affected_services"])
