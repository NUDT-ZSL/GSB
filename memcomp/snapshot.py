"""记忆条目快照与迁移配置的版本化整体替换及并发提交控制。"""

import hashlib
import json
import os
from pathlib import Path

from .errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    MemError,
)


def _checksum(payload_bytes):
    return hashlib.sha256(payload_bytes).hexdigest()


class SnapshotStore:
    """快照携带单调递增版本号与内容校验和；任何变化都整体替换。"""

    def __init__(self, root):
        self.root = Path(root) / "snapshots"
        self.root.mkdir(parents=True, exist_ok=True)

    def _latest_path(self, kind):
        return self.root / f"LATEST_{kind}"

    def _snapshot_path(self, kind, version):
        return self.root / f"{kind}_v{version}.json"

    def current_version(self, kind):
        latest = self._latest_path(kind)
        if not latest.exists():
            return 0
        return int(latest.read_text(encoding="utf-8").strip())

    def commit(self, kind, payload_bytes, version):
        """整体替换式提交；版本号必须单调递增，否则 E_SNAPSHOT_ROLLBACK。"""
        current = self.current_version(kind)
        if version <= current:
            raise MemError(
                E_SNAPSHOT_ROLLBACK,
                f"快照回退被拒绝：目标版本 v{version} <= 当前版本 v{current}（kind={kind}）",
            )
        if isinstance(payload_bytes, str):
            payload_bytes = payload_bytes.encode("utf-8")
        doc = {
            "kind": kind,
            "version": version,
            "checksum": _checksum(payload_bytes),
            "payload": json.loads(payload_bytes.decode("utf-8")),
        }
        tmp = self._snapshot_path(kind, version).with_suffix(".tmp")
        tmp.write_text(
            json.dumps(doc, sort_keys=True, ensure_ascii=True), encoding="utf-8"
        )
        os.replace(tmp, self._snapshot_path(kind, version))
        self._latest_path(kind).write_text(str(version), encoding="utf-8")
        return version

    def load(self, kind, version=None):
        """加载并校验校验和；损坏或校验和不符返回 E_SNAPSHOT_CORRUPT。"""
        if version is None:
            version = self.current_version(kind)
            if version == 0:
                raise MemError(E_SNAPSHOT_CORRUPT, f"快照不存在（kind={kind}）")
        path = self._snapshot_path(kind, version)
        if not path.exists():
            raise MemError(E_SNAPSHOT_CORRUPT, f"快照文件缺失: {path}")
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            payload_bytes = json.dumps(
                doc["payload"], sort_keys=True, ensure_ascii=True
            ).encode("utf-8")
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise MemError(E_SNAPSHOT_CORRUPT, f"快照损坏: {exc}")
        if doc.get("checksum") != _checksum(payload_bytes):
            raise MemError(
                E_SNAPSHOT_CORRUPT,
                f"快照校验和不符（kind={kind}, version={version}）",
            )
        return doc["payload"]


class StageLock:
    """增量提交的非阻塞互斥锁：冲突时后到者立即获得 E_STAGE_BUSY。"""

    def __init__(self, root):
        self.lock_path = Path(root) / "stage.lock"
        self._fd = None

    def acquire(self):
        try:
            self._fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise MemError(E_STAGE_BUSY, "另一次增量提交正在进行，立即失败不等待")
        os.write(self._fd, str(os.getpid()).encode("ascii"))
        return self

    def release(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            os.unlink(self.lock_path)
        except FileNotFoundError:
            pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
