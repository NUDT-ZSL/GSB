"""版本化快照存储与整体替换。

- 三类快照：manifest（事件清单）、policy（显示策略）、lines（行快照）；
- 每个快照携带单调递增版本号与内容校验和（sha256）；
- 版本号或校验和变化 -> 整体替换（临时文件 + 原子 rename），禁止增量修补；
- 旧版本不得覆盖新版本 -> E_SNAPSHOT_ROLLBACK；
- 损坏或校验和不符 -> E_SNAPSHOT_CORRUPT，指明快照类别与版本号；
- 同一时刻只允许一个提交生效，冲突时后到者立即获得 E_STAGE_BUSY。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Dict, Optional

from .config import content_checksum
from .errors import (E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK, E_STAGE_BUSY,
                     BusinessError)

CATEGORIES = ("manifest", "policy", "lines")


@dataclass(frozen=True)
class SnapshotMeta:
    category: str
    version: int
    checksum: str


class SnapshotStore:
    """本地目录快照存储；提交经非阻塞互斥锁串行化。"""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self._commit_lock = threading.Lock()
        self._metas: Dict[str, SnapshotMeta] = {}
        for cat in CATEGORIES:
            snap = self.load(cat, missing_ok=True)
            if snap is not None:
                self._metas[cat] = SnapshotMeta(cat, snap["version"],
                                                snap["checksum"])

    def _path(self, category: str) -> str:
        return os.path.join(self.directory, f"{category}.snapshot.json")

    def meta(self, category: str) -> Optional[SnapshotMeta]:
        return self._metas.get(category)

    def commit(self, category: str, version: int, payload: str) -> SnapshotMeta:
        """整体替换提交；版本回退 -> E_SNAPSHOT_ROLLBACK。"""
        if category not in CATEGORIES:
            raise ValueError(f"unknown snapshot category: {category}")
        if not self._commit_lock.acquire(blocking=False):
            raise BusinessError(E_STAGE_BUSY,
                                "another snapshot commit is in progress",
                                category=category, version=version)
        try:
            checksum = content_checksum(payload)
            current = self._metas.get(category)
            if current is not None:
                if version < current.version:
                    raise BusinessError(
                        E_SNAPSHOT_ROLLBACK,
                        f"snapshot {category} v{version} cannot overwrite "
                        f"v{current.version}",
                        category=category, version=version,
                        current_version=current.version,
                    )
                if version == current.version and checksum == current.checksum:
                    return current  # 同版本同内容：幂等
            doc = {"category": category, "version": version,
                   "checksum": checksum, "payload": payload}
            tmp = self._path(category) + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(doc, ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":")))
            os.replace(tmp, self._path(category))
            meta = SnapshotMeta(category, version, checksum)
            self._metas[category] = meta
            return meta
        finally:
            self._commit_lock.release()

    def load(self, category: str, missing_ok: bool = False) -> Optional[dict]:
        path = self._path(category)
        if not os.path.exists(path):
            if missing_ok:
                return None
            raise BusinessError(E_SNAPSHOT_CORRUPT,
                                f"snapshot {category} missing",
                                category=category, version=-1)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            version = int(doc["version"])
            checksum = str(doc["checksum"])
            payload = doc["payload"]
        except Exception as exc:
            raise BusinessError(E_SNAPSHOT_CORRUPT,
                                f"snapshot {category} unreadable: {exc}",
                                category=category, version=-1) from exc
        if content_checksum(payload) != checksum:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                f"snapshot {category} v{version} checksum mismatch",
                category=category, version=version,
            )
        return doc
