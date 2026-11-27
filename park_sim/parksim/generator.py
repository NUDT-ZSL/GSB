"""确定性样本生成器：按固定种子产出小型乐园拓扑与指令序列。

same seed -> same scenario（random.Random 局部实例，不碰全局状态）。
内置场景 "overflow" 故意造成补给/体力越界，用于验证告警与拒绝政策。
"""
from __future__ import annotations

import random
from typing import List

from .model import (Command, CrowdFactor, Edge, Node, Scenario, Topology,
                    WeatherEvent)

WEATHER_NAMES = ["烈日", "暴雨", "大风", "寒潮", "闷热"]


def generate(seed: int = 42, scenario: str = "normal") -> Scenario:
    rng = random.Random(seed)
    topo = Topology()

    n_fac, n_sup, n_rest = 4, 2, 1
    facilities = [f"facility_{i}" for i in range(n_fac)]
    supplies = [f"supply_{i}" for i in range(n_sup)]
    rests = [f"rest_{i}" for i in range(n_rest)]
    for name in facilities:
        topo.nodes[name] = Node(name, "facility")
    for name in supplies:
        topo.nodes[name] = Node(name, "supply")
    for name in rests:
        topo.nodes[name] = Node(name, "rest")
    topo.nodes["gate"] = Node("gate", "gate")

    names = list(topo.nodes)
    # 先造一棵随机树保证连通，再加随机额外边
    connected = ["gate"]
    pending = [n for n in names if n != "gate"]
    rng.shuffle(pending)
    edges = set()
    for n in pending:
        other = rng.choice(connected)
        d = rng.randint(2, 9)
        key = tuple(sorted((n, other)))
        if key not in edges:
            edges.add(key)
            topo.edges.append(Edge(key[0], key[1], d, bidirectional=True))
        connected.append(n)
    for _ in range(3):
        a, b = rng.sample(names, 2)
        key = tuple(sorted((a, b)))
        if key not in edges:
            edges.add(key)
            topo.edges.append(Edge(key[0], key[1], rng.randint(2, 9),
                                   bidirectional=rng.random() < 0.8))

    total_rounds = 10
    weather: List[WeatherEvent] = []
    for _ in range(3):
        weather.append(WeatherEvent(
            round_no=rng.randint(1, total_rounds),
            name=rng.choice(WEATHER_NAMES),
            stamina_factor=rng.choice([900, 1100, 1250, 1500]),
            supply_factor=rng.choice([1000, 1200, 1500])))
    crowd = [CrowdFactor(round_no=r,
                         stamina_factor=rng.choice([1000, 1050, 1200]),
                         supply_factor=rng.choice([1000, 1100]))
             for r in sorted(rng.sample(range(1, total_rounds + 1), 3))]

    targets = rng.sample(facilities, 3)
    init_stamina, init_supply, cap = 30, 20, 25

    if scenario == "overflow":
        # 故意越界：不给补给机会，强制长距离连续行军
        far = max(facilities,
                  key=lambda f: _dist_from_gate(topo, f))
        targets = [far] + [t for t in targets if t != far][:2]
        init_stamina, init_supply = 12, 6
        commands = [Command("MOVE", ("gate", targets[0]))] + \
                   [Command("MOVE", (targets[i], targets[i + 1]))
                    for i in range(len(targets) - 1)] + \
                   [Command("MOVE", (targets[-1], "gate"))]
    else:
        commands = _random_commands(rng, topo, targets, total_rounds)

    return Scenario(topology=topo, total_rounds=total_rounds,
                    start_node="gate", init_stamina=init_stamina,
                    init_supply=init_supply, supply_capacity=cap,
                    targets=targets, weather=weather, crowd=crowd,
                    commands=commands)


def _dist_from_gate(topo: Topology, name: str) -> int:
    from .graph import Graph
    d = Graph(topo).distance("gate", name)
    return d if d is not None else -1


def _random_commands(rng: random.Random, topo: Topology,
                     targets: List[str], total_rounds: int) -> List[Command]:
    """生成一条能走完目标、中途补给的合理指令序列，再补 WAIT 填满。"""
    cmds: List[Command] = []
    cur = "gate"
    supplies = [n for n, nd in topo.nodes.items() if nd.kind == "supply"]
    rests = [n for n, nd in topo.nodes.items() if nd.kind == "rest"]
    for t in targets:
        if supplies and rng.random() < 0.7:
            s = rng.choice(supplies)
            cmds.append(Command("MOVE", (cur, s)))
            cmds.append(Command("RESUPPLY", amount=rng.randint(5, 10)))
            cur = s
        if rests and rng.random() < 0.5:
            r0 = rng.choice(rests)
            cmds.append(Command("MOVE", (cur, r0)))
            cmds.append(Command("REST", amount=rng.randint(5, 10)))
            cur = r0
        cmds.append(Command("MOVE", (cur, t)))
        cur = t
    cmds.append(Command("MOVE", (cur, "gate")))
    while len(cmds) < total_rounds:
        cmds.append(Command("WAIT"))
    return cmds[:total_rounds]
