"""Pre-built query index over the merged panoptic mask.

The index is the *only* state the interactive query path is allowed to
read: every piece of metadata it needs (config fingerprint, mapping
version, snapshot version, metric contribution per instance) is embedded
at build time, so queries never depend on implicit batch-pipeline state.

``canonical_bytes`` is deterministic: same mask + same report + same
config produce byte-identical output.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json

import numpy as np

from .config import WorkbenchConfig
from .merge import PanopticMask
from .metrics import MetricsReport


@dataclasses.dataclass(frozen=True)
class InstanceEntry:
    instance_id: int
    category_id: int
    pixel_count: int
    bbox: tuple            # (min_x, min_y, max_x, max_y) inclusive
    conf_mean: float
    conf_min: float
    conf_max: float
    metric_status: str     # "TP" | "FP"
    metric_iou: float      # IoU with matched ground-truth instance (0 if FP)
    matched_gt: int        # gt instance id, 0 when unmatched

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "category_id": self.category_id,
            "pixel_count": self.pixel_count,
            "bbox": list(self.bbox),
            "conf_mean": self.conf_mean,
            "conf_min": self.conf_min,
            "conf_max": self.conf_max,
            "metric_status": self.metric_status,
            "metric_iou": self.metric_iou,
            "matched_gt": self.matched_gt,
        }


class QueryIndex:
    def __init__(self, mask: PanopticMask, report: MetricsReport,
                 cfg: WorkbenchConfig, snapshot_version: int = 0):
        self.mask = mask
        self.config_fingerprint = cfg.fingerprint()
        self.mapping_version = cfg.mapping_version
        self.snapshot_version = snapshot_version
        self.iou_threshold = cfg.iou_threshold
        self.precision = cfg.precision
        self.entries: tuple[InstanceEntry, ...] = self._build_entries(mask, report)
        self._by_id = {e.instance_id: e for e in self.entries}
        self._match_lookup = self._build_match_lookup(report)
        # ground-truth context needed for per-pixel metric contribution
        self.valid_mask = report.valid_mask
        self.gt_class_map = report.gt_class_map
        self.gt_instance_map = report.gt_instance_map

    # ------------------------------------------------------------------ build
    @staticmethod
    def _build_entries(mask: PanopticMask, report: MetricsReport):
        match_of: dict[int, dict] = {}
        for cat in report.details["categories"]:
            for m in cat["matches"]:
                match_of[m["pred_instance"]] = m
        entries = []
        inst = mask.instance_map
        scored = ~mask.ignore_mask
        for iid in sorted(int(v) for v in np.unique(inst[scored]) if v != 0):
            region = inst == iid
            ys, xs = np.where(region)
            confs = mask.conf_map[region]
            m = match_of.get(iid)
            entries.append(
                InstanceEntry(
                    instance_id=iid,
                    category_id=int(mask.class_map[region][0]),
                    pixel_count=int(region.sum()),
                    bbox=(int(xs.min()), int(ys.min()),
                          int(xs.max()), int(ys.max())),
                    conf_mean=round(float(confs.mean()), 6),
                    conf_min=round(float(confs.min()), 6),
                    conf_max=round(float(confs.max()), 6),
                    metric_status="TP" if m else "FP",
                    metric_iou=m["iou"] if m else 0.0,
                    matched_gt=m["gt_instance"] if m else 0,
                )
            )
        return tuple(entries)

    @staticmethod
    def _build_match_lookup(report: MetricsReport):
        lookup = {}
        for cat in report.details["categories"]:
            for m in cat["matches"]:
                lookup[m["pred_instance"]] = (m["gt_instance"], m["iou"])
        return lookup

    # ------------------------------------------------------------- identity
    def canonical_bytes(self) -> bytes:
        doc = {
            "config_fingerprint": self.config_fingerprint,
            "mapping_version": self.mapping_version,
            "snapshot_version": self.snapshot_version,
            "iou_threshold": self.iou_threshold,
            "precision": self.precision,
            "width": self.mask.width,
            "height": self.mask.height,
            "entries": [e.to_dict() for e in self.entries],
            "mask_checksum": self.mask.checksum(),
        }
        return json.dumps(doc, sort_keys=True).encode("utf-8")

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    # -------------------------------------------------------------- lookup
    def entry(self, instance_id: int) -> InstanceEntry | None:
        return self._by_id.get(instance_id)

    @property
    def num_entries(self) -> int:
        return len(self.entries)

    @property
    def mask_size(self) -> int:
        return self.mask.width * self.mask.height
