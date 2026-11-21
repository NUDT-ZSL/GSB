"""Interactive query interface with a latency budget.

Latency is *observed* by timing the real local query loop with an
injectable clock, and *estimated* from the index entry count and mask
size via a documented linear model.  Exceeding the configured budget
aborts the query with E_LATENCY_BUDGET, reporting the completed stages
and the number of index entries already inspected.
"""
from __future__ import annotations

import dataclasses
import time

from .config import WorkbenchConfig
from .errors import E_LATENCY_BUDGET, PanoError
from .index import QueryIndex

# estimation model: est_us = ENTRY_COST_US * entries + PIXEL_COST_US * pixels
ENTRY_COST_US = 0.8
PIXEL_COST_US = 0.001

STAGES = ("locate", "fetch_instance", "metric_contribution", "assemble")


@dataclasses.dataclass
class QueryResult:
    kind: str                  # "pixel" | "instance"
    category_id: int
    instance_id: int
    confidence: float
    metric_contribution: dict
    latency_observed_us: float
    latency_estimate_us: float
    budget_ms: float
    stages_completed: tuple
    entries_checked: int
    snapshot_version: int

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def estimate_cost_us(num_entries: int, mask_size: int) -> float:
    return ENTRY_COST_US * num_entries + PIXEL_COST_US * mask_size


class QueryEngine:
    def __init__(self, index: QueryIndex, cfg: WorkbenchConfig, clock=time.perf_counter):
        self.index = index
        self.cfg = cfg
        self.clock = clock

    # ------------------------------------------------------------ internals
    def _elapsed_us(self, t0: float) -> float:
        return (self.clock() - t0) * 1e6

    def _guard(self, t0, stages_done, entries_checked):
        elapsed = self._elapsed_us(t0)
        if elapsed > self.cfg.latency_budget_ms * 1000.0:
            raise PanoError(
                E_LATENCY_BUDGET, "query exceeded latency budget",
                budget_ms=self.cfg.latency_budget_ms,
                elapsed_us=round(elapsed, 3),
                stages_completed=list(stages_done),
                entries_checked=entries_checked,
            )

    def _estimate(self) -> float:
        return estimate_cost_us(self.index.num_entries, self.index.mask_size)

    # -------------------------------------------------------------- queries
    def query_pixel(self, x: int, y: int) -> QueryResult:
        t0 = self.clock()
        stages, checked = [], 0
        mask = self.index.mask

        if not (0 <= x < mask.width and 0 <= y < mask.height):
            raise PanoError(
                "E_OUT_OF_RANGE", "pixel outside image bounds", x=x, y=y,
            )
        cid = int(mask.class_map[y, x])
        iid = int(mask.instance_map[y, x])
        scored = bool(self.index.valid_mask[y, x])
        stages.append("locate")
        self._guard(t0, stages, checked)

        entry = self.index.entry(iid) if iid else None
        checked += 1 if iid else 0
        conf = float(mask.conf_map[y, x])
        stages.append("fetch_instance")
        self._guard(t0, stages, checked)

        contribution = self._pixel_contribution(x, y, cid, iid, scored, entry)
        stages.append("metric_contribution")
        self._guard(t0, stages, checked)

        result = QueryResult(
            kind="pixel", category_id=cid, instance_id=iid,
            confidence=conf,
            metric_contribution=contribution,
            latency_observed_us=self._elapsed_us(t0),
            latency_estimate_us=self._estimate(),
            budget_ms=self.cfg.latency_budget_ms,
            stages_completed=tuple(stages + ["assemble"]),
            entries_checked=checked,
            snapshot_version=self.index.snapshot_version,
        )
        self._guard(t0, result.stages_completed, checked)
        return result

    def query_instance(self, instance_id: int) -> QueryResult:
        t0 = self.clock()
        stages, checked = [], 0

        entry = None
        for cand in self.index.entries:          # linear scan = counted work
            checked += 1
            if cand.instance_id == instance_id:
                entry = cand
                break
        stages.append("locate")
        self._guard(t0, stages, checked)
        if entry is None:
            raise PanoError(
                "E_NO_SUCH_INSTANCE", "instance id not present",
                instance_id=instance_id,
            )

        stages.append("fetch_instance")
        self._guard(t0, stages, checked)

        contribution = {
            "status": entry.metric_status,
            "matched_gt": entry.matched_gt,
            "iou": entry.metric_iou,
            "pixel_count": entry.pixel_count,
            "scored": True,
        }
        stages.append("metric_contribution")
        self._guard(t0, stages, checked)

        result = QueryResult(
            kind="instance", category_id=entry.category_id,
            instance_id=entry.instance_id, confidence=entry.conf_mean,
            metric_contribution=contribution,
            latency_observed_us=self._elapsed_us(t0),
            latency_estimate_us=self._estimate(),
            budget_ms=self.cfg.latency_budget_ms,
            stages_completed=tuple(stages + ["assemble"]),
            entries_checked=checked,
            snapshot_version=self.index.snapshot_version,
        )
        self._guard(t0, result.stages_completed, checked)
        return result

    # --------------------------------------------------------- contribution
    def _pixel_contribution(self, x, y, cid, iid, scored, entry) -> dict:
        if not scored:
            return {
                "scored": False,
                "reason": "ignore_region",
                "status": "excluded",
            }
        gt_cls = int(self.index.gt_class_map[y, x])
        contribution = {
            "scored": True,
            "gt_category": gt_cls,
            "in_intersection": bool(cid == gt_cls),
            "status": entry.metric_status if entry else "background",
            "matched_gt": entry.matched_gt if entry else 0,
            "iou": entry.metric_iou if entry else 0.0,
        }
        return contribution
