"""统一数据模型与稳定 JSON 序列化。

稳定输出约定：json.dumps(sort_keys=True, separators=(",", ":"))，
保证相同输入在任意运行次数下逐位一致。
"""

import json
from dataclasses import dataclass, field


def stable_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass
class Action:
    """统一动作：保留来源版本、原始时间表示。"""

    action_id: str
    kind: str                      # stop | start | switch
    at_tick: int                   # 原始时间表示（v1 逻辑刻 / v2 绝对毫秒）
    script: str = ""
    source_version: str = "v1"     # v1 | v2
    priority: str = ""             # v2 动作优先级标签
    seq: int = 0                   # 服务内步骤序号（水位比较依据）

    def to_dict(self):
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "at_tick": self.at_tick,
            "script": self.script,
            "source_version": self.source_version,
            "priority": self.priority,
            "seq": self.seq,
        }


@dataclass
class Service:
    """统一服务模型。"""

    name: str
    failure_domain: str = ""
    actions: list = field(default_factory=list)       # list[Action]
    upstreams: list = field(default_factory=list)     # list[str] 依赖上游
    tombstoned: bool = False
    source_version: str = "v1"
    # v1 原始字段
    fallback_seconds: int = 0                          # 原始相对回退秒数
    # v2 原始字段
    effective_ms: int = 0                              # 原始绝对生效时刻
    generation: int = 0                                # 恢复代数
    # 时间迁移结果（由 migrate 填充）
    fallback_abs_ms: int = 0                           # 绝对回退时刻
    time_source: str = ""                              # migrated-v1 | absolute-v2
    # 回切流量阶梯（百分比）
    ladder: list = field(default_factory=list)

    def to_dict(self):
        return {
            "name": self.name,
            "failure_domain": self.failure_domain,
            "actions": [a.to_dict() for a in self.actions],
            "upstreams": sorted(self.upstreams),
            "tombstoned": self.tombstoned,
            "source_version": self.source_version,
            "fallback_seconds": self.fallback_seconds,
            "effective_ms": self.effective_ms,
            "generation": self.generation,
            "fallback_abs_ms": self.fallback_abs_ms,
            "time_source": self.time_source,
            "ladder": list(self.ladder),
        }


@dataclass
class Plan:
    """统一恢复计划：服务表 + 裁决依据。"""""

    services: dict = field(default_factory=dict)      # name -> Service
    rationale: list = field(default_factory=list)     # 合并裁决依据

    def topo_order(self):
        """按依赖拓扑序返回服务名（上游在前）；成环抛 E_CYCLE。"""
        from .errors import BizError, ErrCode

        indeg = {n: 0 for n in self.services}
        dependents = {n: [] for n in self.services}
        for name, svc in self.services.items():
            for up in svc.upstreams:
                if up in self.services:
                    indeg[name] += 1
                    dependents[up].append(name)
        queue = sorted(n for n, d in indeg.items() if d == 0)
        order = []
        while queue:
            node = queue.pop(0)
            order.append(node)
            for nxt in sorted(dependents[node]):
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    queue.append(nxt)
            queue.sort()
        if len(order) != len(self.services):
            remaining = sorted(n for n, d in indeg.items() if d > 0)
            raise BizError(ErrCode.E_CYCLE, "依赖图存在环", remaining=remaining)
        return order

    def dependents_closure(self, seeds):
        """沿依赖边（下游方向）计算受影响闭包，含种子本身。"""
        dependents = {n: [] for n in self.services}
        for name, svc in self.services.items():
            for up in svc.upstreams:
                if up in self.services:
                    dependents[up].append(name)
        seen = set()
        stack = list(seeds)
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(dependents.get(node, []))
        return seen

    def to_dict(self):
        return {
            "services": {n: self.services[n].to_dict() for n in sorted(self.services)},
            "rationale": sorted(self.rationale, key=lambda r: (r["service"], r["decision"])),
        }

    def to_stable_json(self):
        return stable_json(self.to_dict())
