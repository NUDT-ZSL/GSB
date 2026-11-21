"""确定性候选匹配：按类别分组、IoU 矩阵、贪心指派、TP/FP/FN 与聚合统计。"""

import numpy as np


def iou_matrix(cand_boxes, gt_boxes):
    """返回 (n_cand, n_gt) 的 IoU 矩阵与比较次数。纯函数，结果确定。"""
    n, m = len(cand_boxes), len(gt_boxes)
    out = np.zeros((n, m), dtype=np.float64)
    if n == 0 or m == 0:
        return out, 0
    c = np.asarray(cand_boxes, dtype=np.float64)
    g = np.asarray(gt_boxes, dtype=np.float64)
    comparisons = 0
    for i in range(n):
        ix1 = np.maximum(c[i, 0], g[:, 0])
        iy1 = np.maximum(c[i, 1], g[:, 1])
        ix2 = np.minimum(c[i, 2], g[:, 2])
        iy2 = np.minimum(c[i, 3], g[:, 3])
        iw = np.maximum(ix2 - ix1, 0.0)
        ih = np.maximum(iy2 - iy1, 0.0)
        inter = iw * ih
        area_c = max((c[i, 2] - c[i, 0]) * (c[i, 3] - c[i, 1]), 0.0)
        area_g = np.maximum((g[:, 2] - g[:, 0]) * (g[:, 3] - g[:, 1]), 0.0)
        union = area_c + area_g - inter
        out[i] = np.where(union > 0.0, inter / np.maximum(union, 1e-12), 0.0)
        comparisons += m
    return out, comparisons


def group_by_class(candidates, gts):
    """按 class_id / label 分组，键排序以保证确定性遍历。"""
    cand_groups = {}
    for c in candidates:
        cand_groups.setdefault(int(c["class_id"]), []).append(c)
    gt_groups = {}
    for g in gts:
        gt_groups.setdefault(int(g["label"]), []).append(g)
    return {cid: (cand_groups.get(cid, []), gt_groups.get(cid, []))
            for cid in sorted(set(cand_groups) | set(gt_groups))}


def greedy_assign(candidates, gts, ious, iou_threshold):
    """贪心指派：score 降序，同分按 (x1,y1,x2,y2,cand_idx) 字典序打破平局。"""
    order = sorted(
        range(len(candidates)),
        key=lambda i: (-candidates[i]["score"],
                       candidates[i]["box"][0], candidates[i]["box"][1],
                       candidates[i]["box"][2], candidates[i]["box"][3],
                       candidates[i]["cand_idx"]),
    )
    matched_gt = set()
    assignment = {}
    for i in order:
        best_gj, best_iou = -1, 0.0
        for gj in range(len(gts)):
            if gj in matched_gt:
                continue
            v = float(ious[i, gj])
            if v >= iou_threshold and (best_gj < 0 or v > best_iou):
                best_gj, best_iou = gj, v
        if best_gj >= 0:
            matched_gt.add(best_gj)
            assignment[candidates[i]["cand_idx"]] = (gts[best_gj]["gt_idx"],
                                                     round(best_iou, 6))
        else:
            assignment[candidates[i]["cand_idx"]] = (-1, 0.0)
    return assignment


def match_sample(sample, iou_threshold, vocab_registry=None, fallback_class_id=None):
    """对单样本执行完整匹配，返回规范化记录列表与统计（v3 全量字段）。"""
    groups = group_by_class(sample["candidates"], sample["gt"])
    records = []
    total_comparisons = 0
    for class_id in sorted(groups):
        cands, gts = groups[class_id]
        ious, cmp_count = iou_matrix([c["box"] for c in cands],
                                     [g["box"] for g in gts])
        total_comparisons += cmp_count
        assignment = greedy_assign(cands, gts, ious, iou_threshold)
        gt_by_idx = {g["gt_idx"]: g for g in gts}
        provenance = "vocab"
        eff_class_id, class_name = class_id, None
        if vocab_registry is not None:
            eff_class_id, class_name, provenance = vocab_registry.resolve_class(
                class_id, fallback_class_id)
        for c in sorted(cands, key=lambda c: c["cand_idx"]):
            gt_idx, iou = assignment[c["cand_idx"]]
            gt = gt_by_idx.get(gt_idx)
            rec = {
                "sample_id": sample["sample_id"],
                "cand_idx": c["cand_idx"],
                "x1": c["box"][0], "y1": c["box"][1],
                "x2": c["box"][2], "y2": c["box"][3],
                "score": c["score"],
                "class_id": eff_class_id,
                "class_name": class_name if class_name is not None else f"class_{class_id}",
                "gt_idx": gt_idx,
                "iou": iou,
                "decision": "TP" if gt_idx >= 0 else "FP",
                "difficulty": gt["difficulty"] if gt else "none",
                "occluded": bool(gt["occluded"]) if gt else False,
                "conf_lo": round(max(0.0, c["score"] - 0.05), 6),
                "conf_hi": round(min(1.0, c["score"] + 0.05), 6),
            }
            if provenance.startswith("fallback:"):
                rec["class_provenance"] = provenance
            records.append(rec)
    tp_c = sorted(r["cand_idx"] for r in records if r["decision"] == "TP")
    fp_c = sorted(r["cand_idx"] for r in records if r["decision"] == "FP")
    matched_gt = {r["gt_idx"] for r in records if r["gt_idx"] >= 0}
    fn_g = sorted(g["gt_idx"] for g in sample["gt"] if g["gt_idx"] not in matched_gt)
    stats = {"tp": tp_c, "fp": fp_c, "fn": fn_g,
             "iou_comparisons": total_comparisons}
    return records, stats


def aggregate(records, samples_gt):
    """按类别与难度聚合。samples_gt: {sample_id: [gt,...]} 用于 FN 难度归属。"""
    gt_lookup = {}
    for sid, gts in samples_gt.items():
        for g in gts:
            gt_lookup[(sid, g["gt_idx"])] = g
    by_class = {}
    by_class_diff = {}
    matched_gt = set()
    for r in sorted(records, key=lambda r: (r["sample_id"], r["cand_idx"])):
        cid = r["class_id"]
        b = by_class.setdefault(cid, {"tp": 0, "fp": 0, "fn": 0})
        if r["decision"] == "TP":
            b["tp"] += 1
            matched_gt.add((r["sample_id"], r["gt_idx"]))
            d = by_class_diff.setdefault((cid, r["difficulty"]),
                                         {"tp": 0, "fp": 0, "fn": 0})
            d["tp"] += 1
        else:
            b["fp"] += 1
            d = by_class_diff.setdefault((cid, "none"), {"tp": 0, "fp": 0, "fn": 0})
            d["fp"] += 1
    for (sid, gt_idx), g in sorted(gt_lookup.items()):
        if (sid, gt_idx) in matched_gt:
            continue
        cid = int(g["label"])
        by_class.setdefault(cid, {"tp": 0, "fp": 0, "fn": 0})["fn"] += 1
        d = by_class_diff.setdefault((cid, g["difficulty"]), {"tp": 0, "fp": 0, "fn": 0})
        d["fn"] += 1

    def finalize(bucket):
        tp, fp, fn = bucket["tp"], bucket["fp"], bucket["fn"]
        bucket["precision"] = round(tp / (tp + fp), 6) if (tp + fp) else 0.0
        bucket["recall"] = round(tp / (tp + fn), 6) if (tp + fn) else 0.0
        return bucket

    return {
        "by_class": {str(k): finalize(v) for k, v in sorted(by_class.items())},
        "by_class_difficulty": {f"{k[0]}|{k[1]}": finalize(v)
                                for k, v in sorted(by_class_diff.items())},
    }
