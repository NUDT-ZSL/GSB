"""快照版本化与并发控制，以及可配置资源上限。"""

import threading

from .canon import canonical_json, sha256_text
from .errors import Err, WorkbenchError


class Snapshot:
    def __init__(self, version, payload):
        self.version = int(version)
        self.payload = payload
        self.checksum = sha256_text(canonical_json(payload))

    def to_dict(self):
        return {"version": self.version, "checksum": self.checksum,
                "payload": self.payload}

    @classmethod
    def from_dict(cls, data):
        snap = cls(data["version"], data["payload"])
        if snap.checksum != data.get("checksum"):
            raise WorkbenchError(Err.CACHE_CORRUPT,
                                 "快照校验和与内容不符",
                                 field="checksum",
                                 details={"expected": snap.checksum,
                                          "actual": data.get("checksum")})
        return snap


class SnapshotStore:
    """键 -> Snapshot。提交经非阻塞互斥，冲突立即报 E_STAGE_BUSY。"""

    def __init__(self):
        self._entries = {}
        self._commit_lock = threading.Lock()

    def commit(self, key, payload):
        """整体替换式提交；版本号单调递增。返回新快照。"""
        if not self._commit_lock.acquire(blocking=False):
            raise WorkbenchError(Err.STAGE_BUSY,
                                 f"另一提交正在进行，键 {key} 的本次提交被拒绝",
                                 details={"key": key})
        try:
            prev = self._entries.get(key)
            version = 1 if prev is None else prev.version + 1
            snap = Snapshot(version, payload)
            self._entries[key] = snap  # 整体替换，不做增量修补
            return snap
        finally:
            self._commit_lock.release()

    def read(self, key):
        snap = self._entries.get(key)
        if snap is None:
            return None
        if snap.checksum != sha256_text(canonical_json(snap.payload)):
            raise WorkbenchError(Err.CACHE_CORRUPT,
                                 f"快照 {key} 校验和与内容不符",
                                 field="checksum", details={"key": key})
        return snap

    def corrupt_for_test(self, key):
        """测试辅助：原地篡改载荷以模拟损坏。"""
        snap = self._entries[key]
        if isinstance(snap.payload, dict):
            snap.payload["_tampered"] = True
        else:
            snap.payload = {"_tampered": True}

    def keys(self):
        return sorted(self._entries)

    def to_dict(self):
        return {k: self._entries[k].to_dict() for k in sorted(self._entries)}

    @classmethod
    def from_dict(cls, data):
        store = cls()
        for k in sorted(data):
            store._entries[k] = Snapshot.from_dict(data[k])
        return store


class ResourceLimits:
    """图像数、IoU 比较次数与驻留候选内存的可配置上限。""" 

    def __init__(self, max_images=1000, max_iou_comparisons=10000000,
                 max_resident_candidates=100000, degrade_allowed=False):
        self.max_images = int(max_images)
        self.max_iou_comparisons = int(max_iou_comparisons)
        self.max_resident_candidates = int(max_resident_candidates)
        self.degrade_allowed = bool(degrade_allowed)

    def to_dict(self):
        return {"max_images": self.max_images,
                "max_iou_comparisons": self.max_iou_comparisons,
                "max_resident_candidates": self.max_resident_candidates,
                "degrade_allowed": self.degrade_allowed}

    @classmethod
    def from_dict(cls, data):
        return cls(**data)

    def check_images(self, count):
        if count > self.max_images:
            raise WorkbenchError(
                Err.RESOURCE_LIMIT,
                f"图像数 {count} 超过上限 {self.max_images}",
                category="images",
                details={"count": count, "limit": self.max_images})

    def check_iou_comparisons(self, count):
        if count > self.max_iou_comparisons:
            raise WorkbenchError(
                Err.RESOURCE_LIMIT,
                f"IoU 比较次数 {count} 超过上限 {self.max_iou_comparisons}",
                category="iou_comparisons",
                details={"count": count,
                         "limit": self.max_iou_comparisons})

    def check_resident_candidates(self, count):
        """驻留候选超限：允许降级时返回降级类别，否则抛 E_RESOURCE_LIMIT。"""
        if count <= self.max_resident_candidates:
            return None
        if self.degrade_allowed:
            return "DEGRADE_CHUNKED_RESIDENT"
        raise WorkbenchError(
            Err.RESOURCE_LIMIT,
            f"驻留候选数 {count} 超过上限 {self.max_resident_candidates}",
            category="resident_candidates",
            details={"count": count,
                     "limit": self.max_resident_candidates})
