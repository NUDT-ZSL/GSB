"""配置与统计量的版本化快照及整体替换。

每类快照（生成配置 / 量化参数 / 误差谱统计）是一个独立 JSON 文件：
  {"name", "version", "checksum", "payload"}
  - version  单调递增，初始为 1
  - checksum 对 payload 的规范 JSON 取 sha256

语义：
  - 加载时校验 checksum，不符 -> E_SNAPSHOT_CORRUPT
  - 保存时版本号必须严格大于已存版本，否则 -> E_SNAPSHOT_ROLLBACK
    （旧版本不得覆盖新版本，回退尝试显式报错）
  - 内容变化时整体替换（临时文件 + os.replace 原子写），不做增量修补
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .errors import StageError, E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK

SNAPSHOT_CONFIG = "config"
SNAPSHOT_QUANT = "quant"
SNAPSHOT_ERROR_SPECTRUM = "error_spectrum"
SNAPSHOT_NAMES = (SNAPSHOT_CONFIG, SNAPSHOT_QUANT, SNAPSHOT_ERROR_SPECTRUM)

_SYSTEM_TASK_ID = "__snapshot__"
_STAGE = "snapshot_store"


def canonical_json(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def content_checksum(payload) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class SnapshotStore:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.snapshot.json"

    def load(self, name: str):
        """加载并校验快照；不存在返回 None，损坏抛 E_SNAPSHOT_CORRUPT。"""
        path = self._path(name)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            version = int(doc["version"])
            checksum = str(doc["checksum"])
            payload = doc["payload"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StageError(
                _STAGE, E_SNAPSHOT_CORRUPT, _SYSTEM_TASK_ID,
                f"snapshot {name!r} is not parseable: {exc}",
                {"name": name}) from exc
        if content_checksum(payload) != checksum:
            raise StageError(
                _STAGE, E_SNAPSHOT_CORRUPT, _SYSTEM_TASK_ID,
                f"snapshot {name!r} checksum mismatch",
                {"name": name, "expected": checksum,
                 "actual": content_checksum(payload)})
        return {"name": name, "version": version,
                "checksum": checksum, "payload": payload}

    def save(self, name: str, payload, version: int) -> dict:
        """整体替换式保存；version 必须严格大于已存版本。"""
        existing = self.load(name)
        if existing is not None and version <= existing["version"]:
            raise StageError(
                _STAGE, E_SNAPSHOT_ROLLBACK, _SYSTEM_TASK_ID,
                f"snapshot {name!r}: version {version} does not advance "
                f"past stored version {existing['version']}",
                {"name": name, "attempted": version,
                 "stored": existing["version"]})
        doc = {
            "name": name,
            "version": int(version),
            "checksum": content_checksum(payload),
            "payload": payload,
        }
        path = self._path(name)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, path)  # 原子整体替换
        return doc

    def update(self, name: str, payload) -> dict:
        """以已存版本 +1（或初始版本 1）整体替换。"""
        existing = self.load(name)
        version = 1 if existing is None else existing["version"] + 1
        return self.save(name, payload, version)

    def versions(self) -> dict:
        out = {}
        for name in SNAPSHOT_NAMES:
            snap = self.load(name)
            out[name] = None if snap is None else {
                "version": snap["version"], "checksum": snap["checksum"]}
        return out
