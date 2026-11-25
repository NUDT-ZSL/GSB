"""Stage 2: per-class grouped greedy assignment with full comparison audit."""
from .models import ImageMatch, MatchRow, norm_float


def iou_detail(a, b):
    """Return (intersection, union, iou) for two boxes with x1/y1/x2/y2."""
    ix1 = max(a.x1, b.x1)
    iy1 = max(a.y1, b.y1)
    ix2 = min(a.x2, b.x2)
    iy2 = min(a.y2, b.y2)
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = (a.x2 - a.x1) * (a.y2 - a.y1)
    area_b = (b.x2 - b.x1) * (b.y2 - b.y1)
    union = area_a + area_b - inter
    iou = inter / union if union > 0 else 0.0
    return norm_float(inter), norm_float(union), norm_float(iou)


def _aggregate(rows):
    per_class = {}
    tp = fp = fn = 0
    iou_sum = 0.0
    for row in rows:
        bucket = per_class.setdefault(row.class_id, {"tp": 0, "fp": 0, "fn": 0})
        if row.status == "TP":
            bucket["tp"] += 1
            tp += 1
            iou_sum += row.iou
        elif row.status == "FP":
            bucket["fp"] += 1
            fp += 1
        else:
            bucket["fn"] += 1
            fn += 1
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": norm_float(tp / (tp + fp)) if (tp + fp) else 0.0,
        "recall": norm_float(tp / (tp + fn)) if (tp + fn) else 0.0,
        "mean_iou_tp": norm_float(iou_sum / tp) if tp else 0.0,
        "per_class": {str(k): per_class[k] for k in sorted(per_class)},
    }


def match_image(image_id, cands, gts, threshold):
    """Greedy per-class assignment for one image.

    Candidates sorted by (-score, x1, y1, box_idx); each candidate walks the
    still-unmatched ground truths (box_idx order) and takes the first one
    reaching the IoU threshold. Every comparison is logged for replay.
    """
    im = ImageMatch(image_id=image_id)
    cand_by_class = {}
    for cand in cands:
        cand_by_class.setdefault(cand.class_id, []).append(cand)
    gt_by_class = {}
    for gt in gts:
        gt_by_class.setdefault(gt.label, []).append(gt)
    for class_id in sorted(set(cand_by_class) | set(gt_by_class)):
        group_cands = sorted(
            cand_by_class.get(class_id, []),
            key=lambda c: (-c.score, c.x1, c.y1, c.box_idx),
        )
        group_gts = sorted(gt_by_class.get(class_id, []), key=lambda g: g.box_idx)
        used_gt = set()
        for cand in group_cands:
            assigned = None
            best_iou = 0.0
            for gt in group_gts:
                if gt.box_idx in used_gt:
                    continue
                inter, union, iou = iou_detail(cand, gt)
                best_iou = max(best_iou, iou)
                accepted = iou >= threshold
                im.comparisons.append(
                    {
                        "image_id": image_id,
                        "class_id": class_id,
                        "cand_idx": cand.box_idx,
                        "gt_idx": gt.box_idx,
                        "inter": inter,
                        "union": union,
                        "iou": iou,
                        "threshold": norm_float(threshold),
                        "result": "accept" if accepted else "reject",
                    }
                )
                if accepted:
                    assigned = (gt, inter, union, iou)
                    used_gt.add(gt.box_idx)
                    break
            if assigned is not None:
                gt, inter, union, iou = assigned
                im.rows.append(
                    MatchRow(
                        image_id=image_id,
                        cand_idx=cand.box_idx,
                        gt_idx=gt.box_idx,
                        status="TP",
                        iou=iou,
                        inter=inter,
                        union=union,
                        class_id=class_id,
                        score=cand.score,
                        cand_hash=cand.hash(),
                        gt_revision=gt.revision,
                    )
                )
            else:
                im.rows.append(
                    MatchRow(
                        image_id=image_id,
                        cand_idx=cand.box_idx,
                        gt_idx=None,
                        status="FP",
                        iou=norm_float(best_iou),
                        inter=0.0,
                        union=0.0,
                        class_id=class_id,
                        score=cand.score,
                        cand_hash=cand.hash(),
                        gt_revision=None,
                    )
                )
        for gt in group_gts:
            if gt.box_idx not in used_gt:
                im.rows.append(
                    MatchRow(
                        image_id=image_id,
                        cand_idx=None,
                        gt_idx=gt.box_idx,
                        status="FN",
                        iou=0.0,
                        inter=0.0,
                        union=0.0,
                        class_id=class_id,
                        score=None,
                        cand_hash=None,
                        gt_revision=gt.revision,
                    )
                )
    im.aggregate = _aggregate(im.rows)
    return im


def aggregate_all(matches):
    """Aggregate TP/FP/FN statistics across images."""
    rows = [row for im in matches for row in im.rows]
    return _aggregate(rows)
