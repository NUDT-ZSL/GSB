"""核心数据模型与确定性序列化工具。"""

import hashlib
import json
from dataclasses import dataclass, field

SEVERITIES = ("info", "notice", "warning", "major", "critical")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}

EVENT_FLAGS = ("none", "silenced", "maintenance")
WINDOW_KINDS = ("silence", "maintenance")

WEIGHT_SCALE = 1_000_000


def weight_to_micro(weight):
    return int(round(float(weight) * WEIGHT_SCALE))


def canonical_json(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_obj(obj):
    return sha256_text(canonical_json(obj))


@dataclass(frozen=True)
class Instance:
    instance_id: str
    service: str
    weight_micro: int


@dataclass(frozen=True)
class Service:
    name: str
    fault_domain: str
    upstream: tuple
    instances: tuple


@dataclass(frozen=True)
class Event:
    seq: int
    instance_id: str
    service: str
    rule: str
    severity: str
    ts_ms: int
    metric: float
    flag: str


@dataclass(frozen=True)
class Window:
    window_id: str
    kind: str
    service: str
    instance_id: str
    start_ms: int
    end_ms: int

    def covers(self, event):
        if self.service and self.service != event.service:
            return False
        if self.instance_id and self.instance_id != event.instance_id:
            return False
        return self.start_ms <= event.ts_ms <= self.end_ms


@dataclass
class Topology:
    version: int
    services: dict = field(default_factory=dict)
    instances: dict = field(default_factory=dict)
    windows: dict = field(default_factory=dict)

    def upstream_map(self):
        return {name: set(svc.upstream) for name, svc in self.services.items()}

    def downstream_map(self):
        down = {name: set() for name in self.services}
        for name, svc in self.services.items():
            for up in svc.upstream:
                down[up].add(name)
        return down

    def upstream_closure(self, service):
        """返回 {服务名: 因果距离}, 含自身(距离0)。沿上游依赖边 BFS。"""
        up = self.upstream_map()
        dist = {service: 0}
        frontier = [service]
        while frontier:
            nxt = []
            for node in frontier:
                for parent in sorted(up[node]):
                    if parent not in dist:
                        dist[parent] = dist[node] + 1
                        nxt.append(parent)
            frontier = nxt
        return dist

    def downstream_closure(self, service):
        """返回 {服务名: 波及层级}, 含自身(层级0)。沿下游(被依赖方向) BFS。"""
        down = self.downstream_map()
        dist = {service: 0}
        frontier = [service]
        while frontier:
            nxt = []
            for node in frontier:
                for child in sorted(down[node]):
                    if child not in dist:
                        dist[child] = dist[node] + 1
                        nxt.append(child)
            frontier = nxt
        return dist

    def dependency_path(self, from_service, to_service):
        """from_service 的失效传导到 to_service 的最短依赖路径(含两端)。"""
        if from_service == to_service:
            return [from_service]
        down = self.downstream_map()
        prev = {from_service: None}
        frontier = [from_service]
        while frontier:
            nxt = []
            for node in frontier:
                for child in sorted(down[node]):
                    if child not in prev:
                        prev[child] = node
                        if child == to_service:
                            path = [to_service]
                            cur = to_service
                            while prev[cur] is not None:
                                cur = prev[cur]
                                path.append(cur)
                            return list(reversed(path))
                        nxt.append(child)
            frontier = nxt
        return []
