"""匹配侧缓存与增量失效联动。"""

from .canon import sha256_obj
from .errors import Err, WorkbenchError


def boxes_digest(boxes):
    return sha256_obj(boxes)[:16]


def node_cache_key(node, sample_id, image_hash, cand_digest, gt_digest,
                   iou_threshold, vocab_version, upstream_hash):
    """缓存键纳入图像哈希、框摘要、IoU 阈值、词表版本与上游节点哈希。"""
    return sha256_obj({
        "node": node,
        "sample_id": sample_id,
        "image_hash": image_hash,
        "cand_digest": cand_digest,
        "gt_digest": gt_digest,
        "iou_threshold": iou_threshold,
        "vocab_version": vocab_version,
        "upstream_hash": upstream_hash,
    })


class MatchCache:
    """基于 SnapshotStore 的节点级缓存。"""

    def __init__(self, store):
        self.store = store
        self.hits = []
        self.misses = []

    def get(self, key):
        snap = self.store.read(key)  # 校验和不符 => E_CACHE_CORRUPT
        if snap is None:
            self.misses.append(key)
            return None
        self.hits.append(key)
        return snap.payload

    def put(self, key, payload):
        return self.store.commit(key, payload)

    def stats(self):
        return {"hits": len(self.hits), "misses": len(self.misses)}


def plan_invalidation(samples, old_config, new_config, old_vocab, new_vocab):
    """计算配置/词表变化后的重算与复用集合。"""
    all_ids = sorted(s["sample_id"] for s in samples)
    affected = set()
    if old_config.get("iou_threshold") != new_config.get("iou_threshold"):
        affected.update(all_ids)
    if old_vocab is not None and new_vocab is not None:
        if old_vocab.version != new_vocab.version \
                or old_vocab.checksum != new_vocab.checksum:
            changed = _changed_classes(old_vocab, new_vocab)
            for s in samples:
                classes = {int(c["class_id"]) for c in s["candidates"]}
                classes |= {int(g["label"]) for g in s["gt"]}
                if classes & changed:
                    affected.add(s["sample_id"])
    recompute = sorted(affected)
    reuse = sorted(set(all_ids) - affected)
    if set(recompute) & set(reuse):
        raise WorkbenchError(Err.INCONSISTENT,
                             "重算集合与复用集合存在交集",
                             category="invalidation_overlap")
    if sorted(set(recompute) | set(reuse)) != all_ids:
        raise WorkbenchError(Err.INCONSISTENT,
                             "重算与复用集合的并集未覆盖全部样本",
                             category="invalidation_coverage")
    return {"recompute": recompute, "reuse": reuse}


def _changed_classes(old_vocab, new_vocab):
    changed = set()
    ids = set(old_vocab.categories) | set(new_vocab.categories)
    for cid in ids:
        if old_vocab.categories.get(cid) != new_vocab.categories.get(cid):
            changed.add(cid)
    return changed
