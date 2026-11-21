"""快照版本化整体替换与并发写控制。

切片集、词表与识别配置快照各携带单调递增版本号与内容校验和；
版本或校验和变化时整体替换；旧版本不得覆盖新版本（E_SNAPSHOT_ROLLBACK）；
损坏或校验和不符（E_SNAPSHOT_CORRUPT）；同一时刻只允许一次提交生效，
冲突时后到者立即获得 E_STAGE_BUSY 而不阻塞等待。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from .config import sha256_hex
from .errors import SnapshotRollback, SnapshotCorrupt, StageBusy


@dataclass
class StagedSnapshot:
    name: str
    version: int
    payload: bytes
    checksum: str


@dataclass
class Snapshot:
    name: str
    version: int
    payload: bytes
    checksum: str

    def to_dict(self):
        return {"name": self.name, "version": self.version,
                "checksum": self.checksum, "size": len(self.payload)}


class SnapshotStore:
    def __init__(self):
        self._snapshots = {}
        self._commit_lock = threading.Lock()

    def stage(self, name: str, version: int, payload: bytes) -> StagedSnapshot:
        return StagedSnapshot(name=name, version=version, payload=payload,
                              checksum=sha256_hex(payload))

    def commit(self, staged: StagedSnapshot) -> Snapshot:
        if not self._commit_lock.acquire(blocking=False):
            raise StageBusy(f"快照 {staged.name} 提交冲突：已有进行中的提交",
                            details={"snapshot": staged.name})
        try:
            if staged.checksum != sha256_hex(staged.payload):
                raise SnapshotCorrupt(
                    f"快照 {staged.name} 校验和不符",
                    details={"snapshot": staged.name, "version": staged.version})
            current = self._snapshots.get(staged.name)
            if current is not None and staged.version <= current.version:
                raise SnapshotRollback(
                    f"快照 {staged.name} 版本 {staged.version} 不得覆盖 "
                    f"当前版本 {current.version}",
                    details={"snapshot": staged.name,
                             "staged_version": staged.version,
                             "current_version": current.version})
            snapshot = Snapshot(name=staged.name, version=staged.version,
                                payload=staged.payload, checksum=staged.checksum)
            self._snapshots[staged.name] = snapshot
            return snapshot
        finally:
            self._commit_lock.release()

    def get(self, name: str) -> Snapshot:
        snapshot = self._snapshots[name]
        if snapshot.checksum != sha256_hex(snapshot.payload):
            raise SnapshotCorrupt(f"快照 {name} 内容损坏",
                                  details={"snapshot": name})
        return snapshot

    def versions(self):
        return {name: s.version for name, s in sorted(self._snapshots.items())}
