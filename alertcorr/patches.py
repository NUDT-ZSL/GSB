"""规则增量更新与依赖失效闭包。

补丁以 JSON 形式提交: 依赖关系 / 实例权重 / 静默与维护窗口的增删。
应用补丁后沿依赖图计算受影响闭包, 输出失效清单(触发来源 + 波及层级)。
"""

from . import errors as E
from .model import Instance, Service, Topology, weight_to_micro
from .validation import detect_cycle, validate_window_dict

CHANGE_TYPES = ("set_dependencies", "set_weight", "add_window", "remove_window")


def validate_patch_dict(raw):
    if not isinstance(raw, dict):
        raise E.BusinessError(E.E3007_BAD_PATCH_SHAPE, "patch must be object")
    patch_id = raw.get("patch_id")
    base_version = raw.get("base_version")
    changes = raw.get("changes")
    if not (isinstance(patch_id, str) and patch_id):
        raise E.BusinessError(E.E3007_BAD_PATCH_SHAPE, "patch_id missing")
    if not (isinstance(base_version, int) and not isinstance(base_version, bool)
            and base_version >= 1):
        raise E.BusinessError(E.E3007_BAD_PATCH_SHAPE,
                              "base_version must be int >= 1",
                              base_version=base_version)
    if not (isinstance(changes, list) and changes):
        raise E.BusinessError(E.E3007_BAD_PATCH_SHAPE,
                              "changes must be non-empty list", patch_id=patch_id)
    for ch in changes:
        if not isinstance(ch, dict) or ch.get("type") not in CHANGE_TYPES:
            raise E.BusinessError(E.E3007_BAD_PATCH_SHAPE,
                                  "unknown change type",
                                  patch_id=patch_id, change=ch)
    return raw


def apply_patch_to_topology(topology, patch):
    """在内存中应用补丁, 返回 (新Topology, affected: {service: layer}, sources)。

    affected 的 value 是距变更源的波及层级(下游距离)。
    校验失败抛 BusinessError, 不修改任何状态。
    """
    validate_patch_dict(patch)
    if patch["base_version"] < topology.version:
        raise E.BusinessError(
            E.E3003_TOPOLOGY_VERSION_ROLLBACK,
            "patch base_version older than current topology version",
            patch_id=patch["patch_id"], base_version=patch["base_version"],
            current_version=topology.version)
    if patch["base_version"] != topology.version:
        raise E.BusinessError(
            E.E3005_PATCH_BASE_VERSION_MISMATCH,
            "patch base_version does not match current topology version",
            patch_id=patch["patch_id"], base_version=patch["base_version"],
            current_version=topology.version)

    services = {n: Service(name=s.name, fault_domain=s.fault_domain,
                           upstream=tuple(s.upstream),
                           instances=tuple(s.instances))
                for n, s in topology.services.items()}
    instances = dict(topology.instances)
    windows = dict(topology.windows)
    affected = {}
    sources = []

    def mark(origin, layer_map):
        for svc, layer in layer_map.items():
            if svc not in affected or layer < affected[svc]:
                affected[svc] = layer

    def mark_zero(scope):
        if scope and (scope not in affected or 0 < affected[scope]):
            affected[scope] = 0

    for ch in patch["changes"]:
        ctype = ch["type"]
        if ctype == "set_dependencies":
            svc = ch.get("service")
            if svc not in services:
                raise E.BusinessError(E.E3001_PATCH_UNKNOWN_SERVICE,
                                      "set_dependencies references unknown "
                                      "service", patch_id=patch["patch_id"],
                                      service=svc)
            upstream = ch.get("upstream", [])
            if not (isinstance(upstream, list)
                    and all(isinstance(u, str) for u in upstream)):
                raise E.BusinessError(E.E3007_BAD_PATCH_SHAPE,
                                      "upstream must be list of names",
                                      patch_id=patch["patch_id"], service=svc)
            if svc in upstream:
                raise E.BusinessError(E.E1004_SELF_DEPENDENCY,
                                      "service must not depend on itself",
                                      service=svc)
            for up in upstream:
                if up not in services:
                    raise E.BusinessError(E.E1003_UNKNOWN_UPSTREAM,
                                          "upstream dependency not declared",
                                          service=svc, upstream=up)
            old = services[svc]
            services[svc] = Service(name=old.name, fault_domain=old.fault_domain,
                                    upstream=tuple(sorted(set(upstream))),
                                    instances=old.instances)
            probe = {n: set(s.upstream) for n, s in services.items()}
            cycle = detect_cycle(probe)
            if cycle:
                raise E.BusinessError(E.E1005_DEPENDENCY_CYCLE,
                                      "patch introduces dependency cycle",
                                      patch_id=patch["patch_id"], cycle=cycle)
            tmp = Topology(version=topology.version, services=services,
                           instances=instances, windows=windows)
            mark(svc, tmp.downstream_closure(svc))
            sources.append("set_dependencies:%s" % svc)
        elif ctype == "set_weight":
            iid = ch.get("instance_id")
            if iid not in instances:
                raise E.BusinessError(E.E3004_PATCH_UNKNOWN_INSTANCE,
                                      "set_weight references unknown instance",
                                      patch_id=patch["patch_id"],
                                      instance_id=iid)
            weight = ch.get("weight")
            if not (isinstance(weight, (int, float))
                    and not isinstance(weight, bool) and weight > 0):
                raise E.BusinessError(E.E1007_BAD_WEIGHT,
                                      "weight must be finite > 0",
                                      instance_id=iid, weight=weight)
            inst = instances[iid]
            instances[iid] = Instance(instance_id=iid, service=inst.service,
                                      weight_micro=weight_to_micro(weight))
            tmp = Topology(version=topology.version, services=services,
                           instances=instances, windows=windows)
            mark(inst.service, tmp.downstream_closure(inst.service))
            sources.append("set_weight:%s" % iid)
        elif ctype == "add_window":
            win = validate_window_dict(ch.get("window"), set(services),
                                       set(instances))
            windows[win.window_id] = win
            scope = win.service or (
                instances[win.instance_id].service if win.instance_id else "")
            mark_zero(scope)
            sources.append("add_window:%s" % win.window_id)
        elif ctype == "remove_window":
            wid = ch.get("window_id")
            if wid not in windows:
                raise E.BusinessError(E.E3006_UNKNOWN_WINDOW,
                                      "remove_window references unknown window",
                                      patch_id=patch["patch_id"],
                                      window_id=wid)
            win = windows.pop(wid)
            scope = win.service or (
                instances[win.instance_id].service if win.instance_id else "")
            mark_zero(scope)
            sources.append("remove_window:%s" % wid)

    new_topo = Topology(version=topology.version + 1, services=services,
                        instances=instances, windows=windows)
    return new_topo, affected, sources
