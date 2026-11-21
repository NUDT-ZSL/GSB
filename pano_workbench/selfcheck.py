"""Cross-module consistency self-check.

Verifies three categories:
* ``index_vs_mask``     - query index entries cross-check against the mask
* ``metrics_recompute`` - aggregates recompute from the per-pixel details
* ``snapshot_version``  - index and statistics carry the same snapshot version

Any failure raises E_INCONSISTENT naming the category.
"""
from __future__ import annotations

import numpy as np

from .config import WorkbenchConfig
from .errors import E_INCONSISTENT, PanoError
from .index import QueryIndex
from .merge import PanopticMask
from .metrics import MetricsReport, recompute_from_details
from .snapshot import Snapshot


def _fail(category: str, message: str, **details):
    raise PanoError(E_INCONSISTENT, message, category=category, **details)


def check_index_vs_mask(index: QueryIndex, mask: PanopticMask) -> dict:
    scored = ~mask.ignore_mask
    inst = mask.instance_map
    for entry in index.entries:
        region = (inst == entry.instance_id) & scored
        count = int(region.sum())
        if count != entry.pixel_count:
            _fail("index_vs_mask", "index pixel count disagrees with mask",
                  instance_id=entry.instance_id,
                  index_value=entry.pixel_count, mask_value=count)
        if count:
            cid = int(mask.class_map[region][0])
            if cid != entry.category_id:
                _fail("index_vs_mask", "index category disagrees with mask",
                      instance_id=entry.instance_id,
                      index_value=entry.category_id, mask_value=cid)
            conf_mean = round(float(mask.conf_map[region].mean()), 6)
            if abs(conf_mean - entry.conf_mean) > index.precision:
                _fail("index_vs_mask", "index confidence disagrees with mask",
                      instance_id=entry.instance_id,
                      index_value=entry.conf_mean, mask_value=conf_mean)
    # mask -> index direction: every scored instance must be indexed
    indexed = {e.instance_id for e in index.entries}
    for iid in (int(v) for v in np.unique(inst[scored]) if v != 0):
        if iid not in indexed:
            _fail("index_vs_mask", "mask instance missing from index",
                  instance_id=iid)
    return {"entries_checked": len(index.entries)}


def check_metrics_recompute(report: MetricsReport, cfg: WorkbenchConfig) -> dict:
    recomputed = recompute_from_details(report.details, cfg)
    if abs(recomputed["miou"] - report.miou) > cfg.precision:
        _fail("metrics_recompute", "mIoU does not recompute from details",
              batch_value=report.miou, recomputed=recomputed["miou"])
    if abs(recomputed["pq_mean"] - report.pq_mean) > cfg.precision:
        _fail("metrics_recompute", "PQ does not recompute from details",
              batch_value=report.pq_mean, recomputed=recomputed["pq_mean"])
    return {"miou": report.miou, "pq_mean": report.pq_mean}


def check_snapshot_version(index: QueryIndex, snapshot: Snapshot | None) -> dict:
    if snapshot is None:
        _fail("snapshot_version", "no snapshot published",
              index_version=index.snapshot_version)
    if index.snapshot_version != snapshot.version:
        _fail("snapshot_version", "index/stats snapshot version skew",
              index_version=index.snapshot_version,
              stats_version=snapshot.version)
    snapshot.verify()
    return {"snapshot_version": snapshot.version}


def run_self_check(index: QueryIndex, mask: PanopticMask,
                   report: MetricsReport, snapshot: Snapshot | None,
                   cfg: WorkbenchConfig) -> dict:
    return {
        "index_vs_mask": check_index_vs_mask(index, mask),
        "metrics_recompute": check_metrics_recompute(report, cfg),
        "snapshot_version": check_snapshot_version(index, snapshot),
    }
