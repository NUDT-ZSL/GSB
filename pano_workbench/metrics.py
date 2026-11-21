"""Semantic mIoU and instance PQ aggregation.

Both metrics are computed over *scored* pixels only (pixels ignored by
either prediction or ground truth are excluded, per config policy).
Instance matching is greedy: candidate pairs sorted by IoU descending,
ties broken by ascending instance id.  Every aggregate can be
independently recomputed from the per-image per-pixel details via
``recompute_from_details``.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json

import numpy as np

from .config import WorkbenchConfig
from .merge import PanopticMask


@dataclasses.dataclass
class CategoryMetrics:
    category_id: int
    iou: float | None          # None when the category is absent from both sides
    pq: float
    sq: float
    rq: float
    tp: int
    fp: int
    fn: int


@dataclasses.dataclass
class MetricsReport:
    miou: float
    pq_mean: float
    per_category: dict          # category_id -> CategoryMetrics
    details: dict               # JSON-serializable per-pixel/per-instance detail
    valid_mask: np.ndarray      # scored pixels
    gt_class_map: np.ndarray
    gt_instance_map: np.ndarray
    config_fingerprint: str

    def canonical_bytes(self) -> bytes:
        return json.dumps(self.details, sort_keys=True).encode("utf-8")

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def evaluate(pred: PanopticMask, gt: PanopticMask, cfg: WorkbenchConfig) -> MetricsReport:
    valid = (~pred.ignore_mask) & (~gt.ignore_mask)
    pc = pred.class_map[valid]
    gc = gt.class_map[valid]
    pi = pred.instance_map[valid]
    gi = gt.instance_map[valid]
    categories = list(range(0, cfg.num_categories + 1))  # 0 = background stuff
    per_category: dict[int, CategoryMetrics] = {}
    det_categories = []

    for c in categories:
        inter = int(np.count_nonzero((pc == c) & (gc == c)))
        union = int(np.count_nonzero((pc == c) | (gc == c)))
        iou = (inter / union) if union > 0 else None

        pred_ids = [int(v) for v in np.unique(pi[pc == c]) if v != 0]
        gt_ids = [int(v) for v in np.unique(gi[gc == c]) if v != 0]
        pred_area = {i: int(np.count_nonzero(pi == i)) for i in pred_ids}
        gt_area = {i: int(np.count_nonzero(gi == i)) for i in gt_ids}

        # pairwise intersections via joint histogram on scored pixels
        pairs: dict[tuple[int, int], int] = {}
        if pred_ids and gt_ids:
            key = pi.astype(np.int64) * (max(gt_ids) + 1) + gi
            sel = np.isin(pi, pred_ids) & np.isin(gi, gt_ids)
            vals, counts = np.unique(key[sel], return_counts=True)
            base = max(gt_ids) + 1
            for k, n in zip(vals.tolist(), counts.tolist()):
                pairs[(k // base, k % base)] = int(n)

        candidates = []
        for (pid, gid), inter_n in pairs.items():
            union_n = pred_area[pid] + gt_area[gid] - inter_n
            pair_iou = inter_n / union_n if union_n > 0 else 0.0
            if pair_iou > cfg.iou_threshold:
                candidates.append((pid, gid, pair_iou, inter_n, union_n))
        # IoU desc, ties broken by ascending instance id
        candidates.sort(key=lambda t: (-t[2], t[0], t[1]))

        matched_pred, matched_gt, matches = set(), set(), []
        for pid, gid, pair_iou, inter_n, union_n in candidates:
            if pid in matched_pred or gid in matched_gt:
                continue
            matched_pred.add(pid)
            matched_gt.add(gid)
            matches.append(
                {"pred_instance": pid, "gt_instance": gid,
                 "iou": round(pair_iou, 8)}
            )

        tp = len(matches)
        fp = len(pred_ids) - tp
        fn = len(gt_ids) - tp
        sq = sum(m["iou"] for m in matches) / tp if tp else 0.0
        rq = tp / (tp + 0.5 * fp + 0.5 * fn) if (tp + fp + fn) else 0.0
        pq = sq * rq

        per_category[c] = CategoryMetrics(
            category_id=c, iou=iou, pq=pq, sq=sq, rq=rq, tp=tp, fp=fp, fn=fn,
        )
        det_categories.append(
            {
                "category_id": c,
                "semantic": {"intersection": inter, "union": union},
                "pred_instances": [
                    {"instance_id": i, "area": pred_area[i],
                     "matched": i in matched_pred}
                    for i in sorted(pred_ids)
                ],
                "gt_instances": [
                    {"instance_id": i, "area": gt_area[i],
                     "matched": i in matched_gt}
                    for i in sorted(gt_ids)
                ],
                "matches": sorted(matches, key=lambda m: m["pred_instance"]),
                "tp": tp, "fp": fp, "fn": fn,
            }
        )

    ious = [m.iou for m in per_category.values() if m.iou is not None]
    miou = float(np.mean(ious)) if ious else 0.0
    pqs = [m.pq for m in per_category.values()]
    pq_mean = float(np.mean(pqs)) if pqs else 0.0

    details = {
        "iou_threshold": cfg.iou_threshold,
        "ignore_policy": "exclude_either",
        "scored_pixels": int(valid.sum()),
        "categories": det_categories,
        "miou": round(miou, 8),
        "pq_mean": round(pq_mean, 8),
    }
    return MetricsReport(
        miou=miou, pq_mean=pq_mean, per_category=per_category, details=details,
        valid_mask=valid, gt_class_map=gt.class_map, gt_instance_map=gt.instance_map,
        config_fingerprint=cfg.fingerprint(),
    )


def recompute_from_details(details: dict, cfg: WorkbenchConfig) -> dict:
    """Independent recomputation of every aggregate from the details only.

    Uses plain Python loops (a different code path than ``evaluate``) so the
    self-check genuinely cross-validates the batch pipeline.
    """
    ious, pqs = [], []
    for cat in details["categories"]:
        sem = cat["semantic"]
        if sem["union"] > 0:
            ious.append(sem["intersection"] / sem["union"])
        tp = sum(1 for p in cat["pred_instances"] if p["matched"])
        fp = sum(1 for p in cat["pred_instances"] if not p["matched"])
        fn = sum(1 for g in cat["gt_instances"] if not g["matched"])
        sq = sum(m["iou"] for m in cat["matches"]) / tp if tp else 0.0
        rq = tp / (tp + 0.5 * fp + 0.5 * fn) if (tp + fp + fn) else 0.0
        pqs.append(sq * rq)
        if tp != cat["tp"] or fp != cat["fp"] or fn != cat["fn"]:
            raise ValueError(
                f"detail bookkeeping mismatch in category {cat['category_id']}"
            )
    return {
        "miou": sum(ious) / len(ious) if ious else 0.0,
        "pq_mean": sum(pqs) / len(pqs) if pqs else 0.0,
    }
