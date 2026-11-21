"""候选匹配有向无环计算图：摄入校验→按类分组→IoU→贪心指派→统计→聚合。"""

from .cache import MatchCache, boxes_digest, node_cache_key, plan_invalidation
from .canon import sha256_obj
from .errors import Err, WorkbenchError
from .matching import aggregate, group_by_class, greedy_assign, iou_matrix
from .samples import image_sha256
from .snapshots import ResourceLimits, SnapshotStore

NODES = ["ingest", "group", "iou", "assign", "stats", "aggregate"]
EDGES = [("ingest", "group"), ("group", "iou"), ("iou", "assign"),
         ("assign", "stats"), ("stats", "aggregate")]


def topo_order(nodes, edges):
    """Kahn 拓扑排序；存在环时返回 E_INCONSISTENT(category=cycle)。"""
    indeg = {n: 0 for n in nodes}
    adj = {n: [] for n in nodes}
    for a, b in edges:
        adj[a].append(b)
        indeg[b] += 1
    queue = sorted(n for n in nodes if indeg[n] == 0)
    order = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for m in sorted(adj[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
        queue.sort()
    if len(order) != len(nodes):
        raise WorkbenchError(Err.INCONSISTENT, "计算图存在环",
                             category="cycle")
    return order


class Pipeline:
    """确定性匹配流水线：节点级缓存 + 资源上限 + 降级标记。"""

    def __init__(self, config, vocab_registry, store=None):
        self.config = config
        self.vocab = vocab_registry
        self.store = store if store is not None else SnapshotStore()
        self.cache = MatchCache(self.store)
        self.limits = ResourceLimits.from_dict(config.get("limits", {}))
        self.node_states = {n: {"computed": 0, "reused": 0} for n in NODES}
        self.degradation = []
        self.errors = []

    def run(self, samples, prev_config=None, prev_vocab=None):
        cfg = self.config
        threshold = float(cfg["iou_threshold"])
        fallback = cfg.get("fallback_class_id")
        vocab_snap = self.vocab.current
        self.limits.check_images(len(samples))
        order = topo_order(NODES, EDGES)

        invalidation = None
        if prev_config is not None:
            invalidation = plan_invalidation(
                samples, prev_config, cfg, prev_vocab, vocab_snap)

        ingested = []
        for s in sorted(samples, key=lambda s: s["sample_id"]):
            actual = image_sha256(s["seed"], s["index"])
            if actual != s["image"]["sha256"]:
                raise WorkbenchError(
                    Err.INCONSISTENT,
                    f"样本 {s['sample_id']} 图像内容哈希与清单不符",
                    category="image_hash")
            ingested.append(s)
        self.node_states["ingest"]["computed"] += len(ingested)

        resident = sum(len(s["candidates"]) for s in ingested)
        degrade = self.limits.check_resident_candidates(resident)
        if degrade:
            self.degradation.append(degrade)

        all_records = []
        stats = {}
        total_comparisons = 0
        for s in ingested:
            sid = s["sample_id"]
            cand_d = boxes_digest(s["candidates"])
            gt_d = boxes_digest(s["gt"])
            ingest_hash = sha256_obj({"sample": sid,
                                      "image": s["image"]["sha256"],
                                      "cand": cand_d, "gt": gt_d})
            groups = group_by_class(s["candidates"], s["gt"])
            self.node_states["group"]["computed"] += 1

            iou_key = node_cache_key("iou", sid, s["image"]["sha256"],
                                     cand_d, gt_d, None, None, ingest_hash)
            iou_payload = self.cache.get(iou_key)
            if iou_payload is not None:
                matrices = {int(k): v for k, v in iou_payload["matrices"].items()}
                comparisons = iou_payload["comparisons"]
                self.node_states["iou"]["reused"] += 1
            else:
                matrices, comparisons = {}, 0
                for cid in sorted(groups):
                    cands, gts = groups[cid]
                    m, cnt = iou_matrix([c["box"] for c in cands],
                                        [g["box"] for g in gts])
                    matrices[cid] = m.tolist()
                    comparisons += cnt
                self.cache.put(iou_key,
                               {"matrices": {str(k): v for k, v in matrices.items()},
                                "comparisons": comparisons})
                self.node_states["iou"]["computed"] += 1
            total_comparisons += comparisons
            self.limits.check_iou_comparisons(total_comparisons)

            assign_key = node_cache_key("assign", sid, s["image"]["sha256"],
                                        cand_d, gt_d, threshold,
                                        vocab_snap.version, iou_key)
            assign_payload = self.cache.get(assign_key)
            if assign_payload is not None and (
                    not invalidation or sid in invalidation["reuse"]):
                recs, st = assign_payload["records"], assign_payload["stats"]
                self.node_states["assign"]["reused"] += 1
                self.node_states["stats"]["reused"] += 1
            else:
                recs, st = self._assign(s, groups, matrices, threshold, fallback)
                self.cache.put(assign_key, {"records": recs, "stats": st})
                self.node_states["assign"]["computed"] += 1
                self.node_states["stats"]["computed"] += 1
            all_records.extend(recs)
            stats[sid] = st

        samples_gt = {s["sample_id"]: s["gt"] for s in ingested}
        aggregates = aggregate(all_records, samples_gt)
        self.node_states["aggregate"]["computed"] += 1

        return {
            "config": {"iou_threshold": threshold,
                       "fallback_class_id": fallback,
                       "limits": self.limits.to_dict()},
            "vocab_version": vocab_snap.version,
            "vocab_checksum": vocab_snap.checksum,
            "topo_order": order,
            "records": sorted(all_records,
                              key=lambda r: (r["sample_id"], r["cand_idx"])),
            "stats": stats,
            "aggregates": aggregates,
            "node_states": self.node_states,
            "invalidation": invalidation,
            "degradation": sorted(self.degradation),
            "iou_comparisons": total_comparisons,
            "cache": self.cache.stats(),
            "errors": list(self.errors),
        }

    def _assign(self, sample, groups, matrices, threshold, fallback):
        import numpy as np
        records = []
        for cid in sorted(groups):
            cands, gts = groups[cid]
            ious = np.asarray(matrices[cid], dtype=np.float64)
            assignment = greedy_assign(cands, gts, ious, threshold)
            gt_by_idx = {g["gt_idx"]: g for g in gts}
            eff_cid, class_name, prov = self.vocab.resolve_class(cid, fallback)
            for c in sorted(cands, key=lambda c: c["cand_idx"]):
                gt_idx, iou = assignment[c["cand_idx"]]
                gt = gt_by_idx.get(gt_idx)
                rec = {
                    "sample_id": sample["sample_id"],
                    "cand_idx": c["cand_idx"],
                    "x1": c["box"][0], "y1": c["box"][1],
                    "x2": c["box"][2], "y2": c["box"][3],
                    "score": c["score"],
                    "class_id": eff_cid,
                    "class_name": class_name,
                    "gt_idx": gt_idx,
                    "iou": iou,
                    "decision": "TP" if gt_idx >= 0 else "FP",
                    "difficulty": gt["difficulty"] if gt else "none",
                    "occluded": bool(gt["occluded"]) if gt else False,
                    "conf_lo": round(max(0.0, c["score"] - 0.05), 6),
                    "conf_hi": round(min(1.0, c["score"] + 0.05), 6),
                }
                if prov.startswith("fallback:"):
                    rec["class_provenance"] = prov
                records.append(rec)
        tp = sorted(r["cand_idx"] for r in records if r["decision"] == "TP")
        fp = sorted(r["cand_idx"] for r in records if r["decision"] == "FP")
        matched = {r["gt_idx"] for r in records if r["gt_idx"] >= 0}
        fn = sorted(g["gt_idx"] for g in sample["gt"]
                    if g["gt_idx"] not in matched)
        return records, {"tp": tp, "fp": fp, "fn": fn}
