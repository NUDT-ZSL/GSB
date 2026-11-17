"""阶段化快照、整体替换提交与恢复一致性。

目录布局::

    snapshot_dir/
      manifest.json                # 全局清单（原子替换）
      requests/<rid>.json          # 每请求阶段产物（原子替换）
      failures.json                # 失败清单（原子替换）

清单记录全局版本号、配置哈希、分组表哈希、注入哈希、已处理请求游标与
各阶段产物版本。每个阶段产物先写临时文件再 os.replace 整体替换。
"""

import os

from .atomicio import atomic_write_json, read_json
from .errors import ConfigMismatch

MANIFEST_NAME = "manifest.json"
STAGE_VERSIONS_INIT = {"load": 0, "normalize": 0, "fuse": 0, "rerank": 0}


class SnapshotStore:
    def __init__(self, snapshot_dir):
        self.dir = snapshot_dir
        self.requests_dir = os.path.join(snapshot_dir, "requests")
        os.makedirs(self.requests_dir, exist_ok=True)
        self.manifest_path = os.path.join(snapshot_dir, MANIFEST_NAME)
        self.failures_path = os.path.join(snapshot_dir, "failures.json")

    def exists(self):
        return os.path.exists(self.manifest_path)

    def load_manifest(self):
        return read_json(self.manifest_path)

    def init_manifest(self, config_hash_value, groups_hash_value, injection_hash):
        manifest = {
            "global_version": 1,
            "config_hash": config_hash_value,
            "groups_hash": groups_hash_value,
            "injection_hash": injection_hash,
            "cursor": [],
            "stage_versions": dict(STAGE_VERSIONS_INIT),
            "request_artifacts": {},
            "completed": False,
        }
        self._write_manifest(manifest)
        atomic_write_json(self.failures_path, [])
        return manifest

    def verify_or_init(self, config_hash_value, groups_hash_value, injection_hash,
                       allow_mismatch=False):
        if not self.exists():
            return self.init_manifest(config_hash_value, groups_hash_value, injection_hash), True
        manifest = self.load_manifest()
        mismatches = []
        if manifest["config_hash"] != config_hash_value:
            mismatches.append("config_hash")
        if manifest["groups_hash"] != groups_hash_value:
            mismatches.append("groups_hash")
        if manifest.get("injection_hash") != injection_hash:
            mismatches.append("injection_hash")
        if mismatches and not allow_mismatch:
            raise ConfigMismatch(
                "snapshot fingerprint mismatch: " + ",".join(mismatches),
                "load",
                "E_CONFIG_MISMATCH",
            )
        if mismatches and allow_mismatch:
            manifest["config_hash"] = config_hash_value
            manifest["groups_hash"] = groups_hash_value
            manifest["injection_hash"] = injection_hash
            manifest["cursor"] = []
            manifest["stage_versions"] = dict(STAGE_VERSIONS_INIT)
            manifest["request_artifacts"] = {}
            manifest["completed"] = False
            self._write_manifest(manifest)
            atomic_write_json(self.failures_path, [])
        return manifest, False

    def request_path(self, request_id):
        safe = request_id.replace(os.sep, "_").replace("/", "_")
        return os.path.join(self.requests_dir, f"{safe}.json")

    def load_request_artifact(self, request_id):
        path = self.request_path(request_id)
        if not os.path.exists(path):
            return None
        return read_json(path)

    def write_request_artifact(self, request_id, artifact):
        atomic_write_json(self.request_path(request_id), artifact)

    def append_failure(self, failure):
        failures = self.read_failures()
        failures.append(failure)
        failures.sort(key=lambda f: (f["line_no"], f.get("request_id") or "", f["message"]))
        atomic_write_json(self.failures_path, failures)

    def read_failures(self):
        if not os.path.exists(self.failures_path):
            return []
        return read_json(self.failures_path)

    def commit_cursor(self, manifest, request_id, stage_versions):
        if request_id not in manifest["cursor"]:
            manifest["cursor"].append(request_id)
        for stage, version in stage_versions.items():
            manifest["stage_versions"][stage] = max(
                manifest["stage_versions"].get(stage, 0), version
            )
        manifest["global_version"] += 1
        manifest["request_artifacts"][request_id] = {
            "path": os.path.relpath(self.request_path(request_id), self.dir),
            "stages": stage_versions,
        }
        self._write_manifest(manifest)

    def mark_completed(self, manifest):
        manifest["completed"] = True
        manifest["global_version"] += 1
        self._write_manifest(manifest)

    def _write_manifest(self, manifest):
        atomic_write_json(self.manifest_path, manifest)
