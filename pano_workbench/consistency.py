"""Cross-path consistency: interactive query results vs batch evaluation.

Every scored pixel and every instance is compared between the query path
(index-backed) and the batch path (mask + metrics report).  Values must
agree within the declared precision; the first deviation raises
E_INCONSISTENT with its position and both sides' values.
"""
from __future__ import annotations

import dataclasses

from .config import WorkbenchConfig
from .errors import E_INCONSISTENT, PanoError
from .index import QueryIndex
from .merge import PanopticMask
from .metrics import MetricsReport
from .query import QueryEngine


def _fail(where: str, position, query_value, batch_value):
    raise PanoError(
        E_INCONSISTENT, f"query/batch divergence at {where}",
        position=position, query_path=query_value, batch_path=batch_value,
    )


def verify_query_vs_batch(
    engine: QueryEngine,
    index: QueryIndex,
    mask: PanopticMask,
    report: MetricsReport,
    cfg: WorkbenchConfig,
) -> dict:
    """Full pixel + instance sweep. Returns a summary on success."""
    prec = cfg.precision
    pixels_checked = 0

    # batch-side per-pixel truth, computed without touching the index
    valid = report.valid_mask
    for y in range(mask.height):
        for x in range(mask.width):
            if not valid[y, x]:
                continue
            pixels_checked += 1
            q = engine.query_pixel(x, y)
            b_cid = int(mask.class_map[y, x])
            b_iid = int(mask.instance_map[y, x])
            b_conf = float(mask.conf_map[y, x])
            if q.category_id != b_cid:
                _fail("pixel.category", (x, y), q.category_id, b_cid)
            if q.instance_id != b_iid:
                _fail("pixel.instance", (x, y), q.instance_id, b_iid)
            if abs(q.confidence - b_conf) > prec:
                _fail("pixel.confidence", (x, y), q.confidence, b_conf)
            b_inter = bool(b_cid == int(report.gt_class_map[y, x]))
            if bool(q.metric_contribution.get("in_intersection")) != b_inter:
                _fail("pixel.contribution", (x, y),
                      q.metric_contribution, {"in_intersection": b_inter})

    # batch-side per-instance truth from the metrics details
    match_of = {}
    for cat in report.details["categories"]:
        for m in cat["matches"]:
            match_of[m["pred_instance"]] = m
    instances_checked = 0
    for entry in index.entries:
        instances_checked += 1
        q = engine.query_instance(entry.instance_id)
        m = match_of.get(entry.instance_id)
        b_status = "TP" if m else "FP"
        b_iou = m["iou"] if m else 0.0
        if q.metric_contribution["status"] != b_status:
            _fail("instance.status", entry.instance_id,
                  q.metric_contribution["status"], b_status)
        if abs(q.metric_contribution["iou"] - b_iou) > prec:
            _fail("instance.iou", entry.instance_id,
                  q.metric_contribution["iou"], b_iou)
        if abs(q.confidence - entry.conf_mean) > prec:
            _fail("instance.confidence", entry.instance_id,
                  q.confidence, entry.conf_mean)

    return {
        "pixels_checked": pixels_checked,
        "instances_checked": instances_checked,
        "precision": prec,
        "consistent": True,
    }
