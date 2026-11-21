from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .errors import fail
from .plan import MAX_I64, AggregationPlan
from .records import FlowRecord


GROUP_BASE_ESTIMATE_BYTES = 640
GROUP_DIMENSION_ESTIMATE_BYTES = 24
GROUP_SPILL_CHAIN_ITEM_BYTES = 8
GROUP_CHAIN_HASH_BYTES = 64
READ_BUFFER_OVERHEAD_BYTES = 256


@dataclass(slots=True)
class StateVector:
    count: int = 0
    bytes_sum: int = 0
    packets_sum: int = 0
    bytes_sq_sum: int = 0
    packets_sq_sum: int = 0
    first_clock_ms: int | None = None
    last_clock_ms: int | None = None
    first_record_seq: int | None = None
    last_record_seq: int | None = None

    def is_zero(self) -> bool:
        return self.count == 0

    def copy(self) -> "StateVector":
        return StateVector(
            count=self.count,
            bytes_sum=self.bytes_sum,
            packets_sum=self.packets_sum,
            bytes_sq_sum=self.bytes_sq_sum,
            packets_sq_sum=self.packets_sq_sum,
            first_clock_ms=self.first_clock_ms,
            last_clock_ms=self.last_clock_ms,
            first_record_seq=self.first_record_seq,
            last_record_seq=self.last_record_seq,
        )

    def add_record(self, record: FlowRecord) -> None:
        byte_square = record.bytes * record.bytes
        packet_square = record.packets * record.packets
        new_values = (
            self.count + 1,
            self.bytes_sum + record.bytes,
            self.packets_sum + record.packets,
            self.bytes_sq_sum + byte_square,
            self.packets_sq_sum + packet_square,
        )
        if any(value > MAX_I64 for value in new_values):
            raise fail(
                "E_OVERFLOW",
                "aggregate signed 64-bit overflow while updating group state",
                record_seq=record.record_seq,
                field="aggregate",
                policy="reject_batch_preserve_committed_state",
            )
        (
            self.count,
            self.bytes_sum,
            self.packets_sum,
            self.bytes_sq_sum,
            self.packets_sq_sum,
        ) = new_values
        if self.first_clock_ms is None:
            self.first_clock_ms = record.clock_ms
            self.first_record_seq = record.record_seq
        else:
            self.first_clock_ms = min(self.first_clock_ms, record.clock_ms)
            self.first_record_seq = min(self.first_record_seq or record.record_seq, record.record_seq)
        if self.last_clock_ms is None:
            self.last_clock_ms = record.clock_ms
            self.last_record_seq = record.record_seq
        else:
            self.last_clock_ms = max(self.last_clock_ms, record.clock_ms)
            self.last_record_seq = max(self.last_record_seq or record.record_seq, record.record_seq)

    def merge(self, other: "StateVector") -> None:
        merged = (
            self.count + other.count,
            self.bytes_sum + other.bytes_sum,
            self.packets_sum + other.packets_sum,
            self.bytes_sq_sum + other.bytes_sq_sum,
            self.packets_sq_sum + other.packets_sq_sum,
        )
        if any(value > MAX_I64 for value in merged):
            raise fail(
                "E_OVERFLOW",
                "signed 64-bit overflow while merging state vectors",
            )
        (
            self.count,
            self.bytes_sum,
            self.packets_sum,
            self.bytes_sq_sum,
            self.packets_sq_sum,
        ) = merged
        if other.first_clock_ms is not None:
            if self.first_clock_ms is None or other.first_clock_ms < self.first_clock_ms:
                self.first_clock_ms = other.first_clock_ms
            if self.first_record_seq is None or (
                other.first_record_seq is not None
                and other.first_record_seq < self.first_record_seq
            ):
                self.first_record_seq = other.first_record_seq
        if other.last_clock_ms is not None:
            if self.last_clock_ms is None or other.last_clock_ms > self.last_clock_ms:
                self.last_clock_ms = other.last_clock_ms
            if self.last_record_seq is None or (
                other.last_record_seq is not None
                and other.last_record_seq > self.last_record_seq
            ):
                self.last_record_seq = other.last_record_seq

    def subtract(self, other: "StateVector") -> "StateVector":
        values = {
            "count": self.count - other.count,
            "bytes_sum": self.bytes_sum - other.bytes_sum,
            "packets_sum": self.packets_sum - other.packets_sum,
            "bytes_sq_sum": self.bytes_sq_sum - other.bytes_sq_sum,
            "packets_sq_sum": self.packets_sq_sum - other.packets_sq_sum,
        }
        if any(value < 0 for value in values.values()):
            raise fail(
                "E_STATE_CORRUPT",
                "spill baseline exceeds current group state",
                fields={key: value for key, value in values.items() if value < 0},
            )
        return StateVector(
            **values,
            first_clock_ms=self.first_clock_ms,
            last_clock_ms=self.last_clock_ms,
            first_record_seq=self.first_record_seq,
            last_record_seq=self.last_record_seq,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bytes_sq_sum": self.bytes_sq_sum,
            "bytes_sum": self.bytes_sum,
            "count": self.count,
            "first_clock_ms": self.first_clock_ms,
            "first_record_seq": self.first_record_seq,
            "last_clock_ms": self.last_clock_ms,
            "last_record_seq": self.last_record_seq,
            "packets_sq_sum": self.packets_sq_sum,
            "packets_sum": self.packets_sum,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StateVector":
        required = {
            "bytes_sq_sum",
            "bytes_sum",
            "count",
            "first_clock_ms",
            "first_record_seq",
            "last_clock_ms",
            "last_record_seq",
            "packets_sq_sum",
            "packets_sum",
        }
        if not isinstance(data, dict) or set(data) != required:
            raise fail("E_STATE_CORRUPT", "state vector has an unexpected shape")
        for key in required:
            value = data[key]
            if value is not None and type(value) is not int:
                raise fail("E_STATE_CORRUPT", f"state vector field {key} is not an integer")
        if data["count"] < 0:
            raise fail("E_STATE_CORRUPT", "state vector count is negative")
        for key in ("bytes_sum", "packets_sum", "bytes_sq_sum", "packets_sq_sum"):
            if data[key] < 0 or data[key] > MAX_I64:
                raise fail("E_STATE_CORRUPT", f"state vector field {key} is out of range")
        return cls(**{key: data[key] for key in required})


@dataclass(slots=True)
class GroupState:
    group_id: str
    group_key_json: str
    values: tuple[Any, ...]
    state: StateVector
    state_version: int = 0
    base_state_version: int = 0
    spill_sum: StateVector = field(default_factory=StateVector)
    spill_chain: list[int] = field(default_factory=list)
    last_spill_sha256: str | None = None
    last_used: int = 0
    dirty: bool = False
    new_group: bool = False

    @property
    def accounted_bytes(self) -> int:
        return (
            GROUP_BASE_ESTIMATE_BYTES
            + GROUP_DIMENSION_ESTIMATE_BYTES * len(self.values)
            + len(self.group_key_json.encode("utf-8"))
            + GROUP_SPILL_CHAIN_ITEM_BYTES * len(self.spill_chain)
            + (GROUP_CHAIN_HASH_BYTES if self.last_spill_sha256 else 0)
        )

    @property
    def mean_bytes(self) -> float:
        return self.state.bytes_sum / self.state.count if self.state.count else 0.0

    @property
    def mean_packets(self) -> float:
        return self.state.packets_sum / self.state.count if self.state.count else 0.0

    def copy_for_memory(self) -> "GroupState":
        return GroupState(
            group_id=self.group_id,
            group_key_json=self.group_key_json,
            values=self.values,
            state=self.state.copy(),
            state_version=self.state_version,
            base_state_version=self.base_state_version,
            spill_sum=self.spill_sum.copy(),
            spill_chain=list(self.spill_chain),
            last_spill_sha256=self.last_spill_sha256,
            last_used=self.last_used,
            dirty=self.dirty,
            new_group=self.new_group,
        )

    def begin_dirty(self) -> None:
        if self.dirty:
            return
        self.base_state_version = self.state_version
        self.state_version += 1
        self.dirty = True

    def pending_delta(self) -> StateVector:
        return self.state.subtract(self.spill_sum)

    def mark_spilled(self, delta: StateVector, spill_seq: int, sha256: str) -> None:
        merged = self.spill_sum.copy()
        merged.merge(delta)
        if merged.to_dict() != self.state.to_dict():
            raise fail(
                "E_STATE_CORRUPT",
                "spill delta did not reconstruct the active group state",
                group_key=self.group_key_json,
            )
        self.spill_sum = self.state.copy()
        self.spill_chain.append(spill_seq)
        self.last_spill_sha256 = sha256

    def to_public(self) -> dict[str, Any]:
        return {
            "group_key": list(self.values),
            "group_key_json": self.group_key_json,
            "mean_bytes": self.mean_bytes,
            "mean_packets": self.mean_packets,
            "spill_chain": list(self.spill_chain),
            "state_version": self.state_version,
            "totals": self.state.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class GroupSpec:
    group_id: str
    group_key_json: str
    values: tuple[Any, ...]

    @classmethod
    def build(
        cls,
        plan: AggregationPlan,
        src: str,
        dst: str,
        protocol: str,
        direction: str,
    ) -> "GroupSpec":
        values = plan.group_key(src, dst, protocol, direction)
        key_json = plan.canonical_group_key(values)
        group_id = hashlib.sha256(
            (plan.plan_id + "\x1f" + key_json).encode("utf-8")
        ).hexdigest()
        return cls(group_id=group_id, group_key_json=key_json, values=values)


def group_public_view(plan: AggregationPlan, group: GroupState, quantiles: dict[str, Any]) -> dict[str, Any]:
    return {
        "dimensions": plan.dimension_map(group.values),
        "display_key": plan.display_group_key(group.values),
        "group_key": list(group.values),
        "group_key_json": group.group_key_json,
        "group_id": group.group_id,
        "mean_bytes": group.mean_bytes,
        "mean_packets": group.mean_packets,
        "quantiles": quantiles,
        "spill_chain": list(group.spill_chain),
        "state": group.state.to_dict(),
        "state_version": group.state_version,
    }

