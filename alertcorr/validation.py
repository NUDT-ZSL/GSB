"""拓扑 / 事件流 / 补丁的解析校验。任何非法输入抛 BusinessError, 不产生持久化写入。"""

import math

from . import errors as E
from .model import (
    EVENT_FLAGS,
    SEVERITIES,
    WINDOW_KINDS,
    Event,
    Instance,
    Service,
    Topology,
    Window,
    weight_to_micro,
)


def detect_cycle(upstream_map):
    """Kahn 拓扑排序检测环; 有环时返回环上节点列表(确定性序), 无环返回 []。"""
    remaining = {name: set(ups) for name, ups in upstream_map.items()}
    progressed = True
    while progressed:
        progressed = False
        for name in sorted(list(remaining)):
            deps = remaining[name] & remaining.keys()
            if not deps:
                del remaining[name]
                for ups in remaining.values():
                    ups.discard(name)
                progressed = True
    return sorted(remaining)


def _require(cond, code, message, **ctx):
    if not cond:
        raise E.BusinessError(code, message, **ctx)


def validate_window_dict(raw, known_services=None, known_instances=None):
    _require(isinstance(raw, dict), E.E3007_BAD_PATCH_SHAPE, "window must be object")
    window_id = raw.get("id")
    kind = raw.get("kind")
    start = raw.get("start_ms")
    end = raw.get("end_ms")
    service = raw.get("service") or ""
    instance_id = raw.get("instance_id") or ""
    _require(isinstance(window_id, str) and window_id, E.E3007_BAD_PATCH_SHAPE,
             "window id missing", window=raw)
    _require(kind in WINDOW_KINDS, E.E3007_BAD_PATCH_SHAPE,
             "window kind must be silence|maintenance", window_id=window_id, kind=kind)
    _require(isinstance(start, int) and isinstance(end, int)
             and not isinstance(start, bool) and not isinstance(end, bool),
             E.E3007_BAD_PATCH_SHAPE, "window bounds must be int ms", window_id=window_id)
    _require(0 <= start <= end, E.E3002_INVALID_WINDOW_RANGE,
             "window range invalid: require 0 <= start_ms <= end_ms",
             window_id=window_id, start_ms=start, end_ms=end)
    if known_services is not None and service:
        _require(service in known_services, E.E3001_PATCH_UNKNOWN_SERVICE,
                 "window references unknown service", window_id=window_id, service=service)
    if known_instances is not None and instance_id:
        _require(instance_id in known_instances, E.E3004_PATCH_UNKNOWN_INSTANCE,
                 "window references unknown instance", window_id=window_id,
                 instance_id=instance_id)
    return Window(window_id=window_id, kind=kind, service=service,
                  instance_id=instance_id, start_ms=start, end_ms=end)


def validate_topology_dict(raw):
    """解析并校验拓扑 JSON, 返回 Topology。非法输入抛 BusinessError。"""
    _require(isinstance(raw, dict), E.E1008_BAD_TOPOLOGY_SHAPE, "topology must be object")
    version = raw.get("version")
    _require(isinstance(version, int) and not isinstance(version, bool) and version >= 1,
             E.E1008_BAD_TOPOLOGY_SHAPE, "topology.version must be int >= 1",
             version=version)
    services_raw = raw.get("services")
    _require(isinstance(services_raw, list) and services_raw,
             E.E1008_BAD_TOPOLOGY_SHAPE, "topology.services must be non-empty list")

    services = {}
    instances = {}
    for svc_raw in services_raw:
        _require(isinstance(svc_raw, dict), E.E1008_BAD_TOPOLOGY_SHAPE,
                 "service entry must be object")
        name = svc_raw.get("name")
        _require(isinstance(name, str) and name, E.E1008_BAD_TOPOLOGY_SHAPE,
                 "service name missing")
        _require(name not in services, E.E1001_DUPLICATE_SERVICE,
                 "duplicate service name", service=name)
        domain = svc_raw.get("fault_domain")
        _require(isinstance(domain, str) and domain, E.E1008_BAD_TOPOLOGY_SHAPE,
                 "fault_domain missing", service=name)
        upstream = svc_raw.get("upstream", [])
        _require(isinstance(upstream, list)
                 and all(isinstance(u, str) for u in upstream),
                 E.E1008_BAD_TOPOLOGY_SHAPE, "upstream must be list of service names",
                 service=name)
        _require(name not in upstream, E.E1004_SELF_DEPENDENCY,
                 "service must not depend on itself", service=name)
        insts_raw = svc_raw.get("instances")
        _require(isinstance(insts_raw, list) and insts_raw,
                 E.E1008_BAD_TOPOLOGY_SHAPE, "instances must be non-empty list",
                 service=name)
        inst_ids = []
        for inst_raw in insts_raw:
            _require(isinstance(inst_raw, dict), E.E1008_BAD_TOPOLOGY_SHAPE,
                     "instance entry must be object", service=name)
            iid = inst_raw.get("id")
            _require(isinstance(iid, str) and iid, E.E1008_BAD_TOPOLOGY_SHAPE,
                     "instance id missing", service=name)
            _require(iid not in instances, E.E1002_DUPLICATE_INSTANCE,
                     "duplicate instance id", instance_id=iid)
            weight = inst_raw.get("weight")
            _require(isinstance(weight, (int, float))
                     and not isinstance(weight, bool)
                     and math.isfinite(weight) and weight > 0,
                     E.E1007_BAD_WEIGHT, "instance weight must be finite > 0",
                     instance_id=iid, weight=weight)
            inst_ids.append(iid)
            instances[iid] = Instance(instance_id=iid, service=name,
                                      weight_micro=weight_to_micro(weight))
        services[name] = Service(name=name, fault_domain=domain,
                                 upstream=tuple(sorted(set(upstream))),
                                 instances=tuple(sorted(inst_ids)))

    for name, svc in services.items():
        for up in svc.upstream:
            _require(up in services, E.E1003_UNKNOWN_UPSTREAM,
                     "upstream dependency not declared", service=name, upstream=up)

    cycle = detect_cycle({n: set(s.upstream) for n, s in services.items()})
    _require(not cycle, E.E1005_DEPENDENCY_CYCLE,
             "dependency graph has a cycle", cycle=cycle)

    windows = {}
    for win_raw in raw.get("windows", []):
        win = validate_window_dict(win_raw, set(services), set(instances))
        _require(win.window_id not in windows, E.E3007_BAD_PATCH_SHAPE,
                 "duplicate window id", window_id=win.window_id)
        windows[win.window_id] = win

    return Topology(version=version, services=services,
                    instances=instances, windows=windows)


def validate_events_list(raw_events, topology):
    """校验事件流, 返回按 (ts, seq) 排序前的 Event 列表(保持输入顺序)。"""
    _require(isinstance(raw_events, list), E.E2007_BAD_EVENT_SHAPE,
             "events must be a list")
    events = []
    prev_seq = None
    for raw in raw_events:
        _require(isinstance(raw, dict), E.E2007_BAD_EVENT_SHAPE,
                 "event entry must be object")
        seq = raw.get("seq")
        _require(isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0,
                 E.E2007_BAD_EVENT_SHAPE, "event seq must be non-negative int", seq=seq)
        if prev_seq is not None:
            _require(seq > prev_seq, E.E2001_SEQ_NOT_INCREASING,
                     "event seq must be strictly increasing",
                     prev_seq=prev_seq, seq=seq)
        prev_seq = seq
        iid = raw.get("instance_id")
        _require(iid in topology.instances, E.E2006_UNKNOWN_INSTANCE,
                 "event references undeclared instance", seq=seq, instance_id=iid)
        rule = raw.get("rule")
        _require(isinstance(rule, str) and rule, E.E2007_BAD_EVENT_SHAPE,
                 "event rule missing", seq=seq)
        severity = raw.get("severity")
        _require(severity in SEVERITIES, E.E2003_INVALID_SEVERITY,
                 "invalid severity", seq=seq, severity=severity,
                 allowed=list(SEVERITIES))
        ts = raw.get("logical_ts_ms")
        _require(isinstance(ts, int) and not isinstance(ts, bool),
                 E.E2007_BAD_EVENT_SHAPE, "logical_ts_ms must be int", seq=seq)
        _require(ts >= 0, E.E2002_NEGATIVE_CLOCK,
                 "logical clock must be non-negative", seq=seq, logical_ts_ms=ts)
        metric = raw.get("metric")
        _require(isinstance(metric, (int, float)) and not isinstance(metric, bool)
                 and math.isfinite(metric),
                 E.E2005_NON_FINITE_METRIC, "metric must be finite",
                 seq=seq, metric=metric)
        flag = raw.get("flag", "none")
        _require(flag in EVENT_FLAGS, E.E2004_INVALID_FLAG,
                 "invalid silence/maintenance flag", seq=seq, flag=flag,
                 allowed=list(EVENT_FLAGS))
        events.append(Event(seq=seq, instance_id=iid,
                            service=topology.instances[iid].service,
                            rule=rule, severity=severity, ts_ms=ts,
                            metric=float(metric), flag=flag))
    return events
