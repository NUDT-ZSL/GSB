from __future__ import annotations

from dataclasses import dataclass, field

from .canonical import stable_hash64


@dataclass
class _Node:
    key: int
    values: list[int] = field(default_factory=list)


class OpenAddressingMultiMap:
    def __init__(self, domain: str, capacity: int, probe_limit: int = 8) -> None:
        self.domain = domain
        self.capacity = max(1, capacity)
        self.probe_limit = max(1, probe_limit)
        self.slots: list[_Node | None] = [None] * self.capacity
        self.overflow: list[_Node] = []
        self.build_probe_count = 0
        self.lookup_probe_count = 0
        self.overflow_access_count = 0
        self.max_probe_length = 0

    def _home(self, key: int) -> int:
        return stable_hash64(self.domain, key) & (self.capacity - 1)

    def insert(self, key: int, value: int) -> None:
        home = self._home(key)
        for distance in range(self.probe_limit):
            self.build_probe_count += 1
            self.max_probe_length = max(self.max_probe_length, distance + 1)
            index = (home + distance) & (self.capacity - 1)
            node = self.slots[index]
            if node is None:
                self.slots[index] = _Node(key=key, values=[value])
                return
            if node.key == key:
                node.values.append(value)
                return
        for node in self.overflow:
            self.overflow_access_count += 1
            if node.key == key:
                node.values.append(value)
                return
        self.overflow.append(_Node(key=key, values=[value]))

    def lookup(self, key: int) -> tuple[list[int], int, int]:
        home = self._home(key)
        visits = 0
        for distance in range(self.probe_limit):
            self.lookup_probe_count += 1
            visits += 1
            self.max_probe_length = max(self.max_probe_length, distance + 1)
            index = (home + distance) & (self.capacity - 1)
            node = self.slots[index]
            if node is None:
                return [], visits, 0
            if node.key == key:
                return list(node.values), visits, 0
        overflow_visits = 0
        for node in self.overflow:
            self.overflow_access_count += 1
            overflow_visits += 1
            if node.key == key:
                return list(node.values), visits, overflow_visits
        return [], visits, overflow_visits

    @property
    def occupied_buckets(self) -> int:
        return sum(1 for node in self.slots if node is not None)

    @property
    def entry_count(self) -> int:
        count = sum(len(node.values) for node in self.slots if node is not None)
        count += sum(len(node.values) for node in self.overflow)
        return count


def next_power_of_two(value: int) -> int:
    if value <= 1:
        return 1
    return 1 << (value - 1).bit_length()


def capacity_for_rows(row_count: int) -> int:
    desired = max(4, row_count * 2)
    return next_power_of_two(desired)
