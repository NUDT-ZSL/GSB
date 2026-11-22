"""意图配置快照的版本化、整体替换与回退保护。

快照文件按版本号落地，加载新版本时整体替换内存中的旧快照；
旧版本不得覆盖新版本，回退尝试返回 E_SNAPSHOT_ROLLBACK。
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import BizError
from .models import IntentConfig, canonical_json, sha256_hex


class SnapshotStore:
    """本地快照目录，文件名与内容均为确定性内容寻址。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._current: Optional[IntentConfig] = None

    def _path(self, version: int) -> Path:
        return self.root / f"config_v{version:04d}.json"

    def _index_path(self) -> Path:
        return self.root / "index.json"

    def saved_versions(self) -> List[int]:
        versions = [
            int(path.stem.split("_v")[-1])
            for path in self.root.glob("config_v*.json")
        ]
        return sorted(versions)

    def load_index_current_version(self) -> Optional[int]:
        index = self._index_path()
        if not index.exists():
            return None
        return int(json.loads(index.read_text(encoding="utf-8"))["current_version"])

    def save(self, config: IntentConfig) -> Dict[str, Any]:
        """保存新版本快照；同版本校验和变化报冲突，低版本报回退。"""
        current_version = self.load_index_current_version()
        if current_version is not None and config.version < current_version:
            raise BizError(
                "E_SNAPSHOT_ROLLBACK",
                "version",
                {
                    "attempted_version": config.version,
                    "current_version": current_version,
                },
            )

        target = self._path(config.version)
        if target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            if existing["checksum"] != config.checksum:
                raise BizError(
                    "E_SNAPSHOT_CONFLICT",
                    "checksum",
                    {
                        "version": config.version,
                        "existing_checksum": existing["checksum"],
                        "incoming_checksum": config.checksum,
                    },
                )

        target.write_text(
            canonical_json(config.to_dict()) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        index_payload = canonical_json(
            {
                "current_version": config.version,
                "checksum": config.checksum,
                "fingerprint": sha256_hex(canonical_json(config.to_dict())),
            }
        )
        self._index_path().write_text(
            index_payload + "\n", encoding="utf-8", newline="\n"
        )
        self._current = config
        return {
            "version": config.version,
            "checksum": config.checksum,
            "replaced_previous": current_version is not None
            and current_version != config.version,
            "previous_version": current_version,
            "saved_versions": self.saved_versions(),
        }

    def load(self, version: Optional[int] = None) -> IntentConfig:
        """加载指定版本；未知版本返回 E_UNKNOWN_CONFIG_VERSION。"""
        target_version = version
        if target_version is None:
            target_version = self.load_index_current_version()
        if target_version is None or not self._path(target_version).exists():
            raise BizError(
                "E_UNKNOWN_CONFIG_VERSION",
                "version",
                {"requested_version": version, "known_versions": self.saved_versions()},
            )
        payload = json.loads(self._path(target_version).read_text(encoding="utf-8"))
        config = IntentConfig.from_dict(payload)
        recomputed = IntentConfig.build(
            version=config.version,
            controlled_labels=config.controlled_labels,
            rules=config.rules,
            default_fallback_label=config.default_fallback_label,
        )
        if recomputed.checksum != config.checksum:
            raise BizError(
                "E_SNAPSHOT_CONFLICT",
                "checksum",
                {
                    "version": config.version,
                    "stored_checksum": config.checksum,
                    "recomputed_checksum": recomputed.checksum,
                },
            )
        self._current = config
        return config

    def current(self) -> Optional[IntentConfig]:
        return self._current

    def replace_whole(self, config: IntentConfig) -> Dict[str, Any]:
        """整体替换入口：新版本完整落盘并切换当前指针。"""
        return self.save(config)
