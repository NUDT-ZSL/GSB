"""候选簇构建与跨服务依赖合并（并查集），合并过程全程留痕。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Event, MergeRecord
from .registry import DependencyRegistry


@dataclass
class Candidate:
    candidate_id: str
    service: str
    events: list[Event] = field(default_factory=list)

    @property
    def start(self) -> int:
        return min(event.ts_ms for event in self.events)

    @property
    def end(self) -> int:
        return max(event.ts_ms for event in self.events)


class _DSU:
    def __init__(self, items):
        self.parent = {item: item for item in items}

    def find(self, item):
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            nxt = self.parent[item]
            self.parent[item] = root
            item = nxt
        return root

    def union(self, left, right) -> bool:
        root_left, root_right = self.find(left), self.find(right)
        if root_left == root_right:
            return False
        if root_left > root_right:
            root_left, root_right = root_right, root_left
        self.parent[root_right] = root_left
        return True


def build_candidates(events: list[Event], proximity_window_ms: int) -> list[Candidate]:
    """同服务邻近窗口内事件归入同一候选簇；recovery 优先回挂到同码未闭环候选簇。"""
    core = [
        event
        for event in events
        if event.source in ("alert", "probe", "release")
    ]
    core.sort(key=lambda item: (item.ts_ms, item.event_id))

    by_service: dict[str, list[Event]] = {}
    for event in core:
        by_service.setdefault(event.service, []).append(event)

    candidates: list[Candidate] = []
    for service in sorted(by_service):
        service_candidates: list[Candidate] = []
        open_by_code: dict[str, Candidate] = {}
        closed_candidate_ids: set[int] = set()
        for event in by_service[service]:
            candidate = _assign_event(
                event, service_candidates, open_by_code, closed_candidate_ids, proximity_window_ms
            )
            if event.status != "recovery" and event.source in ("alert", "probe"):
                open_by_code.setdefault(event.code, candidate)
            elif event.status == "recovery":
                open_by_code.pop(event.code, None)
        for index, candidate in enumerate(service_candidates, start=1):
            candidate.candidate_id = f"C-{service}-{index:02d}"
            candidate.events.sort(key=lambda item: (item.ts_ms, item.event_id))
        candidates.extend(service_candidates)
    candidates.sort(key=lambda item: (item.start, item.candidate_id))
    return candidates


def _assign_event(event, service_candidates, open_by_code, closed_candidate_ids,
                  proximity_window_ms) -> Candidate:
    if event.status == "recovery":
        rebound = open_by_code.pop(event.code, None)
        if rebound is not None:
            rebound.events.append(event)
            closed_candidate_ids.add(id(rebound))
            return rebound
        if (
            service_candidates
            and id(service_candidates[-1]) not in closed_candidate_ids
            and event.ts_ms - service_candidates[-1].end <= proximity_window_ms
        ):
            target = service_candidates[-1]
            target.events.append(event)
            closed_candidate_ids.add(id(target))
            return target
        candidate = Candidate(candidate_id="", service=event.service, events=[event])
        service_candidates.append(candidate)
        closed_candidate_ids.add(id(candidate))
        return candidate
    active = open_by_code.get(event.code)
    if (
        active is not None
        and id(active) not in closed_candidate_ids
        and event.ts_ms - active.end <= proximity_window_ms
    ):
        active.events.append(event)
        return active
    if service_candidates:
        latest = service_candidates[-1]
        if id(latest) not in closed_candidate_ids and event.ts_ms - latest.end <= proximity_window_ms:
            latest.events.append(event)
            return latest
    candidate = Candidate(candidate_id="", service=event.service, events=[event])
    service_candidates.append(candidate)
    return candidate


def merge_candidates(
    candidates: list[Candidate],
    registry: DependencyRegistry,
    merge_window_ms: int,
) -> tuple[dict[str, list[Candidate]], list[MergeRecord]]:
    """候选簇之间存在依赖关联且最近事件时间差不超过窗口则合并。"""
    dsu = _DSU([candidate.candidate_id for candidate in candidates])
    index = {candidate.candidate_id: candidate for candidate in candidates}
    records: list[MergeRecord] = []
    step = 0

    def attempt_pair(left: Candidate, right: Candidate) -> None:
        nonlocal step
        if dsu.find(left.candidate_id) == dsu.find(right.candidate_id):
            return
        gap = _nearest_event_gap(left, right)
        if gap > merge_window_ms:
            return
        if _range_gap(left, right) > merge_window_ms:
            return
        path = _dependency_path(left.service, right.service, registry)
        if not path:
            return
        step += 1
        root_left = dsu.find(left.candidate_id)
        root_right = dsu.find(right.candidate_id)
        edge = "->".join(path)
        records.append(
            MergeRecord(
                step=step,
                left_candidate=root_left,
                right_candidate=root_right,
                basis_service=f"{left.service}|{right.service}",
                basis_edge=edge,
                min_gap_ms=gap,
                window_ms=merge_window_ms,
                reason=(
                    f"两候选簇最近事件时间差 {gap}ms <= 合并窗口 {merge_window_ms}ms，"
                    f"且服务 {left.service} 与 {right.service} 存在依赖链 {edge}"
                ),
            )
        )
        dsu.union(left.candidate_id, right.candidate_id)

    for i, left in enumerate(candidates):
        for right in candidates[i + 1 :]:
            attempt_pair(left, right)

    groups: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        groups.setdefault(dsu.find(candidate.candidate_id), []).append(candidate)
    ordered_groups = {}
    for root in sorted(groups, key=lambda ident: (index[ident].start, ident)):
        members = sorted(groups[root], key=lambda item: (item.start, item.candidate_id))
        ordered_groups[root] = members
    return ordered_groups, records


def _nearest_event_gap(left: Candidate, right: Candidate) -> int:
    """跨两个候选簇的相邻事件最小时间差（要求是真正跨簇的事件对）。"""
    left_ts = sorted(event.ts_ms for event in left.events)
    right_ts = sorted(event.ts_ms for event in right.events)
    gap = None
    i = j = 0
    while i < len(left_ts) and j < len(right_ts):
        current = abs(left_ts[i] - right_ts[j])
        gap = current if gap is None else min(gap, current)
        if left_ts[i] < right_ts[j]:
            i += 1
        else:
            j += 1
    return gap if gap is not None else 0


def _range_gap(left: Candidate, right: Candidate) -> int:
    """两个候选簇整体范围之间的空闲间隔；范围相交/相接时为 0。"""
    return max(0, max(left.start, right.start) - min(left.end, right.end))


def _dependency_path(service_a: str, service_b: str, registry: DependencyRegistry) -> list[str]:
    if service_a == service_b:
        return []
    path = registry.path_between(service_a, service_b)
    if path:
        return path
    reverse = registry.path_between(service_b, service_a)
    return list(reversed(reverse)) if reverse else []
