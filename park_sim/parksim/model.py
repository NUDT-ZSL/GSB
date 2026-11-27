"""数据模型：拓扑、场景配置、指令与推演结果。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Node:
    """园区节点。kind 为 facility / supply / rest / gate。"""
    name: str
    kind: str = "facility"


@dataclass(frozen=True)
class Edge:
    """连接路径。bidirectional=False 表示仅 a -> b 单向。"""
    a: str
    b: str
    distance: int
    bidirectional: bool = True


@dataclass
class Topology:
    nodes: Dict[str, Node] = field(default_factory=dict)
    edges: List[Edge] = field(default_factory=list)


@dataclass(frozen=True)
class WeatherEvent:
    """某回合的天气事件。factor 为千分比整数（1000 = 不修正）。"""
    round_no: int
    name: str
    stamina_factor: int = 1000
    supply_factor: int = 1000


@dataclass(frozen=True)
class CrowdFactor:
    """某回合的游客流量修正（千分比整数）。"""
    round_no: int
    stamina_factor: int = 1000
    supply_factor: int = 1000


@dataclass
class Scenario:
    """一局的完整输入参数。"""
    topology: Topology
    total_rounds: int
    start_node: str
    init_stamina: int
    init_supply: int
    supply_capacity: int
    targets: List[str]
    weather: List[WeatherEvent] = field(default_factory=list)
    crowd: List[CrowdFactor] = field(default_factory=list)
    commands: List["Command"] = field(default_factory=list)


@dataclass(frozen=True)
class Command:
    """玩家指令。

    kind:
      MOVE     沿 waypoints 依次行军（相邻途经点之间取最短路径）
      RESUPPLY 在当前节点（须为补给站）补充 amount 点补给
      REST     在当前节点（须为休息区）恢复 amount 点体力
      WAIT     停留一回合
    """
    kind: str
    waypoints: Tuple[str, ...] = ()
    amount: int = 0


@dataclass
class Alert:
    round_no: int
    node: str
    code: str
    message: str
    policy: str


@dataclass
class ModifierTrace:
    source: str
    field: str
    factor: int
    before: int
    after: int


@dataclass
class SegmentCost:
    frm: str
    to: str
    distance: int
    base_stamina: int
    base_supply: int
    final_stamina: int
    final_supply: int
    modifiers: List[ModifierTrace] = field(default_factory=list)


@dataclass
class RoundRecord:
    round_no: int
    command: str
    node_before: str
    node_after: str
    stamina_before: int
    stamina_after: int
    supply_before: int
    supply_after: int
    segments: List[SegmentCost] = field(default_factory=list)
    alerts: List[Alert] = field(default_factory=list)
    executed: bool = True
    note: str = ""


@dataclass
class FacilityScore:
    facility: str
    visited: bool
    visit_round: Optional[int]
    score: int
    detail: Dict[str, int] = field(default_factory=dict)


@dataclass
class Report:
    rounds: List[RoundRecord]
    alerts: List[Alert]
    facility_scores: List[FacilityScore]
    unvisited: List[str]
    overall_score: int
    total_stamina_spent: int
    total_supply_spent: int
    trajectory: List[str]
