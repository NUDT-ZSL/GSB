"""关卡配置（JSON）的读取与写入。

配置即“参数修改入口”：策划可手工编辑 JSON 后重新推演。
所有解析错误都带整数位置（列表序号，从 1 开始）。
"""
from __future__ import annotations

import json
from typing import Any, Dict

from .model import (Command, CrowdFactor, Edge, Node, Scenario, Topology,
                    WeatherEvent)
from .validate import ValidationError, validate_scenario

SCHEMA_VERSION = 1


def _req(d: Dict[str, Any], key: str, pos: int, where: str) -> Any:
    if key not in d:
        raise ValidationError(pos, where, f"缺少必填字段: {key}")
    return d[key]


def _int(v: Any, pos: int, where: str, key: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValidationError(pos, where, f"字段 {key} 必须为整数，得到: {v!r}")
    return v


def topology_from_dict(data: Dict[str, Any]) -> Topology:
    topo = Topology()
    for i, nd in enumerate(data.get("nodes", []), start=1):
        name = _req(nd, "name", i, "node")
        kind = nd.get("kind", "facility")
        if kind not in ("facility", "supply", "rest", "gate"):
            raise ValidationError(i, "node", f"未知节点类型: {kind}")
        if name in topo.nodes:
            raise ValidationError(i, "node", f"重复节点: {name}")
        topo.nodes[name] = Node(name=name, kind=kind)
    for i, ed in enumerate(data.get("edges", []), start=1):
        topo.edges.append(Edge(
            a=_req(ed, "a", i, "edge"),
            b=_req(ed, "b", i, "edge"),
            distance=_int(_req(ed, "distance", i, "edge"), i, "edge", "distance"),
            bidirectional=bool(ed.get("bidirectional", True)),
        ))
    return topo


def scenario_from_dict(data: Dict[str, Any]) -> Scenario:
    if data.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ValidationError(0, "schema", "不支持的 schema_version")
    topo = topology_from_dict(_req(data, "topology", 0, "scenario"))
    sc = Scenario(
        topology=topo,
        total_rounds=_int(_req(data, "total_rounds", 0, "scenario"), 0, "scenario", "total_rounds"),
        start_node=_req(data, "start_node", 0, "scenario"),
        init_stamina=_int(_req(data, "init_stamina", 0, "scenario"), 0, "scenario", "init_stamina"),
        init_supply=_int(_req(data, "init_supply", 0, "scenario"), 0, "scenario", "init_supply"),
        supply_capacity=_int(_req(data, "supply_capacity", 0, "scenario"), 0, "scenario", "supply_capacity"),
        targets=list(_req(data, "targets", 0, "scenario")),
    )
    for i, w in enumerate(data.get("weather", []), start=1):
        sc.weather.append(WeatherEvent(
            round_no=_int(_req(w, "round", i, "weather"), i, "weather", "round"),
            name=_req(w, "name", i, "weather"),
            stamina_factor=_int(w.get("stamina_factor", 1000), i, "weather", "stamina_factor"),
            supply_factor=_int(w.get("supply_factor", 1000), i, "weather", "supply_factor"),
        ))
    for i, c in enumerate(data.get("crowd", []), start=1):
        sc.crowd.append(CrowdFactor(
            round_no=_int(_req(c, "round", i, "crowd"), i, "crowd", "round"),
            stamina_factor=_int(c.get("stamina_factor", 1000), i, "crowd", "stamina_factor"),
            supply_factor=_int(c.get("supply_factor", 1000), i, "crowd", "supply_factor"),
        ))
    for i, cd in enumerate(data.get("commands", []), start=1):
        kind = _req(cd, "kind", i, "command")
        sc.commands.append(Command(
            kind=kind,
            waypoints=tuple(cd.get("waypoints", [])),
            amount=_int(cd.get("amount", 0), i, "command", "amount"),
        ))
    validate_scenario(sc)
    return sc


def load_scenario(path: str) -> Scenario:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return scenario_from_dict(data)


def scenario_to_dict(sc: Scenario) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "topology": {
            "nodes": [{"name": n.name, "kind": n.kind} for n in sc.topology.nodes.values()],
            "edges": [{"a": e.a, "b": e.b, "distance": e.distance,
                       "bidirectional": e.bidirectional} for e in sc.topology.edges],
        },
        "total_rounds": sc.total_rounds,
        "start_node": sc.start_node,
        "init_stamina": sc.init_stamina,
        "init_supply": sc.init_supply,
        "supply_capacity": sc.supply_capacity,
        "targets": list(sc.targets),
        "weather": [{"round": w.round_no, "name": w.name,
                     "stamina_factor": w.stamina_factor,
                     "supply_factor": w.supply_factor} for w in sc.weather],
        "crowd": [{"round": c.round_no, "stamina_factor": c.stamina_factor,
                   "supply_factor": c.supply_factor} for c in sc.crowd],
        "commands": [{"kind": c.kind, "waypoints": list(c.waypoints),
                      "amount": c.amount} for c in sc.commands],
    }


def dump_scenario(sc: Scenario, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(scenario_to_dict(sc), fh, ensure_ascii=False, indent=2,
                  sort_keys=True)
        fh.write("\n")
