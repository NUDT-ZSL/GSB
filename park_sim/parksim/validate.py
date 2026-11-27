"""拓扑与指令输入的显式校验。

所有校验错误都带可定位的整数位置（行号 / 序号，从 1 开始），
校验失败时抛出 ValidationError，绝不静默修正数据。
"""
from __future__ import annotations

from typing import List

from .model import Scenario, Topology


class ValidationError(Exception):
    """携带整数位置与原因的校验错误。"""

    def __init__(self, position: int, where: str, reason: str):
        self.position = position
        self.where = where
        self.reason = reason
        super().__init__(f"[{where} #{position}] {reason}")


def validate_topology(topo: Topology) -> None:
    if not topo.nodes:
        raise ValidationError(0, "topology", "拓扑为空：至少需要一个节点")
    seen_edges = set()
    for i, e in enumerate(topo.edges, start=1):
        if e.a not in topo.nodes:
            raise ValidationError(i, "edge", f"路径引用了不存在的节点: {e.a}")
        if e.b not in topo.nodes:
            raise ValidationError(i, "edge", f"路径引用了不存在的节点: {e.b}")
        if e.a == e.b:
            raise ValidationError(i, "edge", f"路径两端为同一节点: {e.a}")
        if e.distance < 0:
            raise ValidationError(i, "edge", f"路径距离为负数: {e.distance}")
        if e.distance == 0:
            raise ValidationError(i, "edge", "路径距离为 0，必须为正整数")
        key = (e.a, e.b) if not e.bidirectional else tuple(sorted((e.a, e.b)))
        if key in seen_edges:
            raise ValidationError(i, "edge", f"重复路径: {e.a} <-> {e.b}")
        seen_edges.add(key)


def validate_scenario(sc: Scenario) -> None:
    validate_topology(sc.topology)
    if sc.total_rounds <= 0:
        raise ValidationError(0, "scenario", "总回合数必须为正整数")
    if sc.start_node not in sc.topology.nodes:
        raise ValidationError(0, "scenario", f"起点节点不存在: {sc.start_node}")
    if sc.init_stamina < 0 or sc.init_supply < 0:
        raise ValidationError(0, "scenario", "初始体力/补给不能为负")
    if sc.supply_capacity <= 0:
        raise ValidationError(0, "scenario", "补给容量必须为正整数")
    for i, t in enumerate(sc.targets, start=1):
        if t not in sc.topology.nodes:
            raise ValidationError(i, "target", f"目标设施不存在: {t}")
        if sc.topology.nodes[t].kind != "facility":
            raise ValidationError(i, "target", f"目标节点不是设施: {t}")
    for w in sc.weather:
        if not 1 <= w.round_no <= sc.total_rounds:
            raise ValidationError(w.round_no, "weather",
                                  f"天气事件回合越界（1..{sc.total_rounds}）: {w.name}")
        if w.stamina_factor < 0 or w.supply_factor < 0:
            raise ValidationError(w.round_no, "weather", "修正系数不能为负")
    for c in sc.crowd:
        if not 1 <= c.round_no <= sc.total_rounds:
            raise ValidationError(c.round_no, "crowd",
                                  f"客流系数回合越界（1..{sc.total_rounds}）")
        if c.stamina_factor < 0 or c.supply_factor < 0:
            raise ValidationError(c.round_no, "crowd", "修正系数不能为负")
    for i, cmd in enumerate(sc.commands, start=1):
        if cmd.kind not in ("MOVE", "RESUPPLY", "REST", "WAIT"):
            raise ValidationError(i, "command", f"未知指令类型: {cmd.kind}")
        if cmd.kind == "MOVE":
            if len(cmd.waypoints) < 2:
                raise ValidationError(i, "command", "MOVE 至少需要 2 个途经节点")
            for wp in cmd.waypoints:
                if wp not in sc.topology.nodes:
                    raise ValidationError(i, "command", f"指令引用不存在节点: {wp}")
        if cmd.kind in ("RESUPPLY", "REST") and cmd.amount <= 0:
            raise ValidationError(i, "command", f"{cmd.kind} 数量必须为正整数")


def collect_errors(sc: Scenario) -> List[str]:
    """把校验错误汇总为字符串列表（供 CLI 展示）。"""
    try:
        validate_scenario(sc)
        return []
    except ValidationError as exc:
        return [str(exc)]
