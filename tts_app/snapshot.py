"""配置与统计量快照：版本化整体替换、并发提交控制。"""

import hashlib
import json
import os
import threading

from .cache import canonical_json
from .errors import E_SNAPSHOT_ROLLBACK, E_STAGE_BUSY, TtsError


class StageLock:
    """同一时刻只允许一次增量提交生效。"""

    def __init__(self):
        self._lock = threading.Lock()

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            raise TtsError(E_STAGE_BUSY, "已有增量提交在进行中")
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False


class SnapshotStore:
    """快照携带单调递增版本号与内容校验和，变化时整体替换。"""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.stage_lock = StageLock()

    def _manifest_path(self):
        return os.path.join(self.root, "manifest.json")

    def _data_path(self, version):
        return os.path.join(self.root, f"snapshot_v{version}.json")

    def current(self):
        path = self._manifest_path()
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        data_path = self._data_path(manifest["version"])
        with open(data_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        checksum = hashlib.sha256(
            canonical_json(data).encode("utf-8")).hexdigest()
        manifest["checksum_valid"] = checksum == manifest["checksum"]
        manifest["data"] = data
        return manifest

    def commit(self, data, version):
        """整体替换式提交；旧版本不得覆盖新版本。"""
        with self.stage_lock:
            current = self.current()
            if current is not None and version <= current["version"]:
                raise TtsError(
                    E_SNAPSHOT_ROLLBACK,
                    "旧版本不得覆盖新版本",
                    current_version=current["version"],
                    attempted_version=version)
            checksum = hashlib.sha256(
                canonical_json(data).encode("utf-8")).hexdigest()
            data_path = self._data_path(version)
            tmp_path = data_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1,
                          sort_keys=True)
            os.replace(tmp_path, data_path)
            manifest = {"version": version, "checksum": checksum}
            manifest_tmp = self._manifest_path() + ".tmp"
            with open(manifest_tmp, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, ensure_ascii=False, indent=1)
            os.replace(manifest_tmp, self._manifest_path())
            for name in os.listdir(self.root):
                if (name.startswith("snapshot_v")
                        and name != os.path.basename(data_path)):
                    os.remove(os.path.join(self.root, name))
            return manifest
