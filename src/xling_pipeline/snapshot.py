"""阶段快照：版本化持久化、latest 指针、原子替换与恢复校验。"""

import os

from .errors import E_CONFIG_MISMATCH, E_SNAPSHOT_CORRUPT, ConfigMismatch, SnapshotCorrupt
import json

from .io_utils import atomic_write_json

SNAPSHOT_VERSION = 1
LATEST_NAME = "latest.json"
ENVELOPE_FIELDS = (
    "snapshot_version", "config_hash", "translation_hash",
    "stage", "completed_batches", "version",
)


class SnapshotStore:
    def __init__(self, directory):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def _version_path(self, version):
        return os.path.join(self.directory, f"snapshot_v{version:06d}.json")

    @property
    def latest_path(self):
        return os.path.join(self.directory, LATEST_NAME)

    def commit(self, payload):
        """原子写版本快照，再原子更新 latest 指针。"""
        version = payload["version"]
        atomic_write_json(self._version_path(version), payload)
        atomic_write_json(self.latest_path, {"latest_version": version})
        return version

    def load_latest(self):
        pointer_path = self.latest_path
        if not os.path.exists(pointer_path):
            return None
        try:
            with open(pointer_path, "r", encoding="utf-8") as handle:
                pointer = json.load(handle)
            version = pointer["latest_version"]
        except Exception as exc:
            raise SnapshotCorrupt(f"latest pointer unreadable: {exc}")
        path = self._version_path(version)
        if not os.path.exists(path):
            raise SnapshotCorrupt(f"snapshot version {version} missing")
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        for field in ENVELOPE_FIELDS:
            if field not in payload:
                raise SnapshotCorrupt(f"snapshot missing field {field}")
        if payload["snapshot_version"] != SNAPSHOT_VERSION:
            raise SnapshotCorrupt(
                f"unsupported snapshot_version {payload['snapshot_version']}"
            )
        return payload

    def validate_resume(self, payload, current_config_hash, current_translation_hash):
        """恢复前校验版本号、配置哈希与翻译表哈希，任一不一致拒绝续跑。"""
        if payload["snapshot_version"] != SNAPSHOT_VERSION:
            raise ConfigMismatch(
                f"snapshot version {payload['snapshot_version']} != {SNAPSHOT_VERSION}",
                E_CONFIG_MISMATCH,
            )
        if payload["config_hash"] != current_config_hash:
            raise ConfigMismatch(
                "config hash mismatch: snapshot "
                f"{payload['config_hash']} != current {current_config_hash}",
                E_CONFIG_MISMATCH,
            )
        if payload["translation_hash"] != current_translation_hash:
            raise ConfigMismatch(
                "translation hash mismatch: snapshot "
                f"{payload['translation_hash']} != current {current_translation_hash}",
                E_CONFIG_MISMATCH,
            )
        return True
