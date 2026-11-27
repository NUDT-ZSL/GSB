"""图结构与基础消耗函数：stamina(d)=d，supply(d)=(d+1)//2（向上取整）。"""
from __future__ import annotations

import heapq
from typing import Dict, List, Optional, Tuple

from .model import Topology

UNREACHABLE: Optional[int] = None


def base_stamina_cost(distance: int) -> int:
    return distance


def base_supply_cost(distance: int) -> int:
    return (distance + 1) // 2


class Graph:
    """由 Topology 构建的有向图，提供任意两点最短距离与路径。"""

    def __init__(self, topo: Topology):
        self.adj: Dict[str, List[Tuple[str, int]]] = {n: [] for n in topo.nodes}
        for e in topo.edges:
            self.adj[e.a].append((e.b, e.distance))
            if e.bidirectional:
                self.adj[e.b].append((e.a, e.distance))
        for n in self.adj:
            # 排序保证同距离时选字典序最小的下一跳，结果确定
            self.adj[n].sort(key=lambda t: (t[1], t[0]))

    def shortest(self, src: str) -> Dict[str, int]:
        """Dijkstra：返回 src 到各节点最短距离；不可达节点不出现在结果中。"""
        dist: Dict[str, int] = {src: 0}
        pq: List[Tuple[int, str]] = [(0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v, w in self.adj.get(u, []):
                nd = d + w
                if v not in dist or nd < dist[v]:
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return dist

    def distance(self, src: str, dst: str) -> Optional[int]:
        """两点间最短距离；不可达返回 None（显式标记，不给虚假数值）。"""
        if src == dst:
            return 0
        return self.shortest(src).get(dst, UNREACHABLE)

    def path(self, src: str, dst: str) -> Optional[List[str]]:
        """最短路径节点序列（含端点）；不可达返回 None。"""
        if src == dst:
            return [src]
        dist: Dict[str, int] = {src: 0}
        prev: Dict[str, str] = {}
        pq: List[Tuple[int, str]] = [(0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            if u == dst:
                break
            for v, w in self.adj.get(u, []):
                nd = d + w
                if v not in dist or nd < dist[v]:
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if dst not in dist:
            return None
        nodes = [dst]
        while nodes[-1] != src:
            nodes.append(prev[nodes[-1]])
        nodes.reverse()
        return nodes

    def route_segments(self, waypoints: List[str]) -> Optional[List[Tuple[str, str, int]]]:
        """多途经点路线逐段展开为 (frm, to, distance)；任一段不可达返回 None。"""
        segments: List[Tuple[str, str, int]] = []
        for i in range(len(waypoints) - 1):
            d = self.distance(waypoints[i], waypoints[i + 1])
            if d is None:
                return None
            segments.append((waypoints[i], waypoints[i + 1], d))
        return segments
