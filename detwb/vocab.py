"""类别词表：单调递增版本号、内容校验和、已停用类别集合与整体替换语义。"""

import json

from .canon import canonical_json, sha256_text
from .errors import Err, WorkbenchError


def vocab_checksum(categories):
    """categories: {class_id(int): {"name": str, "retired": bool}}"""
    canon = {str(k): {"name": v["name"], "retired": bool(v.get("retired", False))}
             for k, v in sorted(categories.items(), key=lambda kv: int(kv[0]))}
    return sha256_text(canonical_json(canon))


class VocabSnapshot:
    def __init__(self, version, categories):
        self.version = int(version)
        self.categories = {int(k): {"name": v["name"], "retired": bool(v.get("retired", False))}
                           for k, v in categories.items()}
        self.checksum = vocab_checksum(self.categories)

    @property
    def retired_ids(self):
        return sorted(cid for cid, c in self.categories.items() if c["retired"])

    def name_of(self, class_id):
        entry = self.categories.get(int(class_id))
        return entry["name"] if entry else f"unknown_{class_id}"

    def is_retired(self, class_id):
        entry = self.categories.get(int(class_id))
        return bool(entry and entry["retired"])

    def to_dict(self):
        return {
            "version": self.version,
            "checksum": self.checksum,
            "categories": {str(k): v for k, v in sorted(self.categories.items())},
            "retired": self.retired_ids,
        }

    @classmethod
    def from_dict(cls, data):
        snap = cls(data["version"], data["categories"])
        if "checksum" in data and data["checksum"] != snap.checksum:
            raise WorkbenchError(Err.CACHE_CORRUPT, "词表校验和与内容不符",
                                 field="checksum",
                                 details={"expected": snap.checksum, "actual": data["checksum"]})
        return snap

    @classmethod
    def load_file(cls, path):
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def save_file(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(canonical_json(self.to_dict()) + "\n")


class VocabRegistry:
    """持有当前词表快照；演进时整体替换，禁止回退。""" 

    def __init__(self):
        self.current = None

    def load(self, snapshot):
        """加载新快照。版本或校验和变化 => 整体替换；旧版本覆盖 => E_SNAPSHOT_ROLLBACK。"""
        if self.current is not None:
            if snapshot.version < self.current.version:
                raise WorkbenchError(
                    Err.SNAPSHOT_ROLLBACK,
                    f"拒绝回退：当前词表版本 {self.current.version}，尝试加载 {snapshot.version}",
                    details={"current": self.current.version, "attempted": snapshot.version})
            if (snapshot.version == self.current.version
                    and snapshot.checksum == self.current.checksum):
                return False
        self.current = snapshot  # 整体替换，不做增量修补
        return True

    def resolve_class(self, class_id, fallback_class_id=None):
        """返回 (class_id, class_name, provenance)。命中已停用类别时按策略处理。"""
        snap = self.current
        if snap is None:
            raise WorkbenchError(Err.INCONSISTENT, "词表未加载", category="vocab_missing")
        class_id = int(class_id)
        if snap.is_retired(class_id):
            if fallback_class_id is None:
                raise WorkbenchError(
                    Err.LABEL_RETIRED,
                    f"类别 {class_id}（{snap.name_of(class_id)}）已在词表版本 "
                    f"{snap.version} 中停用",
                    field="class_id",
                    details={"class_id": class_id, "vocab_version": snap.version})
            fb = int(fallback_class_id)
            return fb, snap.name_of(fb), f"fallback:{class_id}->{fb}"
        return class_id, snap.name_of(class_id), "vocab"
