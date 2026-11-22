"""元数据缓存：定点占用下的逐出裁决与 decimal 高精度真值对照。

缓存条目数受上限约束，按确定性的（最近使用逻辑时钟, 占用, 键）次序淘汰。
同一批条目分别用受控定点与 decimal 高精度通路计算占用与淘汰候选序列，
两条通路逐项比对；舍入导致序列不同时标记首个分歧条目与中间值。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from typing import Dict, List, Optional

from .errors import E_CACHE_CAPACITY, StoreError
from .fixedpoint import to_fixed

getcontext().prec = 50

OCCUPANCY_UNIT_BYTES = 12  # 1 个占用单位 = 12 字节序列化元数据


def occupancy_raw(value) -> float:
    """元数据占用的原始浮点值（换算通路输入）。"""
    size = len(json.dumps(value, sort_keys=True).encode("utf-8"))
    return size / OCCUPANCY_UNIT_BYTES


def occupancy_decimal(value) -> Decimal:
    size = len(json.dumps(value, sort_keys=True).encode("utf-8"))
    return Decimal(size) / Decimal(OCCUPANCY_UNIT_BYTES)


@dataclass
class CacheEntry:
    key: str
    occupancy_fixed: int
    occupancy_dec: Decimal
    occupancy_raw: float
    last_clock: int
    version: int


@dataclass
class DivergenceReport:
    diverged: bool
    first_divergence_index: Optional[int] = None
    fixed_entry: Optional[str] = None
    decimal_entry: Optional[str] = None
    fixed_intermediate: Optional[dict] = None
    decimal_intermediate: Optional[dict] = None
    conversion_steps: List[dict] = field(default_factory=list)

    def to_dict(self):
        return {
            "diverged": self.diverged,
            "first_divergence_index": self.first_divergence_index,
            "fixed_entry": self.fixed_entry,
            "decimal_entry": self.decimal_entry,
            "fixed_intermediate": self.fixed_intermediate,
            "decimal_intermediate": self.decimal_intermediate,
            "conversion_steps": self.conversion_steps,
        }


class MetadataCache:
    def __init__(self, capacity: int, scale: int, batch_threshold: int = 128):
        if not isinstance(capacity, int) or capacity <= 0:
            raise StoreError(E_CACHE_CAPACITY,
                             "cache capacity must be a positive integer",
                             capacity=capacity)
        self.capacity = capacity
        self.scale = scale
        self.batch_threshold = batch_threshold
        self.entries: Dict[str, CacheEntry] = {}
        self.last_mode = "precise"

    def put(self, key: str, value, clock: int) -> List[str]:
        """写入/更新条目并按需逐出，返回被逐出的键序列。"""
        raw = occupancy_raw(value)
        rec = to_fixed(raw, self.scale, stage=f"cache_occupancy:{key}")
        version = self.entries[key].version + 1 if key in self.entries else 1
        self.entries[key] = CacheEntry(
            key=key, occupancy_fixed=rec.fixed,
            occupancy_dec=occupancy_decimal(value),
            occupancy_raw=raw, last_clock=clock, version=version)
        return self.evict_if_needed()

    def touch(self, key: str, clock: int):
        entry = self.entries.get(key)
        if entry is not None:
            entry.last_clock = clock

    def _fixed_order(self) -> List[CacheEntry]:
        return sorted(self.entries.values(),
                      key=lambda e: (e.last_clock, e.occupancy_fixed, e.key))

    def _decimal_order(self) -> List[CacheEntry]:
        return sorted(self.entries.values(),
                      key=lambda e: (e.last_clock, e.occupancy_dec, e.key))

    def dual_path_order(self):
        """返回 (定点候选序列, decimal 候选序列, 分歧报告, 模式)。"""
        n = len(self.entries)
        self.last_mode = "batch" if n > self.batch_threshold else "precise"
        fixed_seq = [e.key for e in self._fixed_order()]
        decimal_seq = [e.key for e in self._decimal_order()]
        report = DivergenceReport(diverged=False)
        for idx, (fkey, dkey) in enumerate(zip(fixed_seq, decimal_seq)):
            if fkey != dkey:
                fentry = self.entries[fkey]
                dentry = self.entries[dkey]
                report = DivergenceReport(
                    diverged=True,
                    first_divergence_index=idx,
                    fixed_entry=fkey,
                    decimal_entry=dkey,
                    fixed_intermediate={
                        "key": fkey, "occupancy_fixed": fentry.occupancy_fixed,
                        "occupancy_raw": fentry.occupancy_raw,
                        "last_clock": fentry.last_clock,
                    },
                    decimal_intermediate={
                        "key": dkey, "occupancy_decimal": str(dentry.occupancy_dec),
                        "occupancy_raw": dentry.occupancy_raw,
                        "last_clock": dentry.last_clock,
                    },
                    conversion_steps=[
                        to_fixed(fentry.occupancy_raw, self.scale,
                                 stage=f"cache_occupancy:{fkey}").to_dict(),
                        to_fixed(dentry.occupancy_raw, self.scale,
                                 stage=f"cache_occupancy:{dkey}").to_dict(),
                    ],
                )
                break
        return fixed_seq, decimal_seq, report, self.last_mode

    def evict_if_needed(self) -> List[str]:
        """按定点通路裁决逐出（裁决权威），返回逐出键序列。"""
        evicted: List[str] = []
        while len(self.entries) > self.capacity:
            victim = self._fixed_order()[0]
            del self.entries[victim.key]
            evicted.append(victim.key)
        return evicted
