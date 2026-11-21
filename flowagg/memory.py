from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .errors import fail
from .plan import AggregationPlan
from .state import GroupState, READ_BUFFER_OVERHEAD_BYTES


@dataclass(slots=True)
class MemoryStats:
    active_groups: int = 0
    active_bytes: int = 0
    max_active_groups: int = 0
    max_active_bytes: int = 0
    readback_count: int = 0
    readback_files: int = 0
    readback_bytes: int = 0
    deferred_readbacks: int = 0
    max_read_buffer_bytes: int = 0
    defer_reasons: dict[str, int] = field(default_factory=dict)

    def observe_active(self, count: int, used_bytes: int) -> None:
        self.active_groups = count
        self.active_bytes = used_bytes
        self.max_active_groups = max(self.max_active_groups, count)
        self.max_active_bytes = max(self.max_active_bytes, used_bytes)

    def defer(self, reason: str) -> None:
        self.deferred_readbacks += 1
        self.defer_reasons[reason] = self.defer_reasons.get(reason, 0) + 1

    def to_dict(self) -> dict[str, int | dict[str, int]]:
        return {
            "active_groups": self.active_groups,
            "active_bytes": self.active_bytes,
            "defer_reasons": dict(sorted(self.defer_reasons.items())),
            "deferred_readbacks": self.deferred_readbacks,
            "max_active_bytes": self.max_active_bytes,
            "max_active_groups": self.max_active_groups,
            "max_read_buffer_bytes": self.max_read_buffer_bytes,
            "readback_bytes": self.readback_bytes,
            "readback_count": self.readback_count,
            "readback_files": self.readback_files,
        }


@dataclass(slots=True)
class ActiveGroupTable:
    plan: AggregationPlan
    spill_callback: Callable[[GroupState], None]
    stats: MemoryStats = field(default_factory=MemoryStats)
    groups: dict[str, GroupState] = field(default_factory=dict)
    used_bytes: int = 0
    tick: int = 0

    def _check_single_group(self, accounted: int) -> None:
        if accounted > self.plan.memory_budget_bytes:
            raise fail(
                "E_SINGLE_GROUP_TOO_LARGE",
                "one group state exceeds the total memory budget",
                group_state_bytes=accounted,
                memory_budget_bytes=self.plan.memory_budget_bytes,
            )
        if accounted > self.plan.active_budget_bytes:
            raise fail(
                "E_MEMORY_BUDGET",
                "one group state exceeds the active-table slice of the budget",
                group_state_bytes=accounted,
                active_budget_bytes=self.plan.active_budget_bytes,
                read_buffer_bytes=self.plan.read_buffer_bytes,
            )

    def _evict_until(self, required_bytes: int, protected: str | None = None) -> None:
        while self.used_bytes + required_bytes > self.plan.active_budget_bytes:
            candidates = [
                group for group_id, group in self.groups.items() if group_id != protected
            ]
            if not candidates:
                raise fail(
                    "E_MEMORY_BUDGET",
                    "active group table cannot satisfy the hard memory budget",
                    required_bytes=required_bytes,
                    used_bytes=self.used_bytes,
                    active_budget_bytes=self.plan.active_budget_bytes,
                )
            victim = min(candidates, key=lambda group: (group.last_used, group.group_id))
            self.evict(victim.group_id)

    def next_tick(self) -> int:
        self.tick += 1
        return self.tick

    def get_or_create(self, spec: GroupState | None = None) -> GroupState:
        raise AssertionError("use insert")

    def insert(self, group: GroupState) -> GroupState:
        accounted = group.accounted_bytes
        self._check_single_group(accounted)
        existing = self.groups.get(group.group_id)
        if existing is not None:
            existing.last_used = self.next_tick()
            return existing
        self._evict_until(accounted)
        group.last_used = self.next_tick()
        self.groups[group.group_id] = group
        self.used_bytes += accounted
        self.stats.observe_active(len(self.groups), self.used_bytes)
        return group

    def touch(self, group_id: str) -> GroupState:
        group = self.groups[group_id]
        group.last_used = self.next_tick()
        return group

    def evict(self, group_id: str) -> None:
        group = self.groups[group_id]
        self.spill_callback(group)
        removed = self.groups.pop(group_id)
        self.used_bytes -= removed.accounted_bytes
        self.stats.observe_active(len(self.groups), self.used_bytes)

    def clear(self) -> None:
        for group_id in list(self.groups):
            self.evict(group_id)

    def evict_all_without_spill(self) -> None:
        self.groups.clear()
        self.used_bytes = 0
        self.stats.observe_active(0, 0)

    def buffer_available(self, file_size: int) -> bool:
        required = file_size + READ_BUFFER_OVERHEAD_BYTES
        if required > self.plan.read_buffer_bytes:
            self.stats.defer("READ_BUFFER_TOO_SMALL")
            return False
        self.stats.max_read_buffer_bytes = max(
            self.stats.max_read_buffer_bytes, required
        )
        return True

    def account_read(self, file_size: int) -> None:
        self.stats.readback_count += 1
        self.stats.readback_files += 1
        self.stats.readback_bytes += file_size
        self.stats.max_read_buffer_bytes = max(
            self.stats.max_read_buffer_bytes,
            file_size + READ_BUFFER_OVERHEAD_BYTES,
        )
        if self.used_bytes + self.plan.read_buffer_bytes > self.plan.memory_budget_bytes:
            raise fail(
                "E_MEMORY_BUDGET",
                "active table and read buffer would oversell the total budget",
            )

    def assert_budget(self) -> None:
        if self.used_bytes > self.plan.active_budget_bytes:
            raise fail(
                "E_MEMORY_BUDGET",
                "active table exceeds its budget slice",
                used_bytes=self.used_bytes,
                active_budget_bytes=self.plan.active_budget_bytes,
            )

