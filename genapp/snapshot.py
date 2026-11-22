"""生成快照的版本化与整体替换.

三类快照: 条件融合参数 (fusion_params) / 块划分计划 (block_plan) /
已就绪块清单 (ready_manifest). 每类携带单调递增版本号与内容校验和.
提交时整体替换 (写临时文件后原子替换), 不做增量修补; 旧版本不得
覆盖新版本; 加载时校验和不符返回 E_SNAPSHOT_CORRUPT 并指明种类与版本.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Dict, Optional

from .adjudicate import canonical_bytes
from .errors import E_SNAPSHOT_CORRUPT, E_SNAPSHOT_STALE, GenError

KIND_FUSION_PARAMS = "fusion_params"
KIND_BLOCK_PLAN = "block_plan"
KIND_READY_MANIFEST = "ready_manifest"
KINDS = (KIND_FUSION_PARAMS, KIND_BLOCK_PLAN, KIND_READY_MANIFEST)


def checksum_of(payload: Dict[str, object]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


class SnapshotStore:
    """本地文件系统上的版本化快照存储."""

    def __init__(self, root: str) -> None:
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._versions: Dict[str, int] = {}
        for kind in KINDS:
            record = self._read_file(kind)
            if record is not None:
                self._versions[kind] = int(record["version"])

    def _path(self, kind: str) -> str:
        return os.path.join(self.root, f"{kind}.json")

    def _read_file(self, kind: str) -> Optional[Dict[str, object]]:
        path = self._path(kind)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as fh:
            data = fh.read()
        try:
            record = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise GenError(
                E_SNAPSHOT_CORRUPT,
                f"snapshot file unreadable: {exc}",
                kind=kind,
                version=-1,
            )
        return record

    def submit(
        self, kind: str, version: int, payload: Dict[str, object]
    ) -> Dict[str, object]:
        """整体替换提交: 版本必须单调递增, 旧版本不得覆盖新版本."""
        if kind not in KINDS:
            raise ValueError(f"unknown snapshot kind: {kind}")
        current = self._versions.get(kind, 0)
        if version <= current:
            raise GenError(
                E_SNAPSHOT_STALE,
                "stale version cannot replace newer snapshot",
                kind=kind,
                version=version,
                current=current,
            )
        record = {
            "kind": kind,
            "version": int(version),
            "checksum": checksum_of(payload),
            "payload": payload,
        }
        tmp_path = self._path(kind) + ".tmp"
        with open(tmp_path, "wb") as fh:
            fh.write(canonical_bytes(record))
        os.replace(tmp_path, self._path(kind))
        self._versions[kind] = int(version)
        return record

    def load(self, kind: str) -> Optional[Dict[str, object]]:
        """加载并校验快照; 校验和不符返回 E_SNAPSHOT_CORRUPT."""
        record = self._read_file(kind)
        if record is None:
            return None
        version = int(record.get("version", -1))
        expect = checksum_of(record.get("payload", {}))
        if record.get("checksum") != expect:
            raise GenError(
                E_SNAPSHOT_CORRUPT,
                "snapshot checksum mismatch",
                kind=kind,
                version=version,
            )
        self._versions[kind] = version
        return record

    def load_all(self) -> Dict[str, Optional[Dict[str, object]]]:
        """重启加载: 逐类识别版本号与校验和, 变化即整体替换内存视图."""
        return {kind: self.load(kind) for kind in KINDS}

    def current_version(self, kind: str) -> int:
        return self._versions.get(kind, 0)
